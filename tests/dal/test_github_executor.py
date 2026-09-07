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
import time
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
from personal_agent_dal.github import executor as executor_module
from personal_agent_dal.github.executor import (
    ExecutorRefusal,
    record_composition_confirm_receipt,
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


# --- F2 (2026-09-07 review): expired reconciler claims must auto-recover ----


def _seed_stale_reconciling_claim(engine, *, same_day: bool) -> str:
    """A reconciling claim whose expiry has passed — by seconds, or by days.

    The sweep's expiry comparison binds against the RFC 3339 text column;
    round-2 review finding 1 proved a naive datetime bind (space separator,
    no Z) never matches a same-day expiry, so only cross-day staleness was
    ever reclaimed. `same_day=True` seeds the expiry at a genuinely past
    instant on the sweep's own calendar day (the sweep reads the real clock)
    — the shape the first round's tests missed by using a 2020 timestamp.
    """
    from personal_agent_dal.github.reconciliation import start_effect_reconciliation
    from personal_agent_core.timeutil import to_rfc3339, utc_now

    feature_id, effect_id = park_unknown(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-stale",
        feature_id=feature_id,
    )
    if same_day:
        # A real past instant on today's calendar day: the claim was stamped
        # 15 minutes into the future, so "now minus 60s" is past the expiry
        # wall-clock AND on the same day — the exact shape a naive bind misses.
        expired_text = to_rfc3339(utc_now() - timedelta(seconds=60))
    else:
        expired_text = "2020-01-01T00:00:00Z"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :past "
                "WHERE effect_id = :e"
            ).bindparams(past=expired_text, e=effect_id)
        )
    return effect_id


def test_same_day_expired_reconciling_claim_is_reclaimed(engine) -> None:
    """R2-1: an expiry seconds past, on the claim's own day, must reclaim.

    The naive datetime bind rendered '2026-09-07 14:00:00-00:00' while the
    column stores '2026-09-07T14:00:00Z'; as text 'T' > ' ', so the same-day
    comparison never matched and the claim only looked expired the next
    calendar day. Round-1's tests used a 2020 timestamp and missed it.
    """
    effect_id = _seed_stale_reconciling_claim(engine, same_day=True)

    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    run_unknown_sweep(engine, adapter)

    # The reclaim ran (the sweep's normal pass then re-reconciled read-only):
    # a read was issued, which only happens for a row that entered `unknown`.
    assert adapter.calls == ["read_branch"]
    assert effect_row_full(engine, effect_id)[0] == "reconciling"


def test_same_day_expired_dispatch_park_is_recovered(engine) -> None:
    """R2-1 (dispatch side): the pre-existing `_recover_expired_dispatches`
    binds the same naive datetime against the same RFC 3339 column — the same
    same-day blindness, older than the reconciler half. An expired
    `dispatch_started` crash window must move to `unknown` the same day."""
    feature_id, effect_id = seed_intent(engine)
    from personal_agent_dal.github.adapter_controller import (
        _apply_step,
        _claim_guard_facts,
        _dispatch_guard_facts,
    )
    _apply_step(
        engine, command_type="claim_external_effect",
        evidence_source="external-effect-controller", effect_id=effect_id,
        expected_version=1, idempotency_key="r2-1:claim",
        facts=_claim_guard_facts(engine, effect_id, 0),
    )
    _apply_step(
        engine, command_type="record_effect_dispatch",
        evidence_source="effect-executor", effect_id=effect_id,
        expected_version=2, idempotency_key="r2-1:dispatch",
        facts=_dispatch_guard_facts(engine, effect_id, 0),
    )
    # The dispatch marker stamps claim_expires_at 15 minutes ahead; set it to
    # a real past instant on the sweep's own calendar day (now minus 60s).
    from personal_agent_core.timeutil import to_rfc3339, utc_now

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :past "
                "WHERE effect_id = :e"
            ).bindparams(
                past=to_rfc3339(utc_now() - timedelta(seconds=60)), e=effect_id
            )
        )

    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    run_unknown_sweep(engine, adapter)

    state = effect_row_full(engine, effect_id)[0]
    assert state in ("unknown", "reconciling"), (
        f"the same-day expired park left dispatch_started: {state}"
    )
    assert adapter.calls == ["read_branch"], "the recovery ran the read-back"


