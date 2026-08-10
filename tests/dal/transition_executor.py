"""Replays frozen transition fixtures against the real engine.

Two halves, kept apart on purpose:

- **Arrangement** may read the resolved spec. Seeding an approval before a
  spec whose write set consumes one is setting up the world the transition
  expects, and the contract is the only place that says what that world is.
- **Judgement** may not. The write set the oracle is compared against is
  measured from the database before and after the call, using a mapping from
  write-set member to observable change that lives here rather than in the
  engine. That mapping is the independent second opinion; without it the test
  would be comparing the engine's account of itself against the contract and
  would pass whatever the engine actually did (the defect DAL-008's injection
  sweep found).

Test-only module.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import Engine, inspect, text

from personal_agent_core.timeutil import utc_now

from personal_agent_dal.machine.engine import (
    TransitionCommand,
    TransitionOutcome,
    apply_transition,
)
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_dal.machine.registry import transition_registry
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory


#: Tables whose row count is watched. A change anywhere else is reported as an
#: unexpected write rather than ignored.
_WATCHED_TABLES: tuple[str, ...] = (
    "features",
    "events",
    "operation_events",
    "outbox_events",
    "audit_events",
    "transition_receipts",
    "decisions",
    "decision_card_projections",
    "approvals",
    "approval_action_receipts",
    "capabilities",
    "leases",
    "external_effects",
    "recovery_cases",
    "evidence_records",
    "impact_reports",
    "retention_tombstones",
    "operation_receipts",
    "migration_receipts",
)


def snapshot(engine: Engine) -> dict[str, Any]:
    """Everything a transition could change, as plain values."""
    tables = set(inspect(engine).get_table_names())
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in _WATCHED_TABLES:
            if table in tables:
                counts[table] = connection.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                ).scalar_one()
        statuses = {}
        if "decisions" in tables:
            statuses = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT decision_id, status FROM decisions")
                )
            }
        approvals_consumed = 0
        if "approvals" in tables:
            approvals_consumed = connection.execute(
                text(
                    "SELECT count(*) FROM approvals "
                    "WHERE consumed_by_command_id IS NOT NULL"
                )
            ).scalar_one()
        capabilities_revoked = capability_uses = 0
        if "capabilities" in tables:
            capabilities_revoked = connection.execute(
                text("SELECT count(*) FROM capabilities WHERE revoked_at IS NOT NULL")
            ).scalar_one()
            capability_uses = (
                connection.execute(
                    text("SELECT coalesce(sum(uses_consumed), 0) FROM capabilities")
                ).scalar_one()
                or 0
            )
        leases_revoked = 0
        if "leases" in tables:
            leases_revoked = connection.execute(
                text("SELECT count(*) FROM leases WHERE revoked_at IS NOT NULL")
            ).scalar_one()
        feature = {}
        if "features" in tables:
            row = connection.execute(
                text(
                    "SELECT state, version, reason_code, reason_owner, "
                    "capability_epoch, plan_version, "
                    "external_effect_inventory_sha256, checkpoint_state "
                    "FROM features LIMIT 1"
                )
            ).first()
            if row is not None:
                feature = {
                    "state": row[0], "version": row[1], "reason_code": row[2],
                    "reason_owner": row[3], "capability_epoch": row[4],
                    "plan_version": row[5], "inventory": row[6],
                    "checkpoint_state": row[7],
                }
        effects = {}
        if "external_effects" in tables:
            effects = {
                row[0]: (row[1], row[2], row[3])
                for row in connection.execute(
                    text(
                        "SELECT effect_id, state, version, "
                        "coalesce(executor_id, '') FROM external_effects"
                    )
                )
            }
        recovery = {}
        if "recovery_cases" in tables:
            recovery = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    text("SELECT recovery_case_id, state, version FROM recovery_cases")
                )
            }
    return {
        "counts": counts,
        "decision_statuses": statuses,
        "approvals_consumed": approvals_consumed,
        "capabilities_revoked": capabilities_revoked,
        "capability_uses": capability_uses,
        "leases_revoked": leases_revoked,
        "feature": feature,
        "effects": effects,
        "recovery": recovery,
    }


def _grew(before: dict[str, Any], after: dict[str, Any], table: str) -> bool:
    return after["counts"].get(table, 0) > before["counts"].get(table, 0)


def _status_moved(before: dict[str, Any], after: dict[str, Any], status: str) -> bool:
    was = sum(1 for s in before["decision_statuses"].values() if s == status)
    now = sum(1 for s in after["decision_statuses"].values() if s == status)
    return now > was


#: Write-set member -> "did the database actually show this?". Written from the
#: contract, independently of the engine's appliers, so that an applier that
#: stops doing its work is caught rather than believed.
OBSERVERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], bool]] = {
    "aggregate": lambda b, a: (
        b["feature"].get("version") != a["feature"].get("version")
        or b["recovery"] != a["recovery"]
        or any(b["effects"].get(k, (None,))[1] != v[1] for k, v in a["effects"].items())
    ),
    "business_event": lambda b, a: _grew(b, a, "events"),
    "transition_receipt": lambda b, a: _grew(b, a, "transition_receipts"),
    "recovery_transition_receipt": lambda b, a: _grew(b, a, "transition_receipts"),
    "external_effect_transition_receipt": lambda b, a: _grew(
        b, a, "transition_receipts"
    ),
    "audit": lambda b, a: _grew(b, a, "audit_events"),
    "notification_outbox": lambda b, a: _grew(b, a, "outbox_events"),
    "decision_create": lambda b, a: _grew(b, a, "decisions"),
    "decision": lambda b, a: _grew(b, a, "decisions"),
    "incident_decision": lambda b, a: _grew(b, a, "decisions"),
    "decision_resolve": lambda b, a: _status_moved(b, a, "resolved"),
    "decision_consume": lambda b, a: _status_moved(b, a, "consumed"),
    "decision_supersede": lambda b, a: _status_moved(b, a, "superseded"),
    "decision_projection": lambda b, a: _grew(b, a, "decision_card_projections"),
    "decision_action_receipt": lambda b, a: _grew(b, a, "evidence_records"),
    "approval_record": lambda b, a: _grew(b, a, "approvals"),
    "approval_consume": lambda b, a: a["approvals_consumed"] > b["approvals_consumed"],
    "approval_action_receipt": lambda b, a: _grew(b, a, "approval_action_receipts"),
    "approval_action_receipt_ref": lambda b, a: _grew(b, a, "evidence_records"),
    "capability_epoch_increment": lambda b, a: (
        a["feature"].get("capability_epoch") != b["feature"].get("capability_epoch")
    ),
    "capability_issue": lambda b, a: _grew(b, a, "capabilities"),
    "capability_consume": lambda b, a: a["capability_uses"] > b["capability_uses"],
    "capability_revoke": lambda b, a: (
        a["capabilities_revoked"] > b["capabilities_revoked"]
    ),
    "lease_issue": lambda b, a: _grew(b, a, "leases"),
    "lease_revoke": lambda b, a: a["leases_revoked"] > b["leases_revoked"],
    "external_effect": lambda b, a: (
        _grew(b, a, "external_effects") or b["effects"] != a["effects"]
    ),
    "external_effect_intent": lambda b, a: _grew(b, a, "external_effects"),
    "observed_external_effect": lambda b, a: _grew(b, a, "external_effects"),
    "external_effect_outcome": lambda b, a: b["effects"] != a["effects"],
    "external_effect_inventory": lambda b, a: (
        a["feature"].get("inventory") != b["feature"].get("inventory")
    ),
    "executor_claim": lambda b, a: b["effects"] != a["effects"],
    "executor_claim_release": lambda b, a: b["effects"] != a["effects"],
    "dispatch_marker": lambda b, a: b["effects"] != a["effects"],
    "reconciler_claim": lambda b, a: b["effects"] != a["effects"],
    "recovery_case": lambda b, a: (
        _grew(b, a, "recovery_cases") or b["recovery"] != a["recovery"]
    ),
    "evidence": lambda b, a: _grew(b, a, "evidence_records"),
    "authoritative_receipt": lambda b, a: _grew(b, a, "evidence_records"),
    "authoritative_post_read": lambda b, a: _grew(b, a, "evidence_records"),
    "impact_report": lambda b, a: _grew(b, a, "impact_reports"),
    "impact": lambda b, a: _grew(b, a, "impact_reports"),
    "plan_version": lambda b, a: (
        a["feature"].get("plan_version") != b["feature"].get("plan_version")
    ),
}


def unobserved_members(
    declared: tuple[str, ...], before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    """Declared members the database does not corroborate."""
    missing = []
    for member in declared:
        observer = OBSERVERS.get(member)
        if observer is None:
            missing.append(f"{member} (no independent observer)")
        elif not observer(before, after):
            missing.append(member)
    return missing


def database_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether anything at all changed. Used to hold refusals to zero writes."""
    return before != after


