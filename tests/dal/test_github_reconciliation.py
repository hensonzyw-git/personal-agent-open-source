"""DAL-034: response-loss reconciliation for the ECS GitHub writes.

Two layers, mirroring the DAL-032 split:

- **Mechanics** (`personal_agent_dal.github.adapter` read methods) — the
  three authoritative read-backs (branch ref, open PR list, check run) as
  pure GETs with closed outcome shapes. A read can prove presence at an
  exact identity or absence; anything else is unknown. No write request is
  ever sent — that is the structural guarantee against a duplicate branch,
  PR or check during recovery.
- **Composition** (`personal_agent_dal.github.reconciliation`) — the frozen
  reconciliation edges driven through the real engine:
    EE-RECONCILE-START  unknown -> reconciling   (single reconciler claim)
    EE-RECONCILE-STILL-UNKNOWN  reconciling -> unknown
    RECONCILE-*-resume  human resume_checkpoint with the atomic
    EE-CLOSE-RECONCILE-* companion closing the effect.

The end-to-end drill replays the frozen ``push_ack_reconciled`` oracle's
binding shape against a real ack-loss outcome produced by
``dispatch_github_write``: the feature is parked at
``reconciliation_required`` by a real 5xx/drift composition, the
authoritative read-back then finds the write landed, and the human resume
closes the whole unit — event trace, external-effect trace, state trace and
receipts judged against the oracle's frozen values.

The §5.1 failure shapes this file pins:

reconciliation start
  - a second concurrent ``start_effect_reconciliation`` for another unknown
    effect owned by the same feature refuses (SINGLE_RECONCILER_CLAIM facts
    derived from rows, not asserted);
  - a version mismatch between the claimed effect row and the command
    refuses;
  - the start is refused for an effect that is not ``unknown``.

authoritative read-back
  - transport loss during the read is ``unknown``, never "absent" (fail
    closed): a dropped read must not be allowed to claim not-executed —
    pinned at both the adapter layer (scripted transport errors) and the
    composition layer (a read-back that raises);
  - a drifted identity (wrong PR, wrong external id) reads as not-found for
    the exact target — never as a confirmation; a mutable ref found at a
    different head SHA is *unknown*, not absent (the push may have landed
    and the ref advanced afterwards); an open-only PR listing with zero
    matches is *unknown*, not absent (the PR may exist closed or merged);
    a check listing with zero latest matches is *unknown*, not absent
    (``filter=latest`` hides older runs) — only the 404 shapes are
    absence evidence;
  - the read-back issues no POST/PUT/PATCH (the duplicate-write guard is
    structural, verified on the scripted transport).

still-unknown
  - an inconclusive read-back runs EE-RECONCILE-STILL-UNKNOWN and the
    feature stays stopped with its reason intact.

human resume (the frozen RECONCILE-* root)
  - the human-side fact bundle is derived from rows + read-back; tampered
    evidence (a forged semantic binding digest) is refused; the digest is
    input-sensitive (a different semantic tuple digests differently);
  - the resume moves the feature back to its checkpoint, atomically closes
    the effect, clears the stop reason, and emits exactly one feature
    transition receipt (the frozen push_ack_reconciled oracle count);
  - the resume target is derived server-side from the REC-UNKNOWN parking
    receipt's from_state — the client never supplies the checkpoint, and a
    feature that was never parked has no target to derive (the §2.2 rule
    that the client may not submit an arbitrary target state).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from personal_agent_dal.github.adapter import (
    BranchReadBack,
    CheckRunReadBack,
    GithubAdapter,
    GithubAdapterSettings,
    OpenPullRequestsReadBack,
)
from personal_agent_dal.github.reconciliation import (
    ReconciliationRefusal,
    build_resume_checkpoint_command,
    derive_resume_facts,
    parking_checkpoint_state,
    reconcile_github_write,
    start_effect_reconciliation,
)
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.binding import build_state_binding
from personal_agent_dal.machine.registry import jcs_sha256
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.contract_loader import FrozenContracts
from tests.dal.factories import (
    approval_row,
    decision_row,
    external_effect_row,
    feature_row,
    state_binding_sha256,
)


BRANCH = "dal/feat-1"
HEAD = "a" * 40
BASE = "main"
REPO = "example-owner/dal-sandbox"
CHECK_NAME = "deterministic-verification"
EXTERNAL_ID = "task-0001"


# ---------------------------------------------------------------------------
# Mechanics: the three authoritative read-backs over a scripted transport.
# ---------------------------------------------------------------------------


@dataclass
class _Response:
    status_code: int
    body: Any = None

    def json(self) -> Any:
        return self.body


class _ScriptedReadTransport:
    """Records every request and replays one script entry per call."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.requests: list[tuple[str, str]] = []
        self._script = list(script or [])
        self._script_error: Exception | None = None

    def fail_next(self, error: Exception) -> None:
        self._script_error = error

    def _next(self) -> Any:
        if self._script_error is not None:
            error, self._script_error = self._script_error, None
            raise error
        return self._script.pop(0)

    def get(self, url: str, **_: Any) -> Any:
        self.requests.append(("GET", url))
        return self._next()

    def post(self, url: str, **_: Any) -> Any:
        self.requests.append(("POST", url))
        return self._next()

    def put(self, url: str, **_: Any) -> Any:
        self.requests.append(("PUT", url))
        return self._next()

    def patch(self, url: str, **_: Any) -> Any:
        self.requests.append(("PATCH", url))
        return self._next()


def _settings(tmp_path: Path) -> GithubAdapterSettings:
    return GithubAdapterSettings(
        app_id="4807112",
        installation_id="158537127",
        private_key_path=tmp_path / "key.pem",
        repository=REPO,
    )


@pytest.fixture()
def key_file(tmp_path: Path) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "key.pem"
    path.write_bytes(pem)
    return path


def _adapter_with_token(
    tmp_path: Path, transport: _ScriptedReadTransport
) -> GithubAdapter:
    """An adapter whose transport already answers the token mint, so tests
    script only the read they exercise."""
    transport.requests.append(("POST", "seeded"))  # keep indices honest
    transport.requests.clear()
    transport._script.insert(
        0,
        _Response(
            201,
            {"token": "tok", "expires_at": "2100-01-01T00:00:00Z"},
        ),
    )
    return GithubAdapter(_settings(tmp_path), client=transport)  # type: ignore[arg-type]


