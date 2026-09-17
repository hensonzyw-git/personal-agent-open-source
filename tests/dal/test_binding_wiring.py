"""DAL-011: hash-binding checks are wired into the consumption path.

`test_binding.py` proves the validators refuse a divergent binding in isolation.
This module proves the same mechanism fires where the contract puts it — inside
`_w_decision_resolve` and `_w_approval_consume`, the two write-set appliers the
`approve_plan` transition runs when a human consumes an approval.

These are behaviour tests over the real engine, not frozen-fixture replays: the
frozen `STATEHASH`/`ARTIFACTHASH` fixtures exercise the validator directly with
a `consume_decision`/`consume_artifact_approval` shape, so the wiring — that a
decision/approval row's protected `state_sha256`/`artifact_sha256` is compared
against the binding the device submitted — is its own seam and gets its own
tests. A drift must refuse with zero writes; a matching binding must not be the
thing that refuses the transition.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from sqlalchemy import text

from personal_agent_dal.machine.engine import (
    ReceiptCodes,
    TransitionCommand,
    apply_transition,
)
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_dal.machine.registry import jcs_sha256
from personal_agent_dal.machine.binding import ArtifactReadback, build_state_binding
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.factories import approval_row, decision_row, feature_row


ARTIFACT_BINDING = {
    "schema_version": "dal.artifact-binding/1.0",
    "artifact_schema_version": "dal.plan-artifact/1.0",
    "media_type": "text/markdown",
    "feature_id": "feat-wiring",
    "artifact_kind": "plan",
    "artifact_version": 1,
    "base_sha": "0" * 40,
    "body_canonicalization": "utf8-lf-nfc/1.0",
    "body_sha256": hashlib.sha256(b"a" * 128).hexdigest(),
    "body_size": 128,
    "acceptance_sha256": None,
    "allowed_paths_sha256": None,
}

FEATURE_ID = "feat-wiring"
APPROVAL_ID = "approval-wiring"
DECISION_ID = "decision-wiring"


def _approve_command(**parameters: object) -> TransitionCommand:
    """One `approve_plan` command, bound to the seeded feature/approval."""
    return TransitionCommand(
        aggregate_type="feature",
        aggregate_id=FEATURE_ID,
        command_type="approve_plan",
        command_parameters={
            "target_state": "approved",
            "approval_id": APPROVAL_ID,
            "decision_id": DECISION_ID,
            "submitted_decision_version": 1,
            **parameters,
        },
        actor_type="human",
        evidence_source_types=("registered-device",),
        evidence_schema_versions=("dal.evidence.approval/1.0",),
        decision_action="approve_plan",
        reason_code=None,
        expected_version=7,
        idempotency_key="idem-wiring",
    )


def _seed(
    database: Path,
    *,
    decision_state_sha256: str | None = None,
    approval_state_sha256: str | None = None,
    approval_artifact_sha256: str | None = None,
    feature_artifact_sha256: str | None = None,
    feature_base_sha: str = "0" * 40,
    seed_approval: bool = True,
) -> None:
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        feature = feature_row(
            feature_id=FEATURE_ID, version=7, state="awaiting_plan_review"
        )
        feature.base_sha = feature_base_sha
        feature.artifact_sha256 = feature_artifact_sha256
        session.add(feature)
        dec = decision_row(feature_id=FEATURE_ID, decision_id=DECISION_ID)
        dec.state_sha256 = decision_state_sha256
        session.add(dec)
        if seed_approval:
            ap = approval_row(feature_id=FEATURE_ID, approval_id=APPROVAL_ID)
            ap.decision_id = DECISION_ID
            ap.decision_version = 1
            ap.expected_feature_version = 7
            ap.expected_state = "awaiting_plan_review"
            ap.state_sha256 = approval_state_sha256
            ap.artifact_sha256 = approval_artifact_sha256
            session.add(ap)
    engine.dispose()


def _state_binding(*, base_sha: str = "0" * 40, artifact_sha256: str | None = None):
    feature = feature_row(
        feature_id=FEATURE_ID, version=7, state="awaiting_plan_review"
    )
    feature.base_sha = base_sha
    feature.artifact_sha256 = artifact_sha256
    return build_state_binding(feature)


def _feature_state(database: Path) -> tuple[str, int]:
    engine = create_database_engine(database)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT state, version FROM features WHERE feature_id = :id")
                .bindparams(id=FEATURE_ID)
            ).first()
        return (row[0], row[1])
    finally:
        engine.dispose()


def test_state_binding_drift_is_decision_stale(tmp_path: Path) -> None:
    """A decision bound to a state digest refuses a divergent binding."""
    database = tmp_path / "state-drift.db"
    protected = _state_binding()
    _seed(
        database,
        decision_state_sha256=jcs_sha256(protected),
        approval_state_sha256=jcs_sha256(protected),
        feature_base_sha="2" * 40,
    )
    engine = create_database_engine(database)
    try:
        outcome = apply_transition(
            engine,
            _approve_command(observed_state_sha256=jcs_sha256(protected)),
            facts=GuardFacts({}),
        )
    finally:
        engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.DECISION_STALE
    # Zero writes: the feature is untouched and the approval was not consumed.
    assert _feature_state(database) == ("awaiting_plan_review", 7)
    engine = create_database_engine(database)
    try:
        with engine.connect() as conn:
            consumed = conn.execute(
                text("SELECT count(*) FROM approvals WHERE consumed_by_command_id IS NOT NULL")
            ).scalar_one()
    finally:
        engine.dispose()
    assert consumed == 0


def test_missing_observed_state_digest_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "missing-observed-state.db"
    protected = _state_binding()
    protected_sha256 = jcs_sha256(protected)
    _seed(
        database,
        decision_state_sha256=protected_sha256,
        approval_state_sha256=protected_sha256,
    )

    engine = create_database_engine(database)
    try:
        outcome = apply_transition(
            engine,
            _approve_command(),
            facts=GuardFacts({}),
        )
    finally:
        engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.DECISION_STALE
    assert _feature_state(database) == ("awaiting_plan_review", 7)


def test_artifact_binding_drift_is_approval_invalid(tmp_path: Path) -> None:
    """An approval bound to an artifact digest refuses a divergent binding."""
    database = tmp_path / "artifact-drift.db"
    artifact_sha256 = jcs_sha256(ARTIFACT_BINDING)
    state_binding = _state_binding(artifact_sha256=artifact_sha256)
    _seed(
        database,
        decision_state_sha256=jcs_sha256(state_binding),
        approval_state_sha256=jcs_sha256(state_binding),
        approval_artifact_sha256=artifact_sha256,
        feature_artifact_sha256=artifact_sha256,
    )

    divergent = {**ARTIFACT_BINDING, "body_sha256": "2" * 64}
    engine = create_database_engine(database)
    try:
        outcome = apply_transition(
            engine,
            _approve_command(
                observed_state_sha256=jcs_sha256(state_binding),
                observed_artifact_sha256=artifact_sha256,
            ),
            facts=GuardFacts({}),
            artifact_reader=lambda _digest: ArtifactReadback(
                binding=divergent, canonical_body=b"a" * 128
            ),
        )
    finally:
        engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.APPROVAL_INVALID
    assert _feature_state(database) == ("awaiting_plan_review", 7)


def test_matching_bindings_do_not_refuse(tmp_path: Path) -> None:
    """A decision/approval bound to digests still applies on matching bindings."""
    database = tmp_path / "matching.db"
    artifact_sha256 = jcs_sha256(ARTIFACT_BINDING)
    state_binding = _state_binding(artifact_sha256=artifact_sha256)
    state_sha256 = jcs_sha256(state_binding)
    _seed(
        database,
        decision_state_sha256=state_sha256,
        approval_state_sha256=state_sha256,
        approval_artifact_sha256=artifact_sha256,
        feature_artifact_sha256=artifact_sha256,
    )

    engine = create_database_engine(database)
    try:
        outcome = apply_transition(
            engine,
            _approve_command(
                observed_state_sha256=state_sha256,
                observed_artifact_sha256=artifact_sha256,
            ),
            facts=GuardFacts({}),
            artifact_reader=lambda _digest: ArtifactReadback(
                binding=ARTIFACT_BINDING, canonical_body=b"a" * 128
            ),
        )
    finally:
        engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.APPLIED
    assert _feature_state(database) == ("approved", 8)


def test_non_approval_transition_cannot_consume_injected_approval_id(
    tmp_path: Path,
) -> None:
    """An extra parameter must not turn an ordinary spec into an approval use."""
    database = tmp_path / "injected-approval-id.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        feature = feature_row(feature_id=FEATURE_ID, version=7, state="coding")
        session.add(feature)
        session.add(
            approval_row(
                feature_id=FEATURE_ID,
                approval_id=APPROVAL_ID,
                action="approve_plan",
                expected_feature_version=7,
                expected_state="coding",
                state_sha256=jcs_sha256(build_state_binding(feature)),
            )
        )

    command = TransitionCommand(
        aggregate_type="feature",
        aggregate_id=FEATURE_ID,
        command_type="block_feature",
        command_parameters={
            "target_state": "blocked_auth",
            "effect_outcome": None,
            "approval_id": APPROVAL_ID,
        },
        actor_type="service",
        evidence_source_types=("provider-adapter",),
        evidence_schema_versions=("dal.evidence.provider-failure/1.0",),
        decision_action=None,
        reason_code="AUTH_REQUIRED",
        expected_version=7,
        idempotency_key="idem-injected-approval",
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    with engine.connect() as connection:
        consumed_by = connection.execute(
            text(
                "SELECT consumed_by_command_id FROM approvals "
                "WHERE approval_id = :approval_id"
            ).bindparams(approval_id=APPROVAL_ID)
        ).scalar_one()
    engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.APPROVAL_INVALID
    assert _feature_state(database) == ("coding", 7)
    assert consumed_by is None


def test_approval_action_must_match_command_authority(tmp_path: Path) -> None:
    database = tmp_path / "approval-action-mismatch.db"
    state_sha256 = jcs_sha256(_state_binding())
    _seed(
        database,
        decision_state_sha256=state_sha256,
        approval_state_sha256=state_sha256,
    )
    engine = create_database_engine(database)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE approvals SET action = 'execute_merge' "
                "WHERE approval_id = :approval_id"
            ).bindparams(approval_id=APPROVAL_ID)
        )

    outcome = apply_transition(
        engine,
        _approve_command(observed_state_sha256=state_sha256),
        facts=GuardFacts({}),
    )
    with engine.connect() as connection:
        consumed_by = connection.execute(
            text(
                "SELECT consumed_by_command_id FROM approvals "
                "WHERE approval_id = :approval_id"
            ).bindparams(approval_id=APPROVAL_ID)
        ).scalar_one()
    engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.APPROVAL_INVALID
    assert consumed_by is None


def test_approval_decision_binding_must_match_submitted_card(tmp_path: Path) -> None:
    database = tmp_path / "approval-decision-mismatch.db"
    state_sha256 = jcs_sha256(_state_binding())
    _seed(
        database,
        decision_state_sha256=state_sha256,
        approval_state_sha256=state_sha256,
    )
    engine = create_database_engine(database)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            decision_row(
                feature_id=FEATURE_ID,
                decision_id="decision-other",
                state_sha256=state_sha256,
            )
        )

    base = _approve_command(observed_state_sha256=state_sha256)
    command = replace(
        base,
        command_parameters={
            **base.command_parameters,
            "decision_id": "decision-other",
        },
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    with engine.connect() as connection:
        consumed_by = connection.execute(
            text(
                "SELECT consumed_by_command_id FROM approvals "
                "WHERE approval_id = :approval_id"
            ).bindparams(approval_id=APPROVAL_ID)
        ).scalar_one()
    engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.DECISION_STALE
    assert consumed_by is None


def test_different_approval_loser_is_version_conflict(tmp_path: Path) -> None:
    """Different approvals competing for one version stay on version CAS."""
    database = tmp_path / "different-approval-version-race.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    state_sha256 = jcs_sha256(_state_binding())
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id=FEATURE_ID, version=7, state="awaiting_plan_review"
            )
        )
        for suffix in ("one", "two"):
            decision_id = f"decision-{suffix}"
            approval_id = f"approval-{suffix}"
            session.add(
                decision_row(
                    feature_id=FEATURE_ID,
                    decision_id=decision_id,
                    state_sha256=state_sha256,
                )
            )
            session.add(
                approval_row(
                    feature_id=FEATURE_ID,
                    approval_id=approval_id,
                    action="approve_plan",
                    decision_id=decision_id,
                    decision_version=1,
                    expected_feature_version=7,
                    expected_state="awaiting_plan_review",
                    state_sha256=state_sha256,
                )
            )

    base = _approve_command(observed_state_sha256=state_sha256)
    commands = []
    for suffix in ("one", "two"):
        commands.append(
            replace(
                base,
                command_parameters={
                    **base.command_parameters,
                    "approval_id": f"approval-{suffix}",
                    "decision_id": f"decision-{suffix}",
                },
                idempotency_key=f"idem-{suffix}",
            )
        )

    first = apply_transition(engine, commands[0], facts=GuardFacts({}))
    second = apply_transition(engine, commands[1], facts=GuardFacts({}))
    with engine.connect() as connection:
        second_consumed_by = connection.execute(
            text(
                "SELECT consumed_by_command_id FROM approvals "
                "WHERE approval_id = 'approval-two'"
            )
        ).scalar_one()
    engine.dispose()

    assert first.receipt_code == ReceiptCodes.APPLIED
    assert second.receipt_code == ReceiptCodes.VERSION_CONFLICT
    assert _feature_state(database) == ("approved", 8)
    assert second_consumed_by is None


def test_new_approval_is_server_bound_before_same_transaction_consume(
    tmp_path: Path,
) -> None:
    """The real record+consume path may not persist the historical null hashes."""

    database = tmp_path / "new-approval-bound.db"
    state_binding = _state_binding()
    state_sha256 = jcs_sha256(state_binding)
    _seed(
        database,
        decision_state_sha256=state_sha256,
        seed_approval=False,
    )
    base_command = _approve_command(observed_state_sha256=state_sha256)
    command = replace(
        base_command,
        command_parameters={
            key: value
            for key, value in base_command.command_parameters.items()
            if key != "approval_id"
        },
    )

    engine = create_database_engine(database)
    try:
        outcome = apply_transition(engine, command, facts=GuardFacts({}))
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT state_sha256, artifact_sha256, "
                    "consumed_by_command_id FROM approvals"
                )
            ).one()
    finally:
        engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.APPLIED
    assert row[0] == state_sha256
    assert row[1] is None
    assert row[2] == command.idempotency_key


def test_new_decision_binds_the_post_transition_server_state(tmp_path: Path) -> None:
    database = tmp_path / "new-decision-bound.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(feature_row(feature_id=FEATURE_ID, version=7, state="coding"))

    command = TransitionCommand(
        aggregate_type="feature",
        aggregate_id=FEATURE_ID,
        command_type="block_feature",
        command_parameters={"target_state": "blocked_auth", "effect_outcome": None},
        actor_type="service",
        evidence_source_types=("provider-adapter",),
        evidence_schema_versions=("dal.evidence.provider-failure/1.0",),
        decision_action=None,
        reason_code="AUTH_REQUIRED",
        expected_version=7,
        idempotency_key="idem-new-decision-bound",
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    with sessions() as session:
        feature = session.execute(
            text("SELECT * FROM features WHERE feature_id = :id").bindparams(
                id=FEATURE_ID
            )
        ).mappings().one()
        stored_sha256 = session.execute(
            text("SELECT state_sha256 FROM decisions")
        ).scalar_one()
    engine.dispose()

    assert outcome.receipt_code == ReceiptCodes.APPLIED
    assert stored_sha256 == jcs_sha256(build_state_binding(feature))