def test_reconciler_claim_stamps_expiry(engine) -> None:
    """The claim carries an expiry timestamp the sweep can reclaim on.

    The old `_w_reconciler_claim` stamped executor_id and epoch but no
    `claim_expires_at` — the dispatch-side claims stamp 15 minutes; the
    reconciler claim had nothing a recovery sweep could even look at.
    """
    from personal_agent_dal.github.reconciliation import start_effect_reconciliation

    feature_id, effect_id = park_unknown(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-f2-1",
        feature_id=feature_id,
    )
    with engine.connect() as connection:
        claim_expires_at = connection.execute(
            sa.text(
                "SELECT claim_expires_at FROM external_effects "
                "WHERE effect_id = :e"
            ).bindparams(e=effect_id)
        ).scalar_one()
    assert claim_expires_at is not None, "the claim must stamp an expiry"


def test_expired_reconciling_claim_is_reclaimed_to_unknown(engine) -> None:
    """F2: a process that died after taking the claim is auto-recovered.

    The old sweep selected only `unknown` rows, so a crashed reconciler left
    the effect in `reconciling` forever — invisible to the sweep, invisible to
    the operator listing, recoverable only by a manual wake with a known
    effect id. The reclaim drives the frozen STILL-UNKNOWN edge back to
    `unknown`, and the same sweep's normal pass then re-reconciles it
    read-only — so after one sweep the effect is back in a *fresh* claim with
    a *new* expiry, having issued exactly one authoritative read. That read
    is the proof the reclaim happened: the old code issued none.
    """
    from personal_agent_dal.github.reconciliation import start_effect_reconciliation

    feature_id, effect_id = park_unknown(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-f2-2",
        feature_id=feature_id,
    )
    version_before = effect_row_full(engine, effect_id)[1]
    # Simulate the crash: the claim is committed, the process died before any
    # read-back or state write; its expiry has long passed.
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :past "
                "WHERE effect_id = :e"
            ).bindparams(past="2020-01-01T00:00:00Z", e=effect_id)
        )

    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, adapter)

    # The reclaim (STILL-UNKNOWN) plus the re-reconcile (START + conclusive
    # read leaves reconciling) both moved the row: version advanced by 2 and
    # a read was issued. The old code: no reclaim, no read, version frozen.
    state, version_after = effect_row_full(engine, effect_id)
    assert adapter.calls == ["read_branch"], (
        "the only way a formerly-reconciling row receives a read is through "
        "the reclaim to unknown and the sweep's normal pass"
    )
    assert version_after == version_before + 2, (
        f"reclaim + re-claim must each bump the version: {version_before} "
        f"-> {version_after}"
    )
    assert state == "reconciling", "a fresh claim, not the stale one"

    # The fresh claim carries a new expiry and is left alone by the next pass
    # (live claim, no read issued for it), still never a write.
    run_unknown_sweep(engine, adapter)
    assert adapter.calls == ["read_branch"]
    assert effect_row_full(engine, effect_id)[0] == "reconciling"


def test_live_reconciling_claim_is_not_reclaimed(engine) -> None:
    """A claim whose expiry has not passed is a live reconciler, not a crash."""
    from personal_agent_dal.github.reconciliation import start_effect_reconciliation

    feature_id, effect_id = park_unknown(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-f2-3",
        feature_id=feature_id,
    )

    adapter = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    run_unknown_sweep(engine, adapter)

    state = effect_row_full(engine, effect_id)[0]
    assert state == "reconciling", "the live claim is left alone"
    assert adapter.calls == [], "no read was issued for a live claim"


def test_reclaim_tolerates_cas_loss(engine) -> None:
    """A second reclaim against the same pre-read row refuses silently."""
    from personal_agent_dal.github.executor import _recover_expired_reconciling
    from personal_agent_dal.github.reconciliation import start_effect_reconciliation

    feature_id, effect_id = park_unknown(engine)
    start_effect_reconciliation(
        engine, effect_id=effect_id, idempotency_key="recon-f2-4",
        feature_id=feature_id,
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :past "
                "WHERE effect_id = :e"
            ).bindparams(past="2020-01-01T00:00:00Z", e=effect_id)
        )

    # First pass reclaims; the second sees the row already moved and the
    # version-derived idempotency key replays — neither may raise.
    _recover_expired_reconciling(engine, limit=10, now_epoch=int(time.time()))
    _recover_expired_reconciling(engine, limit=10, now_epoch=int(time.time()))
    assert effect_row_full(engine, effect_id)[0] == "unknown"


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
    # Version 4: the receipt took the concurrency token (R4-1) on top of
    # claim → dispatch (1 → 3).
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 4)
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