def test_branch_readback_found_exact(tmp_path: Path, key_file: Path) -> None:
    transport = _ScriptedReadTransport(
        [_Response(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}})]
    )
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.read_feature_branch(branch=BRANCH)
    assert isinstance(read, BranchReadBack)
    assert read.found is True and read.head_sha == HEAD
    # The token mint (a POST to the installations endpoint) is part of the
    # credential plumbing and is recorded too; the *read* is exactly one GET
    # of this branch's ref. (The transport's "seeded" POST marker is cleared
    # before the call.)
    assert transport.requests == [
        ("POST", "https://api.github.com/app/installations/158537127/access_tokens"),
        ("GET", f"https://api.github.com/repos/{REPO}/git/ref/heads/{BRANCH}"),
    ]


def test_branch_readback_absent_is_a_proof(tmp_path: Path, key_file: Path) -> None:
    transport = _ScriptedReadTransport([_Response(404, {"message": "no ref"})])
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.read_feature_branch(branch=BRANCH)
    assert isinstance(read, BranchReadBack)
    assert read.found is False and read.head_sha is None


def test_branch_readback_transport_loss_is_unknown(tmp_path: Path, key_file: Path) -> None:
    import httpx as real_httpx

    transport = _ScriptedReadTransport()
    transport.fail_next(real_httpx.ConnectError("cable cut"))
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.read_feature_branch(branch=BRANCH)
    assert isinstance(read, BranchReadBack)
    assert read.found is None and read.unknown is True, (
        "a lost read must never be read as absence"
    )


def test_pr_readback_lists_exactly_one(tmp_path: Path, key_file: Path) -> None:
    pr = {
        "number": 7,
        "state": "open",
        "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
        "base": {"ref": BASE},
    }
    transport = _ScriptedReadTransport([_Response(200, [pr])])
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.list_open_pull_requests(branch=BRANCH, base_branch=BASE)
    assert isinstance(read, OpenPullRequestsReadBack)
    assert read.matches == 1
    assert read.pull_request_number == 7 and read.head_sha == HEAD


def test_pr_readback_zero_open_is_a_proof(tmp_path: Path, key_file: Path) -> None:
    transport = _ScriptedReadTransport([_Response(200, [])])
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.list_open_pull_requests(branch=BRANCH, base_branch=BASE)
    assert isinstance(read, OpenPullRequestsReadBack)
    assert read.matches == 0


def test_pr_readback_drifted_branch_is_not_a_match(tmp_path: Path, key_file: Path) -> None:
    """A PR naming another branch must not count as our PR."""
    pr = {
        "number": 7,
        "state": "open",
        "head": {"ref": "dal/other", "sha": HEAD, "repo": {"full_name": REPO}},
        "base": {"ref": BASE},
    }
    transport = _ScriptedReadTransport([_Response(200, [pr, pr])])
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.list_open_pull_requests(branch=BRANCH, base_branch=BASE)
    assert read.matches == 0, "a drifted PR must read as absent for this target"


def test_check_readback_found_exact(tmp_path: Path, key_file: Path) -> None:
    body = {
        "id": 99,
        "name": CHECK_NAME,
        "head_sha": HEAD,
        "external_id": EXTERNAL_ID,
        "conclusion": "success",
    }
    transport = _ScriptedReadTransport([_Response(200, {"total_count": 1, "check_runs": [body]})])
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.read_check_run(
            branch_head_sha=HEAD, check_name=CHECK_NAME, external_id=EXTERNAL_ID
        )
    assert isinstance(read, CheckRunReadBack)
    assert read.found is True and read.check_run_id == 99
    assert read.head_sha == HEAD


def test_check_readback_absent_is_a_proof(tmp_path: Path, key_file: Path) -> None:
    transport = _ScriptedReadTransport([_Response(200, {"total_count": 0, "check_runs": []})])
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        read = adapter.read_check_run(
            branch_head_sha=HEAD, check_name=CHECK_NAME, external_id=EXTERNAL_ID
        )
    assert isinstance(read, CheckRunReadBack)
    assert read.found is False


def test_readbacks_never_issue_write_requests(tmp_path: Path, key_file: Path) -> None:
    """The structural duplicate-write guard: every method is GET-only."""
    transport = _ScriptedReadTransport(
        [
            _Response(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}}),
            _Response(200, []),
            _Response(200, {"total_count": 0, "check_runs": []}),
        ]
    )
    adapter = _adapter_with_token(tmp_path, transport)
    with adapter:
        adapter.read_feature_branch(branch=BRANCH)
        adapter.list_open_pull_requests(branch=BRANCH, base_branch=BASE)
        adapter.read_check_run(
            branch_head_sha=HEAD, check_name=CHECK_NAME, external_id=EXTERNAL_ID
        )
    assert all(
        method == "GET"
        for method, url in transport.requests
        if not url.endswith("/access_tokens")
    ), (
        "a reconciliation read-back must never carry a write request "
        "(the token mint's POST is the only allowed non-GET)"
    )


# ---------------------------------------------------------------------------
# Composition: the frozen reconciliation edges through the real engine.
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path: Path):
    from personal_agent_dal.storage import db

    engine = create_database_engine(tmp_path / "dal.db")
    db.upgrade(engine)
    yield engine
    engine.dispose()


class _ReadBackStub:
    """The read-back result the test wants reconcile_github_write to see."""

    def __init__(self, read: Any, calls: int = 0) -> None:
        self.read = read
        self.calls = calls

    def _bump(self) -> Any:
        self.calls += 1
        return self.read

    def read_feature_branch(self, **_: Any) -> Any:
        return self._bump()

    def list_open_pull_requests(self, **_: Any) -> Any:
        return self._bump()

    def read_check_run(self, **_: Any) -> Any:
        return self._bump()