def seed_for(engine: Engine, fixture: dict[str, Any]) -> None:
    """Build the world the fixture's spec expects to act on.

    Arrangement, not judgement: what has to pre-exist is read from the spec's
    own write set, because the fixture's `pre_state` deliberately carries only
    the aggregate.
    """
    from tests.dal.factories import (
        approval_row,
        capability_row,
        decision_row,
        external_effect_row,
        feature_row,
        lease_row,
        recovery_case_row,
    )

    pre = fixture["pre_state"]
    coverage = fixture.get("coverage_ref")
    spec = (
        transition_registry().by_id(coverage)
        if coverage in transition_registry().spec_ids
        else None
    )
    writes = set(spec["atomic_write_set"]) if spec else set()
    aggregate_type = pre["entity_type"]
    now = utc_now()

    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        # The feature always exists: it owns the decisions, approvals and
        # effects every other entity hangs off.
        feature_id = pre["entity_id"] if aggregate_type == "feature" else "feat-owner"
        # A null pre-state means the aggregate genuinely does not exist yet:
        # this is a creation fixture, and seeding the row would make the
        # transition under test impossible.
        creating = aggregate_type == "feature" and pre["state"] is None
        if not creating:
            session.add(
                feature_row(
                    feature_id=feature_id,
                    version=pre["version"] if aggregate_type == "feature" else 1,
                    state=pre["state"] if aggregate_type == "feature" else "coding",
                )
            )
        if aggregate_type == "recovery_case":
            session.add(
                recovery_case_row(
                    recovery_case_id=pre["entity_id"],
                    feature_id=feature_id,
                    version=pre["version"],
                    state=pre["state"],
                )
            )
        if aggregate_type == "external_effect":
            session.add(
                external_effect_row(
                    effect_id=pre["entity_id"],
                    owner_id=feature_id,
                    version=pre["version"],
                    state=pre["state"],
                )
            )

        # One seeded decision per decision-mutating member: a spec that
        # resolves the current card *and* supersedes an older one needs two
        # open decisions, not one reused twice.
        decision_members = [
            member
            for member in ("decision_resolve", "decision_consume", "decision_supersede")
            if member in writes
        ]
        for ordinal, member in enumerate(decision_members):
            session.add(
                decision_row(
                    feature_id=feature_id,
                    decision_id=f"decision-seeded-{ordinal}-{member}",
                )
            )
        if "approval_consume" in writes and "approval_record" not in writes:
            session.add(approval_row(feature_id=feature_id))
        if writes & {"capability_consume", "capability_revoke"}:
            session.add(capability_row(feature_id=feature_id))
        if "lease_revoke" in writes:
            session.add(lease_row(feature_id=feature_id))
        # A companion that closes an effect needs that effect to already exist,
        # owned by the root aggregate and in the companion's exact from_state.
        companion_from = None
        for companion in (spec["atomic_companion_transitions"] if spec else []):
            if companion["aggregate_type"] == "external_effect":
                companion_from = companion["from_state"]
        needs_effect = writes & {"external_effect_outcome"} or companion_from
        if needs_effect and aggregate_type != "external_effect":
            session.add(
                external_effect_row(
                    effect_id="effect-seeded",
                    owner_id=pre["entity_id"],
                    version=1,
                    state=companion_from or "dispatch_started",
                    owner_type=aggregate_type,
                )
            )


def guard_facts_from(fixture: dict[str, Any]) -> GuardFacts:
    """The trusted resolver's facts, exactly as the fixture supplies them."""
    context = fixture.get("trusted_resolver_context") or {}
    preconditions = context.get("guard_preconditions") or {}
    values = {fact["field"]: fact["value"] for fact in preconditions.get("facts", [])}
    values.setdefault("runtime.now", utc_now())
    return GuardFacts(values)


def run_transition_fixture(
    fixture: dict[str, Any], *, database: Path
) -> tuple[TransitionOutcome, dict[str, Any], dict[str, Any]]:
    """Seed, run the real engine, and return the outcome with both snapshots."""
    engine = create_database_engine(database)
    db.upgrade(engine)
    seed_for(engine, fixture)

    body = fixture["transition_command"]
    command = TransitionCommand.from_fixture(
        body, idempotency_key=f"idem-{fixture['variant_id']}"
    )
    before = snapshot(engine)
    outcome = apply_transition(
        engine, command, facts=guard_facts_from(fixture), now=utc_now()
    )
    after = snapshot(engine)
    return outcome, before, after