def test_expired_recovery_case_owned_park_enters_recovery(engine) -> None:
    """A ``recovery_case``-owned expired park is swept too (R4-2).

    The registry lets recovery-case flows own external effects, so the
    old ``owner_aggregate_type = 'feature'`` filter let such a park rot in
    ``dispatch_started`` forever. The effect-level edge does not depend on
    the owner — the park enters ``unknown`` here without a feature stop
    (no feature id is named; the case's own flow owns what follows), and a
    healthy feature-owned effect in the same pass proves the pass
    continues.
    """
    now = utc_now()
    case_id = "case-f5-7"
    with session_factory(engine)() as session, session.begin():
        from personal_agent_dal.storage.machine_models import RecoveryCase

        # The feature behind the case must exist: the recovery edge's
        # decision_create resolves the effect's owning feature through the
        # case (engine `_owner_feature_id`, R4-2) and parks a decision on it.
        session.add(feature_row(
            feature_id="feature-f5-8", version=1, state="verifying", now=now
        ))
        session.add(RecoveryCase(
            recovery_case_id=case_id, feature_id="feature-f5-8",
            version=1, state="verifying", reason_code="RECOVERY_EFFECT_UNKNOWN",
            execution_epoch=1, created_at=now, updated_at=now,
        ))
    with session_factory(engine)() as session, session.begin():
        from personal_agent_dal.storage.machine_models import ExternalEffect

        effect = external_effect_row(
            effect_id="effect-f5-7", owner_id=case_id, version=3,
            state="dispatch_started", owner_type="recovery_case", now=now,
        )
        effect.remote_idempotency_key = "idem-f5-7"
        session.add(effect)
        session.add(EffectDispatchTarget(
            effect_id="effect-f5-7", action="push_branch",
            payload_json=json.dumps(push_payload()),
            target_fingerprint=fingerprint_for("push_branch", push_payload()),
            recorded_at=now,
        ))
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE external_effects SET claim_expires_at = :expired "
                "WHERE effect_id = 'effect-f5-7'"
            ).bindparams(expired="2020-01-01T00:00:00.000000Z")
        )
    # A healthy feature-owned crash window in the same pass.
    _feature_id, healthy_id = seed_intent(
        engine, effect_id="effect-f5-9", feature_id="feature-f5-9",
        remote_key="idem-f5-9",
    )
    _dispatch_past_expiry(engine, healthy_id, key="healthy-rc")
    read_only = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
    outcomes = run_unknown_sweep(engine, read_only, now_epoch=1_700_000_000)
    by_effect = {o.effect_id: o for o in outcomes}
    assert "effect-f5-7" in by_effect, "the case-owned park must be swept"
    # The recovery sweep's read-back composes feature reconciliation only
    # (``_effect_facts`` refuses a non-feature owner), so the case-owned
    # park is reported as a visible TARGET_INVALID refusal instead of
    # silently rotting — the case's own flow owns what follows.
    assert by_effect["effect-f5-7"].refusal is not None
    assert by_effect["effect-f5-7"].refusal.code == "TARGET_INVALID"
    assert effect_row_full(engine, "effect-f5-7")[0] == "unknown"
    assert by_effect[healthy_id].reconciled is not None, (
        "one case-owned park must not abort the pass"
    )