BRANCH_FOUND = BranchReadBack(found=True, head_sha=HEAD)
BRANCH_ABSENT = BranchReadBack(found=False)
BRANCH_ADVANCED = BranchReadBack(found=True, head_sha="b" * 40)
BRANCH_UNKNOWN = BranchReadBack(found=None, unknown=True)
PR_FOUND = OpenPullRequestsReadBack(matches=1, pull_request_number=7, head_sha=HEAD)
PR_ABSENT = OpenPullRequestsReadBack(matches=0)
CHECK_FOUND = CheckRunReadBack(found=True, check_run_id=99, head_sha=HEAD)
# Only the 404-on-SHA shape proves a check run absent; a live SHA whose
# latest-filtered listing names no match is unprovable (review F4, 2026-09-04).
CHECK_ABSENT = CheckRunReadBack(found=False, sha_absent=True)
CHECK_SUPERSEDED = CheckRunReadBack(found=False)


def seed_unknown_effect(
    engine, *, feature_state: str = "reconciliation_required",
    effect_state: str = "unknown",
) -> tuple[str, str]:
    """One parked feature plus its unknown push effect, as REC-UNKNOWN left it."""
    feature_id = "feature-rec-1"
    effect_id = "effect-rec-1"
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        feature = feature_row(
            feature_id=feature_id, version=4, state=feature_state, now=now
        )
        if feature_state == "reconciliation_required":
            feature.reason_code = "EXTERNAL_RESULT_UNKNOWN"
            feature.reason_owner = "feature"
        session.add(feature)
        session.add(
            external_effect_row(
                effect_id=effect_id, owner_id=feature_id, version=5,
                state=effect_state, now=now,
            )
        )
    return feature_id, effect_id


def seed_ready_resume(
    engine, *, checkpoint_before: str = "awaiting_merge"
) -> tuple[str, str, int]:
    """Everything the frozen RECONCILE-* resume composes from, for real.

    The feature is parked by the real controller composition
    (`_stop_feature_for_unknown`, i.e. a genuine REC-UNKNOWN parking
    receipt exists), the EE-RECONCILE-START claim moved the effect to
    ``reconciling``, and the open decision + action-bound approval the
    human submits against exist.
    Returns (feature_id, effect_id, decision_and_approval_digest).
    """
    feature_id, effect_id = seed_unknown_effect(
        engine, feature_state=checkpoint_before, effect_state="unknown"
    )
    from personal_agent_dal.github.adapter_controller import _stop_feature_for_unknown

    _stop_feature_for_unknown(engine, feature_id=feature_id, now_epoch=0)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-claim",
        feature_id=feature_id,
    )
    digest = jcs_sha256(build_state_binding(_feature(engine, feature_id)))
    _decision_and_approval(
        engine, feature_id, "reconciliation_required",
        feature_row_state(engine, feature_id), "resume_checkpoint",
        state_sha256=digest,
    )
    return feature_id, effect_id, digest


def feature_row_state(engine, feature_id: str) -> int:
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT version FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).first()
    assert row is not None
    return row[0]


def effect_row_state(engine, effect_id: str) -> tuple[str, int, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT state, version, executor_id FROM external_effects "
                "WHERE effect_id = :e"
            ).bindparams(e=effect_id)
        ).first()
    assert row is not None
    return row[0], row[1], row[2]


def test_start_reconciliation_claims_single_and_stamps_reconciler(engine) -> None:
    feature_id, effect_id = seed_unknown_effect(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-1", feature_id=feature_id
    )
    state, _version, executor = effect_row_state(engine, effect_id)
    assert state == "reconciling"
    assert executor == "reconciler", "the start stamps the reconciler claim"


def test_start_reconciliation_refuses_non_unknown_effect(engine) -> None:
    feature_id, effect_id = seed_unknown_effect(engine, effect_state="dispatch_started")
    with pytest.raises(ReconciliationRefusal, match="unknown"):
        start_effect_reconciliation(
            engine, effect_id=effect_id, idempotency_key="recon-1", feature_id=feature_id
        )


