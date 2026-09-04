"""The durable GitHub dispatch executor (whole-track review finding 5, F5).

Henson's frozen scope (2026-09-04), pinned here as tests:

- the operator wakes an already-existing, state/version-bound effect — the
  request carries ``(effect_id, expected_state, expected_version)`` and
  **nothing else**; extra request fields (action/payload/idempotency key/
  branch/SHA/body) cannot exist on the surface, so a forged target cannot
  ride in;
- owner, action, payload and remote key are derived from persistence (the
  intent row + the ``effect_dispatch_targets`` record); a missing target
  record refuses, a target/intent fingerprint disagreement refuses;
- intent/claim/dispatch each stay their own committed transaction
  (dispatch_github_write's composition order is already pinned in
  ``test_github_adapter.py`` — here the executor adds the binding checks
  ahead of it);
- a crash after ``dispatch_started`` is never answered by a re-send: waking
  a parked effect refuses, and the recovery path is the reconciliation
  sweep, driven by persistence (``run_unknown_sweep``), which only ever
  issues read-only read-backs;
- the sweep's STILL-UNKNOWN exit returns effects to ``unknown`` so repeated
  passes converge without duplicating a write.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from personal_agent_dal.github.adapter import (
    AdapterRefusal,
    BranchReadBack,
    OpenPullRequestsReadBack,
    PushOutcome,
)
from personal_agent_dal.github.executor import (
    ExecutorRefusal,
    record_effect_target,
    record_github_write_intent,
    run_unknown_sweep,
    unknown_effects,
    wake_effect,
)
from personal_agent_dal.github.adapter_controller import (
    dispatch_github_write,
    fingerprint_for,
)
from personal_agent_dal.github.reconciliation import (
    ReconciliationRefusal,
    start_effect_reconciliation,
)
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import (
    create_database_engine,
    session_factory,
)
from personal_agent_dal.storage.machine_models import EffectDispatchTarget
from personal_agent_core.timeutil import utc_now

from tests.dal.factories import external_effect_row, feature_row

BRANCH = "dal/feat-1"
HEAD = "a" * 40
BASE = "main"
REPO = "example-owner/dal-sandbox"


class StubAdapter:
    """Outcome injection per call; records every call for no-refire pins."""

    def __init__(self, outcome: Any = None, *, raise_error: Exception | None = None) -> None:
        self.outcome = outcome
        self.raise_error = raise_error
        self.calls: list[str] = []

    def _call(self, name: str, **kwargs: Any) -> Any:
        self.calls.append(name)
        if self.raise_error is not None:
            raise self.raise_error
        return self.outcome

    def push_feature_branch(self, **kwargs: Any) -> Any:
        return self._call("push", **kwargs)

    def create_pull_request(self, **kwargs: Any) -> Any:
        return self._call("pr", **kwargs)

    def write_check_run(self, **kwargs: Any) -> Any:
        return self._call("check", **kwargs)

    def read_feature_branch(self, **kwargs: Any) -> Any:
        return self._call("read_branch", **kwargs)

    def list_open_pull_requests(self, **kwargs: Any) -> Any:
        return self._call("read_prs", **kwargs)

    def read_check_run(self, **kwargs: Any) -> Any:
        return self._call("read_check", **kwargs)


CONFIRMED_PUSH = PushOutcome(repository_id=REPO, branch=BRANCH, head_sha=HEAD)


def push_payload() -> dict[str, Any]:
    return {"branch": BRANCH, "head_sha": HEAD}


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "executor.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


def seed_intent(
    engine,
    *,
    effect_id: str = "effect-f5-1",
    feature_id: str = "feature-f5-1",
    feature_state: str = "awaiting_merge",
    effect_state: str = "intent_recorded",
    version: int = 1,
    with_target: bool = True,
    stamp_fingerprint: bool = True,
    remote_key: str | None = None,
) -> tuple[str, str]:
    """One feature plus its intent effect, exactly the frozen pairing."""
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(
            feature_id=feature_id, version=3, state=feature_state, now=now
        ))
        session.add(external_effect_row(
            effect_id=effect_id, owner_id=feature_id, version=version,
            state=effect_state, now=now,
        ))
    if stamp_fingerprint or with_target:
        with session_factory(engine)() as session, session.begin():
            from personal_agent_dal.storage.machine_models import ExternalEffect

            row = session.get(ExternalEffect, effect_id)
            assert row is not None
            if stamp_fingerprint:
                row.target_fingerprint = fingerprint_for("push_branch", push_payload())
            if remote_key is not None:
                row.remote_idempotency_key = remote_key
    if with_target:
        record_target(engine, effect_id=effect_id)
    return feature_id, effect_id


def record_target(engine, *, effect_id: str, action: str = "push_branch",
                  payload: dict[str, Any] | None = None) -> None:
    payload = payload if payload is not None else push_payload()
    with session_factory(engine)() as session, session.begin():
        record_effect_target(
            session, effect_id=effect_id, action=action,
            payload=payload, now=utc_now(),
        )


def effect_row_full(engine, effect_id: str) -> tuple[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT state, version FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).first()
    assert row is not None
    return row[0], row[1]


# ---------------------------------------------------------------------------
# Derivation: everything from persistence, nothing from the request.
# ---------------------------------------------------------------------------


def test_wake_derives_owner_action_payload_and_key_from_persistence(engine) -> None:
    """The request names only the binding; the write uses the stored target."""
    _feature_id, effect_id = seed_intent(
        engine, remote_key="idem-f5-1",
    )
    adapter = StubAdapter(CONFIRMED_PUSH)
    outcome = wake_effect(
        engine, adapter,
        effect_id=effect_id,
        expected_state="intent_recorded",
        expected_version=1,
    )
    assert outcome.dispatch is not None
    assert outcome.dispatch.effect_state == "dispatch_started"
    assert adapter.calls == ["push"]
    # The stored remote key, not a request-supplied one, drove the composition.
    with engine.connect() as connection:
        keys = [
            r[0]
            for r in connection.execute(
                sa.text("SELECT idempotency_key FROM transition_receipts "
                        "WHERE aggregate_id = :e").bindparams(e=effect_id)
            ).all()
        ]
    assert any(k.startswith("idem-f5-1:") for k in keys), keys


def test_wake_refuses_when_no_target_record_exists(engine) -> None:
    """An intent without its persisted target must refuse, never fall back to
    request fields — that fallback is exactly the F5 hole."""
    _feature_id, effect_id = seed_intent(engine, with_target=False)
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ExecutorRefusal) as excinfo:
        wake_effect(
            engine, adapter,
            effect_id=effect_id,
            expected_state="intent_recorded",
            expected_version=1,
        )
    assert excinfo.value.code == "TARGET_MISSING"
    assert adapter.calls == [], "no adapter call may happen without a target"
    assert effect_row_full(engine, effect_id)[0] == "intent_recorded"


def test_target_fingerprint_disagreement_refuses_zero_write(engine) -> None:
    """A target record edited behind the intent row is a broken pairing."""
    _feature_id, effect_id = seed_intent(engine)
    drift = {"branch": "dal/other", "head_sha": "b" * 40}
    from personal_agent_core.manifest import canonical_json

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE effect_dispatch_targets SET payload_json = :payload, "
                "target_fingerprint = :fingerprint WHERE effect_id = :effect_id"
            ).bindparams(
                payload=canonical_json(drift),
                fingerprint=fingerprint_for("push_branch", drift),
                effect_id=effect_id,
            )
        )
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ExecutorRefusal) as excinfo:
        wake_effect(
            engine, adapter,
            effect_id=effect_id,
            expected_state="intent_recorded",
            expected_version=1,
        )
    assert excinfo.value.code == "FINGERPRINT_MISMATCH"
    assert adapter.calls == []


def test_record_effect_target_refuses_non_atomic_fingerprint_pair(engine) -> None:
    """The producer cannot bolt a target onto an unrelated intent later."""
    _feature_id, effect_id = seed_intent(engine, with_target=False)
    with session_factory(engine)() as session, session.begin():
        with pytest.raises(ExecutorRefusal) as excinfo:
            record_effect_target(
                session,
                effect_id=effect_id,
                action="push_branch",
                payload={"branch": "dal/drift", "head_sha": "b" * 40},
                now=utc_now(),
            )
    assert excinfo.value.code == "FINGERPRINT_MISMATCH"


def test_record_effect_target_refuses_an_unknown_action(engine) -> None:
    engine_target = "effect-x"
    with session_factory(engine)() as session, session.begin():
        with pytest.raises(ExecutorRefusal):
            record_effect_target(
                session, effect_id=engine_target, action="merge_pr",
                payload={"branch": "b"}, now=utc_now(),
            )


def test_production_intent_producer_writes_effect_and_target_atomically(engine) -> None:
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(
            feature_id="feature-producer", version=3, state="verified", now=now
        ))
    effect_id = record_github_write_intent(
        engine,
        owner_feature_id="feature-producer",
        action="push_branch",
        payload=push_payload(),
        remote_idempotency_key="producer-key",
    )
    replay_id = record_github_write_intent(
        engine,
        owner_feature_id="feature-producer",
        action="push_branch",
        payload=push_payload(),
        remote_idempotency_key="producer-key",
    )
    assert replay_id == effect_id
    with engine.connect() as connection:
        pair = connection.execute(
            sa.text(
                "SELECT e.state, t.action, t.payload_json "
                "FROM external_effects e JOIN effect_dispatch_targets t "
                "ON t.effect_id = e.effect_id WHERE e.effect_id = :effect_id"
            ).bindparams(effect_id=effect_id)
        ).one()
    assert pair.state == "intent_recorded"
    assert pair.action == "push_branch"
    assert json.loads(pair.payload_json) == push_payload()


def test_production_intent_producer_rolls_back_both_rows_on_target_failure(
    engine, monkeypatch
) -> None:
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(
            feature_id="feature-rollback", version=3, state="verified", now=now
        ))

    def fail_target(*args, **kwargs):
        raise ExecutorRefusal("TARGET_INVALID", "injected target failure")

    monkeypatch.setattr(
        "personal_agent_dal.github.executor.record_effect_target", fail_target
    )
    with pytest.raises(ExecutorRefusal, match="injected target failure"):
        record_github_write_intent(
            engine,
            owner_feature_id="feature-rollback",
            action="push_branch",
            payload=push_payload(),
            remote_idempotency_key="rollback-key",
        )
    with engine.connect() as connection:
        effects = connection.execute(
            sa.text(
                "SELECT count(*) FROM external_effects "
                "WHERE remote_idempotency_key = 'rollback-key'"
            )
        ).scalar_one()
        targets = connection.execute(
            sa.text("SELECT count(*) FROM effect_dispatch_targets")
        ).scalar_one()
    assert effects == 0
    assert targets == 0


# ---------------------------------------------------------------------------
# The request is only the binding: state/version mismatch refuses pre-write.
# ---------------------------------------------------------------------------


def test_state_binding_mismatch_refuses_before_any_step(engine) -> None:
    _feature_id, effect_id = seed_intent(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ExecutorRefusal) as excinfo:
        wake_effect(
            engine, adapter,
            effect_id=effect_id,
            expected_state="claimed",
            expected_version=1,
        )
    assert excinfo.value.code == "STATE_MISMATCH"
    assert adapter.calls == []
    assert effect_row_full(engine, effect_id) == ("intent_recorded", 1)


def test_version_binding_mismatch_refuses(engine) -> None:
    _feature_id, effect_id = seed_intent(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ExecutorRefusal) as excinfo:
        wake_effect(
            engine, adapter,
            effect_id=effect_id,
            expected_state="intent_recorded",
            expected_version=99,
        )
    assert excinfo.value.code == "VERSION_MISMATCH"
    assert adapter.calls == []


def test_wake_of_unknown_effect_id_refuses(engine) -> None:
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ExecutorRefusal) as excinfo:
        wake_effect(
            engine, adapter,
            effect_id="effect-absent",
            expected_state="intent_recorded",
            expected_version=1,
        )
    assert excinfo.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Crash windows.
# ---------------------------------------------------------------------------


def test_wake_after_claim_crash_resumes_the_composition(engine) -> None:
    """A crash between claim and dispatch leaves ``claimed``; a re-wake with
    the binding of that state finishes the remaining edges (nothing sent)."""
    _feature_id, effect_id = seed_intent(engine, effect_state="claimed", version=2)
    adapter = StubAdapter(CONFIRMED_PUSH)
    outcome = wake_effect(
        engine, adapter,
        effect_id=effect_id,
        expected_state="claimed",
        expected_version=2,
    )
    assert outcome.dispatch is not None
    assert outcome.dispatch.effect_state == "dispatch_started"
    assert adapter.calls == ["push"]


def test_wake_of_parked_dispatch_started_never_refires(engine) -> None:
    """The frozen rule: after ``dispatch_started`` the write may have landed;
    the executor refuses and the recovery path is reconciliation, not a
    re-send."""
    _feature_id, effect_id = seed_intent(engine)
    # Simulate the crash window: the dispatch CAS applied, the adapter's
    # answer was lost. The effect row was moved the way the composition does.
    from personal_agent_dal.github.adapter_controller import (
        _apply_step,
        _claim_guard_facts,
        _dispatch_guard_facts,
    )
    _apply_step(
        engine, command_type="claim_external_effect",
        evidence_source="external-effect-controller", effect_id=effect_id,
        expected_version=1, idempotency_key="crash:claim",
        facts=_claim_guard_facts(engine, effect_id, 0),
    )
    _apply_step(
        engine, command_type="record_effect_dispatch",
        evidence_source="effect-executor", effect_id=effect_id,
        expected_version=2, idempotency_key="crash:dispatch",
        facts=_dispatch_guard_facts(engine, effect_id, 0),
    )
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 3)
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ExecutorRefusal) as excinfo:
        wake_effect(
            engine, adapter,
            effect_id=effect_id,
            expected_state="dispatch_started",
            expected_version=3,
        )
    assert excinfo.value.code == "ILLEGAL_STATE"
    assert adapter.calls == [], "a parked effect must never be re-fired"
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 3)


# ---------------------------------------------------------------------------
# The reconciliation sweep: persistence-driven, read-only over the wire.
# ---------------------------------------------------------------------------


class ReadBackAdapter:
    """Read methods only; any write call is the duplicate-write defect."""

    def __init__(self, read: Any) -> None:
        self.read = read
        self.calls: list[str] = []

    def read_feature_branch(self, **_: Any) -> Any:
        self.calls.append("read_branch")
        return self.read

    def list_open_pull_requests(self, **_: Any) -> Any:
        self.calls.append("read_prs")
        return self.read

    def read_check_run(self, **_: Any) -> Any:
        self.calls.append("read_check")
        return self.read

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("push_", "create_pull", "write_check")):
            def _write(**_: Any) -> Any:
                raise AssertionError(f"sweep issued a write: {name}")
            return _write
        raise AttributeError(name)


def park_unknown(engine, *, effect_id: str = "effect-f5-1",
                 feature_id: str = "feature-f5-1") -> tuple[str, str]:
    """Feature parked by REC-UNKNOWN + its unknown push effect."""
    feature_id, effect_id = seed_intent(
        engine, effect_id=effect_id, feature_id=feature_id,
        feature_state="reconciliation_required", effect_state="unknown",
        version=5, remote_key=f"idem-{effect_id}",
    )
    with session_factory(engine)() as session, session.begin():
        from personal_agent_dal.storage.models import Feature

        feature = session.get(Feature, feature_id)
        assert feature is not None
        feature.reason_code = "EXTERNAL_RESULT_UNKNOWN"
        feature.reason_owner = "feature"
    return feature_id, effect_id


def test_unknown_effects_lists_only_unknown_rows(engine) -> None:
    park_unknown(engine)
    seed_intent(
        engine, effect_id="effect-f5-2", feature_id="feature-f5-2",
        feature_state="awaiting_merge", effect_state="intent_recorded",
        remote_key="idem-2",
    )
    listed = unknown_effects(engine)
    assert [f["effect_id"] for f in listed] == ["effect-f5-1"]


def test_sweep_reconciles_a_landed_push_read_only(engine) -> None:
    """The lost response actually landed: the sweep proves it with a GET."""
    park_unknown(engine)
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter)
    assert len(outcomes) == 1
    assert adapter.calls == ["read_branch"], "exactly one authoritative read"
    state, _version = effect_row_full(engine, "effect-f5-1")
    assert state == "reconciling", "a conclusive read leaves it for the human root"
    assert outcomes[0].reconciled is not None
    assert outcomes[0].reconciled.authoritative_result == "confirmed_completed"


def test_sweep_still_unknown_returns_effect_to_unknown(engine) -> None:
    """An inconclusive read runs STILL-UNKNOWN; the next sweep can retry."""
    park_unknown(engine)
    adapter = ReadBackAdapter(BranchReadBack(found=None, unknown=True))
    outcomes = run_unknown_sweep(engine, adapter)
    assert len(outcomes) == 1
    state, _version = effect_row_full(engine, "effect-f5-1")
    assert state == "unknown", "STILL-UNKNOWN releases the claim"
    # The second sweep re-enters (no deadlock) and still never writes.
    outcomes2 = run_unknown_sweep(engine, adapter)
    assert len(outcomes2) == 1
    assert effect_row_full(engine, "effect-f5-1")[0] == "unknown"
    assert adapter.calls == ["read_branch", "read_branch"]


def test_sweep_drifted_branch_is_unknown_not_absent(engine) -> None:
    """A mutable ref at another SHA proves nothing (F4-v2 semantics carry)."""
    park_unknown(engine)
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha="b" * 40))
    outcomes = run_unknown_sweep(engine, adapter)
    assert outcomes[0].reconciled is not None
    assert outcomes[0].reconciled.authoritative_result == "unknown"
    assert effect_row_full(engine, "effect-f5-1")[0] == "unknown"


def test_sweep_refuses_malformed_target_before_any_read(engine) -> None:
    park_unknown(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE effect_dispatch_targets SET payload_json = :payload "
                "WHERE effect_id = :effect_id"
            ).bindparams(payload="[]", effect_id="effect-f5-1")
        )
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter)
    assert outcomes[0].refusal is not None
    assert outcomes[0].refusal.code == "TARGET_INVALID"
    assert adapter.calls == []


def test_sweep_refuses_semantically_invalid_target_even_if_both_hashes_match(
    engine,
) -> None:
    park_unknown(engine)
    malformed = {"branch": BRANCH, "head_sha": "not-a-git-sha"}
    fingerprint = fingerprint_for("push_branch", malformed)
    from personal_agent_core.manifest import canonical_json

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE effect_dispatch_targets SET payload_json = :payload, "
                "target_fingerprint = :fingerprint WHERE effect_id = :effect_id"
            ).bindparams(
                payload=canonical_json(malformed),
                fingerprint=fingerprint,
                effect_id="effect-f5-1",
            )
        )
        connection.execute(
            sa.text(
                "UPDATE external_effects SET target_fingerprint = :fingerprint "
                "WHERE effect_id = :effect_id"
            ).bindparams(fingerprint=fingerprint, effect_id="effect-f5-1")
        )
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter)
    assert outcomes[0].refusal is not None
    assert outcomes[0].refusal.code == "TARGET_INVALID"
    assert adapter.calls == []


def test_expired_dispatch_marker_enters_read_only_reconciliation(engine) -> None:
    """Crash after dispatch CAS: expiry moves to unknown, then GET-only readback."""
    feature_id, effect_id = seed_intent(engine)
    from personal_agent_dal.github.adapter_controller import (
        _apply_step,
        _claim_guard_facts,
        _dispatch_guard_facts,
    )

    _apply_step(
        engine,
        command_type="claim_external_effect",
        evidence_source="external-effect-controller",
        effect_id=effect_id,
        expected_version=1,
        idempotency_key="crash-expired:claim",
        facts=_claim_guard_facts(engine, effect_id, 0),
    )
    _apply_step(
        engine,
        command_type="record_effect_dispatch",
        evidence_source="effect-executor",
        effect_id=effect_id,
        expected_version=2,
        idempotency_key="crash-expired:dispatch",
        facts=_dispatch_guard_facts(engine, effect_id, 0),
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :expired "
                "WHERE effect_id = :effect_id"
            ).bindparams(
                expired="2020-01-01T00:00:00.000000Z",
                effect_id=effect_id,
            )
        )
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter, now_epoch=1_700_000_000)
    assert [outcome.effect_id for outcome in outcomes] == [effect_id]
    assert adapter.calls == ["read_branch"]
    assert effect_row_full(engine, effect_id)[0] == "reconciling"
    with engine.connect() as connection:
        feature = connection.execute(
            sa.text("SELECT state FROM features WHERE feature_id = :feature_id")
            .bindparams(feature_id=feature_id)
        ).scalar_one()
    assert feature == "reconciliation_required"


# ---------------------------------------------------------------------------
# The confirm discriminator (round-3 finding R3-1): a confirmed park must
# never be swept, a crash window with no confirm evidence still must be.
# ---------------------------------------------------------------------------


def _dispatch_past_expiry(engine, effect_id: str, *, key: str) -> None:
    """Drive claim → dispatch, then stamp the marker's expiry far in the past.

    Exactly the crash-window shape the recovery sweep owns — the same
    seeding ``test_expired_dispatch_marker_enters_read_only_reconciliation``
    pins, factored out so each discriminator test starts from it.
    """
    from personal_agent_dal.github.adapter_controller import (
        _apply_step,
        _claim_guard_facts,
        _dispatch_guard_facts,
    )

    _apply_step(
        engine,
        command_type="claim_external_effect",
        evidence_source="external-effect-controller",
        effect_id=effect_id,
        expected_version=1,
        idempotency_key=f"{key}:claim",
        facts=_claim_guard_facts(engine, effect_id, 0),
    )
    _apply_step(
        engine,
        command_type="record_effect_dispatch",
        evidence_source="effect-executor",
        effect_id=effect_id,
        expected_version=2,
        idempotency_key=f"{key}:dispatch",
        facts=_dispatch_guard_facts(engine, effect_id, 0),
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :expired "
                "WHERE effect_id = :effect_id"
            ).bindparams(
                expired="2020-01-01T00:00:00.000000Z",
                effect_id=effect_id,
            )
        )


def test_sweep_never_takes_a_confirmed_expired_park(engine) -> None:
    """A confirmed write parked past its marker expiry stays parked (R3-1).

    The composition confirmed the push, so the executor wrote its confirm
    receipt; fifteen minutes later the expiry sweep runs and must leave the
    effect and its owner exactly where confirmation parked them.
    """
    feature_id, effect_id = seed_intent(engine, remote_key="idem-f5-1")
    adapter = StubAdapter(CONFIRMED_PUSH)
    outcome = dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch",
        idempotency_key="idem-f5-1", payload=push_payload(),
        feature_id=feature_id,
    )
    assert outcome.effect_state == "dispatch_started"
    assert adapter.calls == ["push"]
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :expired "
                "WHERE effect_id = :effect_id"
            ).bindparams(
                expired="2020-01-01T00:00:00.000000Z", effect_id=effect_id
            )
        )
    read_only = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, read_only, now_epoch=1_700_000_000)
    assert outcomes == [], "a confirmed park is nobody's recovery work"
    assert read_only.calls == [], "no read-back may issue for a confirmed park"
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 3)
    with engine.connect() as connection:
        feature = connection.execute(
            sa.text("SELECT state FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).scalar_one()
    assert feature == "awaiting_merge"


def test_sweep_takes_a_crash_window_even_when_the_write_really_landed(
    engine,
) -> None:
    """Crash between the confirmed read-back and the receipt: swept, not kept.

    No receipt means the sweep cannot distinguish this park from any other
    crash window — and must not. Fail-closed direction: the effect enters
    ``unknown`` and the read-only reconciliation proves what landed, so the
    outcome converges without ever re-firing the write.
    """
    feature_id, effect_id = seed_intent(engine)
    _dispatch_past_expiry(engine, effect_id, key="crash-no-receipt")
    read_only = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, read_only, now_epoch=1_700_000_000)
    assert [o.effect_id for o in outcomes] == [effect_id]
    assert read_only.calls == ["read_branch"]
    assert effect_row_full(engine, effect_id)[0] == "reconciling"
    with engine.connect() as connection:
        feature = connection.execute(
            sa.text("SELECT state FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).scalar_one()
    assert feature == "reconciliation_required"


def test_stale_confirm_receipt_suppresses_nothing(engine) -> None:
    """A receipt whose fingerprint no longer matches its effect is dead.

    The sweep's trust boundary is the effect row's own binding: a rearm that
    rewrote the target leaves the old receipt naming a target that no longer
    exists, so the park is recovered as a crash window instead of being
    protected by a stale receipt.
    """
    feature_id, effect_id = seed_intent(engine, remote_key="idem-f5-1")
    adapter = StubAdapter(CONFIRMED_PUSH)
    outcome = dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch",
        idempotency_key="idem-f5-1", payload=push_payload(),
        feature_id=feature_id,
    )
    assert outcome.effect_state == "dispatch_started"
    # A drift-tamper on the binding after confirmation: the receipt's
    # fingerprint now disagrees with the row it was written against.
    # The park keeps its composition-stamped marker; only the expiry needs
    # backdating to reach the sweep's window.
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET target_fingerprint = :drifted, "
                "claim_expires_at = :expired WHERE effect_id = :effect_id"
            ).bindparams(
                drifted="f" * 64,
                expired="2020-01-01T00:00:00.000000Z",
                effect_id=effect_id,
            )
        )
    read_only = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, read_only, now_epoch=1_700_000_000)
    # The stale receipt suppressed nothing: the park entered the recovery
    # edge like any crash window. The drifted binding then fails the
    # sweep's own target re-validation, so reconciliation never starts and
    # the effect waits in ``unknown`` — visibly, for the operator.
    assert [o.effect_id for o in outcomes] == [effect_id]
    assert outcomes[0].refusal is not None
    assert outcomes[0].refusal.code == "FINGERPRINT_MISMATCH"
    assert effect_row_full(engine, effect_id)[0] == "unknown"


def test_confirm_receipt_refuses_for_a_non_parked_effect(engine) -> None:
    """The receipt may only describe a park that still exists."""
    _feature_id, effect_id = seed_intent(engine)
    from personal_agent_dal.github.executor import (
        record_composition_confirm_receipt,
    )

    with pytest.raises(ExecutorRefusal) as excinfo:
        record_composition_confirm_receipt(
            engine, effect_id=effect_id, action="push_branch",
            composition_key="x:confirmed",
        )
    assert excinfo.value.code == "ILLEGAL_STATE"
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT COUNT(*) FROM effect_confirm_receipts")
        ).scalar_one()
    assert rows == 0


def test_expired_park_without_a_target_still_enters_recovery(engine) -> None:
    """No target record: the park must visibly surface, not silently rot.

    The old query joined on ``effect_dispatch_targets`` and dropped exactly
    these rows (round-3 finding R3-2); now they enter ``unknown`` and the
    sweep reports the ``TARGET_MISSING`` refusal — and the pass continues
    for the other effects.
    """
    feature_id, effect_id = seed_intent(engine)
    healthy_id = "effect-f5-9"
    seed_intent(
        engine, effect_id=healthy_id, feature_id="feature-f5-9",
        remote_key="idem-f5-9",
    )
    _dispatch_past_expiry(engine, effect_id, key="orphan-target")
    _dispatch_past_expiry(engine, healthy_id, key="healthy-target")
    with session_factory(engine)() as session, session.begin():
        session.delete(session.get(EffectDispatchTarget, effect_id))
    read_only = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, read_only, now_epoch=1_700_000_000)
    by_effect = {o.effect_id: o for o in outcomes}
    assert by_effect[effect_id].refusal is not None
    assert by_effect[effect_id].refusal.code == "TARGET_MISSING"
    assert by_effect[healthy_id].reconciled is not None, (
        "one broken effect must not abort the pass"
    )
    assert effect_row_full(engine, effect_id)[0] == "unknown"


def test_reconciliation_keys_do_not_collide_across_effects(engine) -> None:
    """Receipt keys are global; effect identity must be part of the episode key."""
    park_unknown(engine, effect_id="effect-key-1", feature_id="feature-key-1")
    park_unknown(engine, effect_id="effect-key-2", feature_id="feature-key-2")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET remote_idempotency_key = :key "
                "WHERE effect_id IN ('effect-key-1', 'effect-key-2')"
            ).bindparams(key="shared-remote-key")
        )
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter)
    assert {outcome.effect_id for outcome in outcomes} == {
        "effect-key-1",
        "effect-key-2",
    }
    assert adapter.calls == ["read_branch", "read_branch"]


def test_sweep_reports_effects_without_a_target_and_continues(engine) -> None:
    park_unknown(engine, effect_id="effect-f5-1", feature_id="feature-f5-1")
    park_unknown(engine, effect_id="effect-f5-3", feature_id="feature-f5-3")
    with session_factory(engine)() as session, session.begin():
        session.get(EffectDispatchTarget, "effect-f5-3")
        session.delete(session.get(EffectDispatchTarget, "effect-f5-3"))
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter)
    by_effect = {o.effect_id: o for o in outcomes}
    assert by_effect["effect-f5-3"].refusal is not None
    assert by_effect["effect-f5-3"].refusal.code == "TARGET_MISSING"
    assert by_effect["effect-f5-1"].reconciled is not None, (
        "one broken effect must not abort the pass"
    )


def test_sweep_wake_of_unknown_runs_reconciliation_not_dispatch(engine) -> None:
    """Waking an unknown effect reconciles it — the one legal operator action
    on a parked unknown; a re-dispatch is what must never happen."""
    park_unknown(engine)
    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcome = wake_effect(
        engine, adapter,
        effect_id="effect-f5-1",
        expected_state="unknown",
        expected_version=5,
    )
    assert outcome.dispatch is None
    assert outcome.reconciled is not None
    assert outcome.reconciled.authoritative_result == "confirmed_completed"
    assert effect_row_full(engine, "effect-f5-1")[0] == "reconciling"


def test_wake_request_validation_is_closed(engine) -> None:
    """Empty/None bindings are refused; the surface cannot be tricked."""
    _feature_id, effect_id = seed_intent(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    for kwargs in (
        {"effect_id": "", "expected_state": "intent_recorded", "expected_version": 1},
        {"effect_id": effect_id, "expected_state": "", "expected_version": 1},
        {"effect_id": effect_id, "expected_state": "intent_recorded", "expected_version": 0},
        {"effect_id": effect_id, "expected_state": "intent_recorded", "expected_version": None},
    ):
        with pytest.raises(ExecutorRefusal) as excinfo:
            wake_effect(engine, adapter, **kwargs)
        assert excinfo.value.code == "INVALID_ARGUMENT"
    assert adapter.calls == []