def test_receipt_committing_mid_sweep_blocks_the_recovery_cas(engine) -> None:
    """Forward interleave (R4-1): a receipt committing after the sweep's
    discriminator read must leave the in-flight recovery CAS refused.

    The sweep's batch query reads ``version`` together with the receipt
    ``NOT EXISTS`` — one snapshot. If the confirm receipt commits between
    that read and the ``record_effect_unknown`` compare-and-swap, the version
    it read is stale: the receipt writer moved it as the concurrency token.
    The CAS must refuse, the park must stay parked with its receipt, and no
    read-back may fire — the deterministic re-run of the exact interleave
    the reviewer's forward race names.
    """
    feature_id, effect_id = seed_intent(engine, remote_key="idem-f5-1")
    # Crash-window shape: parked, expired, *no* receipt yet — the state the
    # sweep's discriminator read sees before the interleave commits one.
    _dispatch_past_expiry(engine, effect_id, key="forward-race")

    real_recover = executor_module._recover_expired_dispatches
    interleaved: list[str] = []

    def recover_then_commit_receipt(engine_, *, limit: int, now_epoch: int) -> None:
        """Run the real sweep decision up to its CAS, commit a receipt, resume.

        The sweep read its rows (parked, no receipt, version 3) before this
        hook fires; committing a *matching* receipt here reproduces the
        forward interleave with the production writer — never a hand-made
        UPDATE — before the sweep's CAS is allowed to proceed.
        """
        blocked = {"armed": True}

        def intercept_cas(engine__, **kwargs: Any) -> Any:
            if blocked.pop("armed", False):
                record_composition_confirm_receipt(
                    engine_, effect_id=effect_id, action="push_branch",
                    composition_key=f"{effect_id}:confirmed",
                )
                interleaved.append("receipt")
            return real_cas(engine__, **kwargs)

        real_cas = executor_module._apply_step
        executor_module._apply_step = intercept_cas  # type: ignore[assignment]
        try:
            real_recover(engine_, limit=limit, now_epoch=now_epoch)
        finally:
            executor_module._apply_step = real_cas  # type: ignore[assignment]

    executor_module._recover_expired_dispatches = (  # type: ignore[assignment]
        recover_then_commit_receipt
    )
    try:
        read_only = ReadBackAdapter(BranchReadBack(found=True, head_sha=HEAD))
        outcomes = run_unknown_sweep(engine, read_only, now_epoch=1_700_000_000)
    finally:
        executor_module._recover_expired_dispatches = real_recover  # type: ignore[assignment]

    assert interleaved == ["receipt"], "the forward interleave must have run"
    assert outcomes == [], "a confirmed park must not be swept mid-race"
    assert read_only.calls == [], "no read-back may issue for a confirmed park"
    # Parked with the receipt's version token (3 → 4); the refused CAS —
    # armed with the pre-receipt version 3 — consumed nothing.
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 4)
    with engine.connect() as connection:
        feature = connection.execute(
            sa.text("SELECT state FROM features WHERE feature_id = :f")
            .bindparams(f=feature_id)
        ).scalar_one()
    assert feature == "awaiting_merge"


def test_sweep_winning_before_receipt_keeps_the_receipt_out(engine) -> None:
    """Reverse interleave (R4-1): the sweep moves the park to ``unknown``
    before the receipt's parked-state check — the receipt must refuse.

    The parked-state check lives inside the receipt's write transaction on a
    fresh read, so a park that has already left ``dispatch_started`` cannot
    gain a confirm receipt, and no ``unknown``-state row ever carries one.
    The interleaved ``record_effect_unknown`` runs through the real frozen
    edge, not a hand-made UPDATE.
    """
    feature_id, effect_id = seed_intent(engine, remote_key="idem-f5-1")
    _dispatch_past_expiry(engine, effect_id, key="reverse-race")

    real_receipt = record_composition_confirm_receipt
    swept: list[str] = []

    def receipt_after_sweep(engine_, *, effect_id: str, **kwargs: Any) -> None:
        """Commit the sweep's unknown edge, then run the real receipt boundary.

        The receipt's outer fingerprint read happens before this hook fires —
        the row was parked then — but the parked-state check it relies on
        sits inside its write transaction, so the sweep's committed
        ``unknown`` must surface as a refusal there.
        """
        from personal_agent_dal.github.adapter_controller import (
            _apply_step,
            _claim_guard_facts,
            _dispatch_guard_facts,
        )

        _apply_step(
            engine_, command_type="record_effect_unknown",
            evidence_source="external-effect-controller", effect_id=effect_id,
            expected_version=3,
            idempotency_key=f"recover-dispatch:{effect_id}:v3:unknown",
            facts={"executor.failure_shape": "response_lost"},
        )
        swept.append(effect_id)
        real_receipt(engine_, effect_id=effect_id, **kwargs)

    with pytest.raises(ExecutorRefusal) as excinfo:
        receipt_after_sweep(
            engine, effect_id=effect_id, action="push_branch",
            composition_key=f"{effect_id}:confirmed",
        )
    assert excinfo.value.code == "ILLEGAL_STATE"
    assert swept == [effect_id]
    # The recovery edge committed; the receipt did not.
    assert effect_row_full(engine, effect_id) == ("unknown", 4)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT COUNT(*) FROM effect_confirm_receipts")
        ).scalar_one()
    assert rows == 0