def test_second_concurrent_reconciler_refuses(engine) -> None:
    """SINGLE_RECONCILER_CLAIM: one reconciler per owner at a time.

    The first claim is live; a second start for a different unknown effect
    of the same feature must refuse from row-derived facts.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(
            external_effect_row(
                effect_id="effect-rec-2", owner_id=feature_id, version=1,
                state="unknown", now=now,
            )
        )
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-1", feature_id=feature_id
    )
    with pytest.raises(ReconciliationRefusal, match="reconcil"):
        start_effect_reconciliation(
            engine, effect_id="effect-rec-2", idempotency_key="recon-2",
            feature_id=feature_id,
        )


def test_concurrent_reconciler_claims_two_effects_one_owner(
    engine, monkeypatch
) -> None:
    """F3 (2026-09-07 review): the owner singleton must survive a real race.

    The SINGLE_RECONCILER_CLAIM guard's `active_claim_count` fact was computed
    outside the retriable transaction: two concurrent starts for two different
    unknown effects of one owner both read zero live claims, both passed the
    `equals 1` guard, and both committed. Here both threads pass the facts
    computation before either commits (a barrier at the facts boundary); the
    partial unique index from migration 0010 must arbitrate — exactly one
    claim survives, the other refuses with the mapped code.
    """
    import threading

    from personal_agent_dal.github import reconciliation as reconciliation_module

    feature_id, effect_id = seed_unknown_effect(engine)
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(
            external_effect_row(
                effect_id="effect-race-2", owner_id=feature_id, version=1,
                state="unknown", now=now,
            )
        )

    barrier = threading.Barrier(2)
    real_facts = reconciliation_module._reconciler_claim_facts

    def synchronized_facts(engine_arg, effect_id_arg):
        # Both threads compute their facts (both see zero live claims) before
        # either proceeds into the transition — the exact production interleave.
        result = real_facts(engine_arg, effect_id_arg)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        reconciliation_module, "_reconciler_claim_facts", synchronized_facts
    )

    outcomes: list[str] = []
    refusals: list[str] = []
    lock = threading.Lock()

    def worker(eid: str, key: str) -> None:
        try:
            start_effect_reconciliation(
                engine, effect_id=eid, idempotency_key=key,
                feature_id=feature_id,
            )
            with lock:
                outcomes.append(eid)
        except ReconciliationRefusal as refusal:
            with lock:
                refusals.append(refusal.code)
        except Exception as exc:  # noqa: BLE001
            with lock:
                refusals.append(f"EXC:{type(exc).__name__}:{exc}")

    threads = [
        threading.Thread(target=worker, args=("effect-rec-1", "race-a")),
        threading.Thread(target=worker, args=("effect-race-2", "race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "race threads hung"

    assert len(outcomes) == 1, f"exactly one claim must survive: {outcomes}"
    assert refusals == ["SINGLE_RECONCILER_CLAIM"], (
        f"the loser must refuse with the mapped code: {refusals}"
    )
    # One row in reconciling under this owner, the other still unknown.
    with engine.connect() as connection:
        states = dict(
            connection.execute(
                sa.text(
                    "SELECT effect_id, state FROM external_effects "
                    "WHERE effect_id IN ('effect-rec-1', 'effect-race-2')"
                )
            ).all()
        )
    assert sorted(states.values()) == ["reconciling", "unknown"], states


def test_feature_and_recovery_case_cannot_both_hold_claims(engine) -> None:
    """R2-4 (round-2 review): the cross-owner single-reconciler constraint.

    The SINGLE_RECONCILER_CLAIM enumeration only walked feature -> its
    recovery cases; a recovery-case-owned effect's facts never looked up at
    the parent feature, so a feature claim followed by a recovery-case claim
    — even serially — left both live. The DB arbitration must key on the
    reconciliation ROOT (the feature), not the direct owner: the claim
    stamps a root id (feature-owned effect: itself; recovery-case-owned:
    the parent feature), and the partial unique index binds that root.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    now = utc_now()
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="cross-a",
        feature_id=feature_id,
    )

    from tests.dal.factories import external_effect_row
    from personal_agent_dal.storage.machine_models import RecoveryCase

    with session_factory(engine)() as session, session.begin():
        session.add(
            RecoveryCase(
                recovery_case_id="recovery-case-1",
                feature_id=feature_id,
                version=1,
                state="investigating",
                execution_epoch=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            external_effect_row(
                effect_id="effect-case-1", owner_id="recovery-case-1",
                version=1, state="unknown", now=now, owner_type="recovery_case",
            )
        )

    with pytest.raises(ReconciliationRefusal) as excinfo:
        start_effect_reconciliation(
            engine, effect_id="effect-case-1", idempotency_key="cross-b",
        )
    # Serial interleave: the facts guard's upward enumeration refuses first
    # (POLICY_DENIED — the count is 2 against `equals 1`). The concurrent
    # interleave hits the arbitration index instead (SINGLE_RECONCILER_CLAIM,
    # covered by the race test below).
    assert excinfo.value.code in (
        "SINGLE_RECONCILER_CLAIM", "POLICY_DENIED",
    ), excinfo.value.code
    # The feature's claim survives; the case effect never left unknown.
    assert effect_row_state(engine, effect_id)[0] == "reconciling"
    assert effect_row_state(engine, "effect-case-1")[0] == "unknown"


def test_concurrent_cross_owner_claims_arbitrate_on_the_root(engine, monkeypatch) -> None:
    """R2-4: the DB arbitration covers the cross pair too.

    The partial unique index keys on the reconciliation ROOT, so a feature
    claim and a case claim of the same feature racing past the facts guard
    (both computed their counts before either committed) still cannot both
    land — the loser's claim UPDATE violates the root index."""
    import threading

    from personal_agent_dal.github import reconciliation as reconciliation_module

    feature_id, effect_id = seed_unknown_effect(engine)
    now = utc_now()
    from personal_agent_dal.storage.machine_models import RecoveryCase

    with session_factory(engine)() as session, session.begin():
        session.add(
            RecoveryCase(
                recovery_case_id="recovery-case-r",
                feature_id=feature_id,
                version=1,
                state="investigating",
                execution_epoch=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            external_effect_row(
                effect_id="effect-case-r", owner_id="recovery-case-r",
                version=1, state="unknown", now=now, owner_type="recovery_case",
            )
        )

    barrier = threading.Barrier(2)
    real_facts = reconciliation_module._reconciler_claim_facts

    def synchronized_facts(engine_arg, effect_id_arg):
        result = real_facts(engine_arg, effect_id_arg)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        reconciliation_module, "_reconciler_claim_facts", synchronized_facts
    )

    outcomes: list[str] = []
    refusals: list[str] = []
    lock = threading.Lock()

    def worker(eid: str, key: str) -> None:
        try:
            start_effect_reconciliation(
                engine, effect_id=eid, idempotency_key=key,
            )
            with lock:
                outcomes.append(eid)
        except ReconciliationRefusal as refusal:
            with lock:
                refusals.append(refusal.code)
        except Exception as exc:  # noqa: BLE001
            with lock:
                refusals.append(f"EXC:{type(exc).__name__}:{exc}")

    threads = [
        threading.Thread(target=worker, args=(effect_id, "cross-race-a")),
        threading.Thread(target=worker, args=("effect-case-r", "cross-race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "race threads hung"

    assert len(outcomes) == 1, f"exactly one cross-owner claim must survive: {outcomes}"
    assert refusals == ["SINGLE_RECONCILER_CLAIM"], refusals
    states = {
        effect_id: effect_row_state(engine, effect_id)[0],
        "effect-case-r": effect_row_state(engine, "effect-case-r")[0],
    }
    assert sorted(states.values()) == ["reconciling", "unknown"], states


def test_stale_version_start_refuses(engine) -> None:
    feature_id, effect_id = seed_unknown_effect(engine)
    _state, version, _executor = effect_row_state(engine, effect_id)
    with pytest.raises(ReconciliationRefusal):
        start_effect_reconciliation(
            engine, effect_id=effect_id, idempotency_key="recon-1",
            feature_id=feature_id, expected_version=version + 3,
        )


def test_reconcile_found_read_backs_completed(engine) -> None:
    """Branch found at the exact head: the read-back proves the write landed.

    The effect must stay ``reconciling`` — closing a feature-owned effect is
    the human root's act, never the service's (§3.6 terminal dispatch owners).
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(BRANCH_FOUND),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="push_branch",
        idempotency_key="recon-1",
        payload={"branch": BRANCH, "head_sha": HEAD},
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "confirmed_completed"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "reconciling", (
        "the service may not close a feature-owned effect; the human root does"
    )


def test_reconcile_advanced_branch_is_unknown_not_absent(engine) -> None:
    """A mutable ref found at a different SHA is unprovable, not absent.

    The push may have landed and the ref been advanced afterwards, so the
    read cannot prove the original write did not execute (the whole-track
    review finding 4, 2026-09-04). Only a server 404 (found=False) is
    absence evidence for a mutable ref.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(BRANCH_ADVANCED),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="push_branch",
        idempotency_key="recon-1",
        payload={"branch": BRANCH, "head_sha": HEAD},
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "unknown"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "unknown", "an unprovable mutable ref returns to unknown"


def test_reconcile_absent_read_backs_stays_reconciling(engine) -> None:
    """Absence provable from one read is still reported to the human layer.

    The service never closes; absence is the human root's not-executed proof
    only after the frozen resume guard accepts it. The only check-run shape
    that proves absence is the 404-on-SHA one: the commit itself does not
    exist, so no run against it can exist either.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(CHECK_ABSENT),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="write_check_run",
        idempotency_key="recon-1",
        payload={
            "branch_head_sha": HEAD, "check_name": CHECK_NAME,
            "external_id": EXTERNAL_ID,
        },
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "absent"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "reconciling"


def test_reconcile_pr_zero_open_matches_is_unknown_not_absent(engine) -> None:
    """``state=open`` with zero hits cannot prove a PR never existed.

    The whole-track review's F4 probe: the PR may have been created and
    then closed (or merged) between the lost dispatch response and the
    reconciliation read, and an open-only listing is blind to both. Judging
    "absent" from it hands the human root a not-executed proof the server
    does not actually have.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(PR_ABSENT),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="create_pull_request",
        idempotency_key="recon-1",
        payload={"branch": BRANCH, "base_branch": BASE},
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "unknown"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "unknown", "an unprovable PR absence returns to unknown"


def test_reconcile_check_zero_latest_matches_is_unknown_not_absent(engine) -> None:
    """A live SHA whose latest-filtered listing names no match is unprovable.

    ``filter=latest`` returns only each (name, external_id) pair's newest
    run; a subsequent run of the same check supersedes the original in that
    listing, so zero hits on a live SHA proves nothing about the original
    write. Only the 404-on-SHA shape (CHECK_ABSENT) is absence evidence.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(CHECK_SUPERSEDED),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="write_check_run",
        idempotency_key="recon-1",
        payload={
            "branch_head_sha": HEAD, "check_name": CHECK_NAME,
            "external_id": EXTERNAL_ID,
        },
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "unknown"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "unknown", "an unprovable check absence returns to unknown"


def test_stale_reconciler_cannot_release_a_newer_claim(engine) -> None:
    """R2-2 (round-2 review): a STILL-UNKNOWN from a superseded reconciler
    must not release the claim that replaced it.

    The old code re-read the row version *after* the read-back returned, so a
    slow reconciler whose claim had expired (reclaimed to unknown, re-claimed
    by a fresh pass to reconciling) would stamp its inconclusive result onto
    the NEW claim's version and roll the fresh claim back to unknown. The
    STILL-UNKNOWN CAS must bind the version this pass held when it entered
    reconciling: superseded → VERSION_CONFLICT refusal, fresh claim untouched.
    """
    feature_id, effect_id = seed_unknown_effect(engine)

    class PausingAdapter:
        """Reads run the reclaim+re-claim interleave, then report unknown."""

        def read_feature_branch(self, **_: Any):
            # While this (old) reconciler's read is in flight: its claim
            # expires, the sweep reclaims it to unknown, and a NEW reconciler
            # claims it — the version moves past what the old pass held.
            from personal_agent_dal.github.executor import (
                _recover_expired_reconciling,
            )

            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE external_effects SET claim_expires_at = :past "
                        "WHERE effect_id = :e"
                    ).bindparams(past="2020-01-01T00:00:00Z", e=effect_id)
                )
            _recover_expired_reconciling(engine, limit=5, now_epoch=int(time.time()))
            # The fresh claim, through the real production entry point.
            start_effect_reconciliation(
                engine,
                effect_id=effect_id,
                idempotency_key="recon-fresh",
                feature_id=feature_id,
            )
            return BranchReadBack(found=None, unknown=True)

    with pytest.raises(ReconciliationRefusal) as excinfo:
        reconcile_github_write(
            engine,
            PausingAdapter(),
            effect_id=effect_id,
            action="push_branch",
            idempotency_key="recon-old",
            payload={"branch": BRANCH, "head_sha": HEAD},
            feature_id=feature_id,
        )
    assert excinfo.value.code == "VERSION_CONFLICT", excinfo.value.code
    # The fresh claim survives untouched.
    state, _version, executor = effect_row_state(engine, effect_id)
    assert state == "reconciling", "the newer claim must not be rolled back"
    assert executor == "reconciler"


def test_stale_claim_before_read_back_cannot_adopt_the_replacement(engine) -> None:
    """R3-1 (round-3 review): the version must come from the claim operation
    itself, never from a post-commit row read.

    The old code read `claimed_version` from the row AFTER
    start_effect_reconciliation returned — not atomic. The interleave: the
    old claim commits → it expires → the sweep reclaims → a fresh reconciler
    claims → the old pass's row read lands NOW and adopts the fresh claim's
    version, then still-unknowns (or reports a verdict) against it. The
    claim operation returns its own committed version, and every verdict —
    unknown and conclusive alike — is void if the row has moved past it.
    """
    feature_id, effect_id = seed_unknown_effect(engine)

    class SupersedingAdapter:
        """Reads first supersede the claim, then report the given shape."""

        def __init__(self, read: Any) -> None:
            self.read = read

        def read_feature_branch(self, **_: Any):
            from personal_agent_dal.github.executor import (
                _recover_expired_reconciling,
            )

            # The old pass has committed its claim and is between the claim
            # and the read: expire it, reclaim it, let a fresh pass claim.
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE external_effects SET claim_expires_at = :past "
                        "WHERE effect_id = :e"
                    ).bindparams(past="2020-01-01T00:00:00Z", e=effect_id)
                )
            _recover_expired_reconciling(engine, limit=5, now_epoch=int(time.time()))
            start_effect_reconciliation(
                engine,
                effect_id=effect_id,
                idempotency_key="recon-fresh-r3",
                feature_id=feature_id,
            )
            return self.read

    # Unknown verdict from the superseded pass: must refuse, not roll the
    # fresh claim back.
    with pytest.raises(ReconciliationRefusal) as excinfo:
        reconcile_github_write(
            engine,
            SupersedingAdapter(BranchReadBack(found=None, unknown=True)),
            effect_id=effect_id,
            action="push_branch",
            idempotency_key="recon-old-r3",
            payload={"branch": BRANCH, "head_sha": HEAD},
            feature_id=feature_id,
        )
    assert excinfo.value.code == "VERSION_CONFLICT", excinfo.value.code
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "reconciling", "the fresh claim must survive"

    # A conclusive verdict from a superseded pass is equally void: the read
    # must not be attributed to the replacement claim either.
    with pytest.raises(ReconciliationRefusal) as excinfo2:
        reconcile_github_write(
            engine,
            SupersedingAdapter(BRANCH_FOUND),
            effect_id=effect_id,
            action="push_branch",
            idempotency_key="recon-old-r3b",
            payload={"branch": BRANCH, "head_sha": HEAD},
            feature_id=feature_id,
        )
    assert excinfo2.value.code == "VERSION_CONFLICT", excinfo2.value.code


def test_reconcile_inconclusive_read_backs_to_still_unknown(engine) -> None:
    """A lost read-back cannot confirm anything: EE-RECONCILE-STILL-UNKNOWN."""
    feature_id, effect_id = seed_unknown_effect(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-0", feature_id=feature_id
    )
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(BRANCH_UNKNOWN),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="push_branch",
        idempotency_key="recon-1",
        payload={"branch": BRANCH, "head_sha": HEAD},
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "unknown"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "unknown", "an inconclusive read returns the effect to unknown"
    # The STILL-UNKNOWN write set has no claim-release member, so the stamped
    # executor_id stays; the claim is released *by the state*, not by the
    # column — a re-entry's reconciler.active_claim_count counts only rows
    # currently in `reconciling`, so the same effect can claim again.
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-2", feature_id=feature_id
    )
    assert effect_row_state(engine, effect_id)[0] == "reconciling", (
        "a still-unknown effect can re-enter reconciliation (no deadlock)"
    )


def test_transport_loss_during_read_back_is_unknown_at_composition(engine) -> None:
    """A raising read-back is judged unknown, never absent (fail closed).

    The adapter layer pins this for scripted transport errors; this pins
    the composition's own except branch: whatever raises under the read,
    the judged outcome must be "unknown" and the effect must go back to
    unknown via STILL-UNKNOWN — a dropped read must never claim
    not-executed.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-0", feature_id=feature_id
    )

    class _ExplodingReadBack:
        def read_feature_branch(self, **_: Any) -> Any:
            raise httpx.ConnectError("connection dropped mid-read")

    outcome = reconcile_github_write(
        engine,
        _ExplodingReadBack(),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="push_branch",
        idempotency_key="recon-1",
        payload={"branch": BRANCH, "head_sha": HEAD},
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "unknown", outcome.authoritative_result
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "unknown", "a transport loss returns the effect to unknown"


def test_semantic_binding_digest_is_input_sensitive(engine) -> None:
    """A different semantic tuple digests differently.

    The cross-source stage in this composition proves the evidence package
    is internally consistent (one server-side digest recomputation); the
    thing that makes the tamper test meaningful is that the digest is a
    function of the tuple's inputs, not a constant. Mutation (c-v2) — a
    constant binding digest — passed the whole suite before this existed.
    """
    feature_id, effect_id, _digest = seed_ready_resume(engine)
    true_facts = derive_resume_facts(
        engine,
        feature_id=feature_id,
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_action="resume_checkpoint",
        read_back=BRANCH_FOUND,
        authoritative_receipt_id="remote-0001",
    )
    forged_facts = derive_resume_facts(
        engine,
        feature_id=feature_id,
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_action="resume_checkpoint",
        read_back=BRANCH_FOUND,
        # a different authoritative receipt id changes the semantic tuple
        authoritative_receipt_id="remote-9999",
    )
    assert (
        true_facts.values["evidence.semantic_binding_sha256"]
        != forged_facts.values["evidence.semantic_binding_sha256"]
    ), "the binding digest must depend on the tuple's inputs"
    assert (
        true_facts.values["evidence.authoritative_readback_sha256"]
        != forged_facts.values["evidence.authoritative_readback_sha256"]
    ), "the readback digest must depend on the tuple's inputs"


def test_reconcile_unknown_payload_keys_refuse(engine) -> None:
    feature_id, effect_id = seed_unknown_effect(engine)
    with pytest.raises(Exception):
        reconcile_github_write(
            engine,
            _ReadBackStub(BRANCH_FOUND),  # type: ignore[arg-type]
            effect_id=effect_id,
            action="push_branch",
            idempotency_key="recon-1",
            payload={"branch": BRANCH, "head_sha": HEAD, "extra": 1},
            feature_id=feature_id,
        )


# ---------------------------------------------------------------------------
# The end-to-end drill against the frozen push_ack_reconciled oracle.
# ---------------------------------------------------------------------------


def _checkpoint_of(engine, feature_id: str) -> str | None:
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT checkpoint_state FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).first()
    assert row is not None
    return row[0]


def _decision_and_approval(engine, feature_id: str, feature_state: str, version: int,
                           action: str, state_sha256: str | None = None) -> tuple[str, str]:
    """Seed the open decision + action-bound approval the resume consumes."""
    from personal_agent_dal.storage.models import Feature

    with session_factory(engine)() as session:
        feature = session.get(Feature, feature_id)
        assert feature is not None
        digest = state_sha256 or state_binding_sha256(feature)
    decision_id = "decision-rec-1"
    approval_id = "approval-rec-1"
    with session_factory(engine)() as session, session.begin():
        session.add(
            decision_row(
                feature_id=feature_id, decision_id=decision_id,
                state_sha256=digest,
            )
        )
        session.add(
            approval_row(
                feature_id=feature_id, approval_id=approval_id, action=action,
                decision_id=decision_id, decision_version=1,
                expected_feature_version=version, expected_state=feature_state,
                state_sha256=digest,
            )
        )
    return decision_id, approval_id


def test_full_drill_matches_frozen_push_ack_reconciled_oracle(engine) -> None:
    """The frozen oracle shape, executed end to end:

    1. a real ack loss (5xx composition) parks the feature;
    2. EE-RECONCILE-START takes the single reconciler claim;
    3. the authoritative read-back finds the branch at the exact head
       (confirmed_completed) — the service reports, never closes;
    4. the human resume_checkpoint closes the effect and resumes the
       feature, with the event/effect/state traces and the single
       transition receipt the oracle freezes.
    """
    # --- 1. the real ack loss -------------------------------------------
    from tests.dal.test_github_adapter import (
        SERVER_5XX_PUSH,
        StubAdapter,
        push_payload,
    )
    from personal_agent_dal.github.adapter_controller import dispatch_github_write

    feature_id = "feature-gh-1"
    effect_id = "effect-gh-1"
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(feature_id=feature_id, version=3, state="awaiting_merge", now=now))
        session.add(
            external_effect_row(
                effect_id=effect_id, owner_id=feature_id, version=1,
                state="intent_recorded", now=now,
            )
        )
    # The intent row carries the frozen binding the real caller stamps
    # (target fingerprint over action+payload, the remote dedupe key);
    # the composition refuses an unstamped or drifted target zero-write.
    from personal_agent_dal.github.adapter_controller import fingerprint_for
    from personal_agent_dal.storage.machine_models import ExternalEffect
    with session_factory(engine)() as session, session.begin():
        row = session.get(ExternalEffect, effect_id)
        assert row is not None
        row.target_fingerprint = fingerprint_for("push_branch", push_payload())
        row.remote_idempotency_key = "idem-loss"
    dispatch_github_write(
        engine, StubAdapter(SERVER_5XX_PUSH),  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-loss",
        payload=push_payload(), feature_id=feature_id,
    )
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "unknown"

    # --- 2. the reconciler claim ----------------------------------------
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="idem-recon",
        feature_id=feature_id,
    )
    assert effect_row_state(engine, effect_id)[0] == "reconciling"

    # --- 3. the authoritative read-back ---------------------------------
    outcome = reconcile_github_write(
        engine,
        _ReadBackStub(BRANCH_FOUND),  # type: ignore[arg-type]
        effect_id=effect_id,
        action="push_branch",
        idempotency_key="idem-recon",
        payload=push_payload(),
        feature_id=feature_id,
    )
    assert outcome.authoritative_result == "confirmed_completed"

    # --- 4. the human resume, judged against the frozen oracle ----------
    # `reconciliation_required` is not a checkpoint-required state, so the
    # park wrote checkpoint_state=None (the engine verified). The resume
    # target is therefore derived server-side from the parking receipt's
    # from_state — which the real 5xx park recorded as awaiting_merge.
    resume_target = parking_checkpoint_state(engine, feature_id)
    assert resume_target == "awaiting_merge"
    assert _checkpoint_of(engine, feature_id) is None, (
        "the park left no checkpoint; the receipt is the saved target"
    )
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT state, version FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).first()
    feature_state, feature_version = row

    decision_id, approval_id = _decision_and_approval(
        engine, feature_id, feature_state, feature_version, "resume_checkpoint"
    )
    read_sha = jcs_sha256(
        build_state_binding(_feature(engine, feature_id))
    )
    facts = derive_resume_facts(
        engine,
        feature_id=feature_id,
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_action="resume_checkpoint",
        read_back=outcome.read_back,
        authoritative_receipt_id="remote-0001",
    )
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT version FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).first()
    facts.values["external_effect.version"] = row[0]
    facts.values["evidence.external_effect_version"] = row[0]
    command = build_resume_checkpoint_command(
        engine,
        feature_id=feature_id,
        expected_version=feature_version,
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_id=decision_id,
        decision_version=1,
        approval_id=approval_id,
        observed_state_sha256=read_sha,
        idempotency_key="idem-resume",
        evidence_facts=facts,
    )
    from personal_agent_dal.machine.engine import apply_transition

    result = apply_transition(engine, command, facts=facts)
    assert result.receipt_code == "APPLIED", result.receipt_code
    assert result.evidence_validation_stage == "applied", (
        "the resume carries real evidence documents"
    )

    # The frozen oracle's dimensions, judged against what actually happened.
    assert list(result.events) == [
        "feature.resumed",
        "external_effect.reconciled",
    ], result.events
    assert result.to_state == "awaiting_merge"
    assert effect_row_state(engine, effect_id)[0] == "confirmed_completed"

    with engine.connect() as connection:
        reason = connection.execute(
            sa.text("SELECT reason_code, reason_owner FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).first()
    assert reason == (None, None), "the resume clears the stop reason"

    # Exactly one feature transition receipt for the resume (the oracle's
    # APPLIED count is one; the companion receipt is a separate row whose
    # schema is the external-effect one).
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT receipt_schema_version FROM transition_receipts "
                "WHERE idempotency_key = :k"
            ).bindparams(k="idem-resume")
        ).all()
    assert [r[0] for r in rows] == ["dal.transition-receipt/1.0"]
    with engine.connect() as connection:
        companion = connection.execute(
            sa.text(
                "SELECT receipt_schema_version FROM transition_receipts "
                "WHERE idempotency_key LIKE 'idem-resume:%'"
            )
        ).all()
    assert [r[0] for r in companion] == ["dal.external-effect-transition-receipt/1.0"]


def _feature(engine, feature_id: str) -> Any:
    from personal_agent_dal.storage.models import Feature

    with session_factory(engine)() as session:
        return session.get(Feature, feature_id)


def test_forged_semantic_binding_refuses_the_resume(engine) -> None:
    """A tampered semantic digest must not pass the cross-source check."""
    feature_id, effect_id, digest = seed_ready_resume(engine)
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT version FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).first()
    feature = _feature(engine, feature_id)
    decision_id = "decision-rec-1"
    approval_id = "approval-rec-1"
    facts = derive_resume_facts(
        engine,
        feature_id=feature_id,
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_action="resume_checkpoint",
        read_back=BRANCH_FOUND,
        authoritative_receipt_id="remote-0001",
    )
    facts.values["external_effect.version"] = row[0]
    facts.values["evidence.external_effect_version"] = row[0]
    # The forgery: the device's recomputed binding disagrees with the
    # controller's. Applied BEFORE the documents are built, so the device
    # document carries the forged digest while the controller's stays true.
    facts.values["runtime.recomputed_registered_device_semantic_binding_sha256"] = "f" * 64
    command = build_resume_checkpoint_command(
        engine,
        feature_id=feature_id,
        expected_version=feature.version,
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_id=decision_id,
        decision_version=1,
        approval_id=approval_id,
        observed_state_sha256=digest,
        idempotency_key="idem-forge",
        evidence_facts=facts,
    )

    from personal_agent_dal.machine.engine import apply_transition

    result = apply_transition(engine, command, facts=facts)
    # The engine returns a POLICY_DENIED receipt for a refused transition —
    # it never raises. The refusal is a cross-source evidence_set one.
    assert result.receipt_code == "POLICY_DENIED", result.receipt_code
    assert (result.evidence_validation_stage or "") == "cross_source_consistency", (
        f"expected the evidence_set cross-source stage, got {result.evidence_validation_stage}"
    )
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "reconciling", "a refused resume must not move anything"