def test_receipt_mid_transaction_sees_the_sweep_via_snapshot_retry(
    engine, monkeypatch
) -> None:
    """Mid-transaction reverse interleave (R4-1, round-5 finding R4-N1).

    The previous reverse test committed the sweep's ``unknown`` *before*
    the receipt boundary started, so the receipt's write transaction never
    held a stale snapshot: the snapshot-conflict retry inside
    ``run_write_transaction`` was docstring-claimed but pinned by no test.
    Here the real receipt boundary opens its write transaction and
    completes its first state read while the row is still parked; only
    then does the real frozen sweep edge commit ``unknown`` on its own
    session. SQLite refuses the stale snapshot's write, the unit re-runs
    from a fresh read (two state reads), and reaches the same
    ``ILLEGAL_STATE`` refusal — no state that left ``dispatch_started``
    ever gains a receipt.
    """
    feature_id, effect_id = seed_intent(engine, remote_key="idem-f5-1")
    _dispatch_past_expiry(engine, effect_id, key="mid-tx-race")

    from personal_agent_dal.github.adapter_controller import _apply_step
    from personal_agent_dal.storage import engine as storage_engine_module

    real_factory = storage_engine_module.session_factory
    observed: dict[str, Any] = {"state_reads": 0, "sweep_committed": False}

    def hooked_factory(factory_engine: Any) -> Any:
        """Session maker whose execute commits the real sweep edge between
        the receipt's first in-tx state read and its write attempt — the
        exact production interleave, through production code only."""
        maker = real_factory(factory_engine)

        def make_session() -> Any:
            session = maker()
            original_execute = session.execute

            def hooked_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = original_execute(statement, *args, **kwargs)
                if "state, version, target_fingerprint" in str(statement):
                    observed["state_reads"] += 1
                    if observed["state_reads"] == 1:
                        # The receipt's transaction has now read the
                        # still-parked row; commit the real frozen sweep
                        # edge from its own session — the interleave.
                        _apply_step(
                            engine,
                            command_type="record_effect_unknown",
                            evidence_source="external-effect-controller",
                            effect_id=effect_id,
                            expected_version=3,
                            idempotency_key=(
                                f"recover-dispatch:{effect_id}:v3:unknown"
                            ),
                            facts={
                                "executor.failure_shape": "response_lost"
                            },
                        )
                        observed["sweep_committed"] = True
                return result

            session.execute = hooked_execute  # type: ignore[method-assign]
            return session

        return make_session

    # The receipt boundary imports session_factory inside the function, so
    # patching the storage.engine module attribute reaches exactly its write
    # transaction; the machine engine's module-level binding stays real.
    monkeypatch.setattr(
        "personal_agent_dal.storage.engine.session_factory", hooked_factory
    )

    with pytest.raises(ExecutorRefusal) as excinfo:
        record_composition_confirm_receipt(
            engine, effect_id=effect_id, action="push_branch",
            composition_key=f"{effect_id}:confirmed",
        )
    assert excinfo.value.code == "ILLEGAL_STATE"
    assert observed["sweep_committed"] is True
    assert observed["state_reads"] == 2, (
        "the refused stale snapshot must re-run the unit from a fresh read"
    )
    # The recovery edge committed; the receipt did not.
    assert effect_row_full(engine, effect_id) == ("unknown", 4)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT COUNT(*) FROM effect_confirm_receipts")
        ).scalar_one()
    assert rows == 0


def test_same_receipt_replay_does_not_move_the_concurrency_token(engine) -> None:
    """Replaying an identical confirm receipt must not bump the version.

    The version bump is the receipt's concurrency token (R4-1); a replay
    that bumped again would let a duplicate confirm pre-move the token
    without any new confirmation evidence. The identical replay must leave
    the row exactly where the first confirm parked it (round-5 finding
    R4-N1: this property had a live probe but no in-repo regression).
    """
    feature_id, effect_id = seed_intent(engine, remote_key="idem-f5-1")
    _dispatch_past_expiry(engine, effect_id, key="replay-token")

    record_composition_confirm_receipt(
        engine, effect_id=effect_id, action="push_branch",
        composition_key=f"{effect_id}:confirmed",
    )
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 4), (
        "the first confirm takes the token (claim+dispatch 1->3, receipt 3->4)"
    )

    record_composition_confirm_receipt(
        engine, effect_id=effect_id, action="push_branch",
        composition_key=f"{effect_id}:confirmed",
    )
    assert effect_row_full(engine, effect_id) == ("dispatch_started", 4), (
        "an identical replay must not move the concurrency token again"
    )
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT COUNT(*) FROM effect_confirm_receipts")
        ).scalar_one()
    assert rows == 1, "the replay must not write a second receipt row"


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