def test_resume_checkpoint_is_derived_from_parking_receipt(engine) -> None:
    """The client does not choose the resume target.

    `reconciliation_required` is not checkpoint-required (no saved
    `checkpoint_state` column value), so the only server record of where
    the work actually was is the REC-UNKNOWN parking receipt's own
    `from_state`. Deriving with no explicit checkpoint must resolve the
    spec the feature really parked from — not one the caller picked.
    """
    feature_id, effect_id, digest = seed_ready_resume(
        engine, checkpoint_before="coding"
    )
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT version FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).first()
    feature = _feature(engine, feature_id)
    facts = derive_resume_facts(
        engine,
        feature_id=feature_id,
        effect_id=effect_id,
        # no checkpoint_state: derived from the parking receipt
        effect_outcome="confirmed_completed",
        decision_action="resume_checkpoint",
        read_back=BRANCH_FOUND,
        authoritative_receipt_id="remote-0001",
    )
    facts.values["external_effect.version"] = row[0]
    facts.values["evidence.external_effect_version"] = row[0]
    assert facts.values["checkpoint.state"] == "coding", (
        "the derived checkpoint is the parking receipt's from_state, not a caller choice"
    )
    command = build_resume_checkpoint_command(
        engine,
        feature_id=feature_id,
        expected_version=feature.version,
        # no checkpoint_state: the spec resolves to --coding, from the receipt
        effect_id=effect_id,
        effect_outcome="confirmed_completed",
        decision_id="decision-rec-1",
        decision_version=1,
        approval_id="approval-rec-1",
        observed_state_sha256=digest,
        idempotency_key="idem-derived-ckpt",
        evidence_facts=facts,
    )

    from personal_agent_dal.machine.engine import apply_transition

    result = apply_transition(engine, command, facts=facts)
    assert result.receipt_code == "APPLIED", result.receipt_code
    assert result.to_state == "coding", result.to_state
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "confirmed_completed"


def test_resume_without_parking_receipt_refuses(engine) -> None:
    """No REC-UNKNOWN receipt — there is no server-side checkpoint to derive.

    A feature that was never parked by require_reconciliation has no
    authoritative resume target; deriving one must refuse instead of
    trusting any caller-supplied state.
    """
    feature_id, effect_id = seed_unknown_effect(engine)
    with pytest.raises(ReconciliationRefusal, match="parking receipt"):
        derive_resume_facts(
            engine,
            feature_id=feature_id,
            effect_id=effect_id,
            effect_outcome="confirmed_completed",
            decision_action="resume_checkpoint",
            read_back=BRANCH_FOUND,
            authoritative_receipt_id="remote-0001",
        )


def test_not_executed_resume_closes_without_receipt(engine) -> None:
    """confirmed_not_executed: the companion closes without a receipt id."""
    feature_id, effect_id, digest = seed_ready_resume(engine)
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT version FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).first()
    feature = _feature(engine, feature_id)
    decision_id = "decision-rec-1"
    approval_id = "approval-rec-1"
    facts = derive_resume_facts(
        engine,
        feature_id=feature_id,
        effect_id=effect_id,
        effect_outcome="confirmed_not_executed",
        decision_action="resume_checkpoint",
        read_back=PR_ABSENT,
        authoritative_receipt_id=None,
    )
    facts.values["external_effect.version"] = row[0]
    facts.values["evidence.external_effect_version"] = row[0]
    command = build_resume_checkpoint_command(
        engine,
        feature_id=feature_id,
        expected_version=feature.version,
        effect_id=effect_id,
        effect_outcome="confirmed_not_executed",
        decision_id=decision_id,
        decision_version=1,
        approval_id=approval_id,
        observed_state_sha256=digest,
        idempotency_key="idem-notexec",
        evidence_facts=facts,
    )

    from personal_agent_dal.machine.engine import apply_transition
    from personal_agent_dal.machine.guards import GuardFacts

    result = apply_transition(engine, command, facts=facts)
    assert result.receipt_code == "APPLIED", result.receipt_code
    assert result.to_state == "awaiting_merge"
    state, _version, _executor = effect_row_state(engine, effect_id)
    assert state == "confirmed_not_executed"
