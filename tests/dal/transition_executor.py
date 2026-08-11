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


def run_scenario_fixture(
    fixture: dict[str, Any], *, database: Path
) -> tuple[TransitionOutcome, dict[str, Any], dict[str, Any]]:
    """Seed, resolve the business command, run the engine, snapshot both sides.

    Seeding is spec-driven for exactly the same reason as the registry
    vectors: a cancel whose write set consumes a decision, a capability and a
    lease needs those rows to pre-exist, and only the resolved spec's write
    set says so. The resolver therefore runs first and hands its spec_id to
    the same `seed_for` the registry vectors use.
    """
    command, facts, spec_id = resolve_business_command(fixture)

    engine = create_database_engine(database)
    db.upgrade(engine)
    seeding = dict(fixture)
    seeding["coverage_ref"] = spec_id
    seed_for(engine, seeding)

    before = snapshot(engine)
    outcome = apply_transition(engine, command, facts=facts, now=utc_now())
    after = snapshot(engine)
    return outcome, before, after



# ---------------------------------------------------------------------------
# Persisted-content judgement
# ---------------------------------------------------------------------------
#
# The frozen oracles assert event-type strings and receipt codes only: they
# were generated without expectations for `from_state`, `to_state`,
# `aggregate_version` or `spec_id`, so an engine that persisted the wrong ones
# would still match every oracle. The functions below close that blind spot.
# They are judgement, not arrangement: they read the fixture's pre-state, the
# outcome and the database. The registry row is consulted only to learn
# *which* companion aggregates exist -- the identity checks themselves are
# derived from the pre-state and the seeding contract above.


def _pre_aggregate(fixture: dict[str, Any]) -> tuple[str, str | None, int]:
    """The command aggregate's (type, state, version) before the transition."""
    pre = fixture["pre_state"]
    return pre["entity_type"], pre["state"], pre["version"]


def persisted_content_divergences(
    database: Path, fixture: dict[str, Any], outcome: TransitionOutcome
) -> list[str]:
    """What the persisted rows must say, checked against what they do say."""
    engine = create_database_engine(database)
    with engine.connect() as connection:
        receipts = [
            {
                "aggregate_type": row[0],
                "aggregate_id": row[1],
                "aggregate_version": row[2],
                "spec_id": row[3],
                "from_state": row[4],
                "to_state": row[5],
                "receipt_schema_version": row[6],
            }
            for row in connection.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, aggregate_version, "
                    "spec_id, from_state, to_state, receipt_schema_version "
                    "FROM transition_receipts"
                )
            )
        ]
        events = [
            {
                "event_id": row[0],
                "event_type": row[1],
                "aggregate_type": row[2],
                "aggregate_id": row[3],
                "aggregate_version": row[4],
            }
            for row in connection.execute(
                text(
                    "SELECT event_id, event_type, aggregate_type, aggregate_id, "
                    "aggregate_version FROM events ORDER BY event_id"
                )
            )
        ]
        states: dict[tuple[str, str], tuple[str, int]] = {}
        for table, id_column in (
            ("features", "feature_id"),
            ("recovery_cases", "recovery_case_id"),
            ("external_effects", "effect_id"),
        ):
            aggregate_key = {
                "features": "feature",
                "recovery_cases": "recovery_case",
                "external_effects": "external_effect",
            }[table]
            for row in connection.execute(
                text(
                    f"SELECT {id_column}, state, version FROM {table}"  # noqa: S608
                )
            ):
                states[(aggregate_key, row[0])] = (row[1], row[2])
    engine.dispose()

    problems = _receipt_divergences(fixture, outcome, receipts)
    problems.extend(_event_divergences(fixture, outcome, events, receipts))
    problems.extend(_state_consistency_divergences(receipts, states))
    return problems


def _state_consistency_divergences(
    receipts: list[dict[str, Any]],
    states: dict[tuple[str, str], tuple[str, int]],
) -> list[str]:
    """A receipt must agree with the aggregate row it certifies.

    The receipt is the durable record of "this aggregate moved to S at version
    V". If the row says something else, one of the two is lying, and the lie
    is exactly what a replay or an audit would later act on.
    """
    problems: list[str] = []
    for receipt in receipts:
        actual = states.get((receipt["aggregate_type"], receipt["aggregate_id"]))
        if actual is None:
            problems.append(
                f"{receipt['aggregate_type']} receipt names "
                f"{receipt['aggregate_id']}, which has no row"
            )
            continue
        if receipt["to_state"] != actual[0]:
            problems.append(
                f"{receipt['aggregate_type']} receipt to_state "
                f"{receipt['to_state']!r} != persisted state {actual[0]!r}"
            )
        if receipt["aggregate_version"] != actual[1]:
            problems.append(
                f"{receipt['aggregate_type']} receipt version "
                f"{receipt['aggregate_version']} != persisted version {actual[1]}"
            )
    return problems


def _receipt_divergences(
    fixture: dict[str, Any],
    outcome: TransitionOutcome,
    receipts: list[dict[str, Any]],
) -> list[str]:
    problems: list[str] = []
    aggregate_type, pre_state, pre_version = _pre_aggregate(fixture)
    expected_id = fixture["pre_state"]["entity_id"]
    expected_version = pre_version + 1 if pre_state is not None else 1

    roots = [r for r in receipts if r["aggregate_type"] == aggregate_type]
    if len(roots) != 1:
        problems.append(
            f"expected 1 root receipt for {aggregate_type}, found {len(roots)}"
        )
    else:
        root = roots[0]
        _check(
            problems, "root receipt",
            aggregate_id=(root["aggregate_id"], expected_id),
            from_state=(root["from_state"], pre_state),
            to_state=(root["to_state"], outcome.to_state),
            aggregate_version=(root["aggregate_version"], expected_version),
            spec_id=(root["spec_id"], outcome.spec_id),
            receipt_schema_version=(
                root["receipt_schema_version"], outcome.receipt_schema
            ),
        )

    companions = _expected_companions(fixture)
    case_companion = any(c["aggregate_type"] == "recovery_case" for c in companions)
    effect_companion = next(
        (c for c in companions if c["aggregate_type"] == "external_effect"), None
    )

    if case_companion and aggregate_type != "recovery_case":
        # The case was created by this command: its receipt binds the new
        # case's own identity -- a fresh id, version 1, no prior state (§2.5).
        # The to_state is checked against the case row itself in
        # `_state_consistency_divergences`: the durable record must agree with
        # the aggregate it certifies, whatever that state is.
        case_receipts = [
            r for r in receipts if r["aggregate_type"] == "recovery_case"
        ]
        if len(case_receipts) != 1:
            problems.append(
                f"expected 1 recovery-case receipt, found {len(case_receipts)}"
            )
        else:
            case = case_receipts[0]
            _check(
                problems, "recovery receipt",
                from_state=(case["from_state"], None),
                aggregate_version=(case["aggregate_version"], 1),
            )
            if case["aggregate_id"] == expected_id:
                problems.append(
                    "recovery receipt aggregate_id is the feature's id, "
                    "not the created case's"
                )

    if aggregate_type == "external_effect":
        # The effect is the command aggregate; the root receipt already
        # covers it. A second effect receipt would be a duplicate.
        extra = [r for r in receipts if r["aggregate_type"] == "external_effect"]
        if len(extra) != 1:
            problems.append(
                f"expected exactly the root effect receipt, found {len(extra)}"
            )
    elif effect_companion is not None:
        expected_effect = _expected_seeded_effect(effect_companion)
        effect_receipts = [
            r for r in receipts if r["aggregate_type"] == "external_effect"
        ]
        if expected_effect is None:
            # A creating companion mints a fresh effect id; only the
            # from/version shape is checkable.
            if len(effect_receipts) != 1:
                problems.append(
                    f"expected 1 external-effect receipt, "
                    f"found {len(effect_receipts)}"
                )
            else:
                _check(
                    problems, "effect receipt",
                    from_state=(effect_receipts[0]["from_state"], None),
                    aggregate_version=(effect_receipts[0]["aggregate_version"], 1),
                )
        else:
            if len(effect_receipts) != 1:
                problems.append(
                    f"expected 1 external-effect receipt, "
                    f"found {len(effect_receipts)}"
                )
            else:
                effect = effect_receipts[0]
                _check(
                    problems, "effect receipt",
                    aggregate_id=(effect["aggregate_id"], expected_effect[0]),
                    from_state=(effect["from_state"], expected_effect[1]),
                    aggregate_version=(
                        effect["aggregate_version"], expected_effect[2]
                    ),
                )
    return problems


def _event_divergences(
    fixture: dict[str, Any],
    outcome: TransitionOutcome,
    events: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> list[str]:
    problems: list[str] = []
    aggregate_type, pre_state, pre_version = _pre_aggregate(fixture)
    expected_id = fixture["pre_state"]["entity_id"]
    expected_version = pre_version + 1 if pre_state is not None else 1

    expected_types = list(outcome.events)
    actual_types = [e["event_type"] for e in events]
    if actual_types != expected_types:
        problems.append(
            f"persisted event trace {actual_types} != declared {expected_types}"
        )
        return problems

    business = events[0]
    _check(
        problems, "business event",
        aggregate_type=(business["aggregate_type"], aggregate_type),
        aggregate_id=(business["aggregate_id"], expected_id),
        aggregate_version=(business["aggregate_version"], expected_version),
    )

    companions = _expected_companions(fixture)
    for event, companion in zip(events[1:], companions):
        if event["aggregate_type"] != companion["aggregate_type"]:
            problems.append(
                f"companion event {event['event_id']} is typed "
                f"{event['aggregate_type']}, expected {companion['aggregate_type']}"
            )
            continue
        if companion["aggregate_type"] == "recovery_case":
            if aggregate_type == "recovery_case":
                expected_case_id, expected_case_version = (
                    expected_id, expected_version
                )
            else:
                case_receipts = [
                    r for r in receipts if r["aggregate_type"] == "recovery_case"
                ]
                expected_case_id = (
                    case_receipts[0]["aggregate_id"] if case_receipts else None
                )
                expected_case_version = 1
            if event["aggregate_id"] != expected_case_id:
                problems.append(
                    f"companion event aggregate_id {event['aggregate_id']!r} "
                    f"!= the case's own id {expected_case_id!r}"
                )
            if event["aggregate_version"] != expected_case_version:
                problems.append(
                    f"companion event aggregate_version "
                    f"{event['aggregate_version']} != {expected_case_version}"
                )
        else:  # external_effect companion
            if aggregate_type == "external_effect":
                expected_effect_id, expected_effect_version = (
                    expected_id, expected_version
                )
            else:
                effect_receipts = [
                    r for r in receipts
                    if r["aggregate_type"] == "external_effect"
                ]
                expected_effect_id = (
                    effect_receipts[0]["aggregate_id"] if effect_receipts else None
                )
                expected_effect_version = (
                    effect_receipts[0]["aggregate_version"]
                    if effect_receipts else None
                )
            if event["aggregate_id"] != expected_effect_id:
                problems.append(
                    f"companion event aggregate_id {event['aggregate_id']!r} "
                    f"!= the effect's own id {expected_effect_id!r}"
                )
            if event["aggregate_version"] != expected_effect_version:
                problems.append(
                    f"companion event aggregate_version "
                    f"{event['aggregate_version']} != {expected_effect_version}"
                )
    return problems


def _check(problems: list[str], subject: str, **pairs: tuple[Any, Any]) -> None:
    for name, (actual, expected) in pairs.items():
        if actual != expected:
            problems.append(f"{subject} {name} {actual!r} != {expected!r}")


def _expected_companions(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """The companions this fixture's transition should emit, read from the
    registry row the fixture names -- used only to know which companion
    aggregates the judgement should expect rows for."""
    coverage = fixture.get("coverage_ref")
    registry = transition_registry()
    if coverage not in registry.spec_ids:
        return []
    return list(registry.by_id(coverage)["atomic_companion_transitions"])


def _expected_seeded_effect(
    companion: dict[str, Any]
) -> tuple[str, str | None, int] | None:
    """(effect_id, from_state, version) of the seeded effect, if it pre-exists.

    `seed_for` seeds exactly one closing effect per fixture -- id
    `effect-seeded`, version 1, in the companion's `from_state` -- so after a
    close the row must be at version 2. A creating companion (`from_state`
    null) mints a fresh id the test cannot predict; `None` marks that.
    """
    from_state = companion["from_state"]
    if from_state is None:
        return None
    return "effect-seeded", from_state, 2


# ---------------------------------------------------------------------------
# Business-command resolver (the 13 `operation_commands` scenario variants)
# ---------------------------------------------------------------------------
#
# A scenario fixture carries no `transition_command`: it describes a business
# operation (`record_plan`, `cancel_recovery`, …) plus the authoritative facts
# the operation observed. The trusted resolver's job -- which in production
# belongs to the workflow service -- is to turn that pair into the one
# registry command the facts commit to. This layer is that resolver, written
# from the contract's evidence bindings rather than from the oracles: an
# oracle says *what happened*, the binding says *what the facts commit to*,
# and the scenario only proves something if the two are derived independently.


def _bindings_for(
    aggregate_type: str, from_state: str, command_type: str
) -> list[dict[str, Any]]:
    """Registry rows matching the resolution triple, with their bindings."""
    registry = transition_registry()
    matched = []
    for spec_id in registry.spec_ids:
        spec = registry.by_id(spec_id)
        if (
            spec["aggregate_type"] == aggregate_type
            and spec["from_state"] == from_state
            and spec["command_type"] == command_type
        ):
            matched.append(spec)
    return matched


def _command_for_spec(
    spec: dict[str, Any],
    *,
    aggregate_id: str,
    expected_version: int | None,
    idempotency_key: str,
    reason_code: str | None = None,
    decision_action: str | None = None,
) -> TransitionCommand:
    binding = spec["actor_evidence_bindings"][0]
    return TransitionCommand(
        aggregate_type=spec["aggregate_type"],
        aggregate_id=aggregate_id,
        command_type=spec["command_type"],
        command_parameters={
            key: value
            for key, value in (spec["command_parameters"] or {}).items()
        },
        actor_type=binding["actor_type"],
        evidence_source_types=tuple(binding["required_evidence_source_types"]),
        evidence_schema_versions=tuple(
            spec["required_evidence_schema_versions"]
        ),
        decision_action=decision_action,
        reason_code=reason_code,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
    )


def resolve_business_command(
    fixture: dict[str, Any],
) -> tuple[TransitionCommand, GuardFacts, str | None]:
    """Resolve a scenario fixture's business command to a registry command.

    Returns the command, the guard facts, and the resolved `spec_id` -- the
    last so the caller can seed the world that spec's write set expects. A
    refusal-by-resolution scenario (no registry row exists) returns `None`:
    nothing is seeded beyond the aggregate, because the engine must refuse
    before any write set is consulted.

    Fails loudly (`KeyError`/`ValueError`) on a business command with no
    contract reading: silently defaulting one would turn an unimplemented
    scenario into a passing one.
    """
    operation = fixture["operation_sequence"][0]
    step = operation["input"]["action_sequence"][0]
    facts = operation["input"]["authoritative_facts"]
    target = operation["input"]["target"]
    business = step["command"]
    state = target["state"]
    version = target["version"]
    entity_id = target["entity_id"]
    key = operation["idempotency_key"]

    guard_values: dict[str, Any] = {"runtime.now": utc_now()}

    def specs_for(command_type: str, from_state: str) -> list[dict[str, Any]]:
        specs = _bindings_for(target["entity_type"], from_state, command_type)
        if not specs:
            raise KeyError(
                f"no registry spec for ({target['entity_type']}, "
                f"{from_state}, {command_type})"
            )
        return specs

    # A terminal aggregate has no exit edges by definition (§2.2): any command
    # aimed at one must be refused as TERMINAL_STATE before the registry is
    # consulted, and the command's parameters never reach a spec. The
    # `terminal_state` fact is the scenario declaring "this probe expects the
    # terminal guard, not a resolved transition".
    if facts.get("terminal_state") is not None:
        command = TransitionCommand(
            aggregate_type=target["entity_type"],
            aggregate_id=entity_id,
            command_type=business,
            command_parameters={"target_state": None, "effect_outcome": None},
            actor_type=operation["actor_type"],
            evidence_source_types=(operation["evidence_source_type"],),
            evidence_schema_versions=(),
            decision_action=None,
            reason_code=None,
            expected_version=version,
            idempotency_key=key,
        )
        return command, GuardFacts(guard_values), None

    if business == "resume_feature":
        observed = facts["observed_base_sha"]
        approved = facts["approved_base_sha"]
        if observed != approved:
            # The drift probe moved: the feature cannot resume, it blocks.
            (spec,) = [
                s for s in specs_for("block_feature", state)
                if s["allowed_reason_codes"] == ["STATE_DRIFT"]
            ]
            command = _command_for_spec(
                spec, aggregate_id=entity_id, expected_version=version,
                idempotency_key=key, reason_code="STATE_DRIFT",
            )
            return command, GuardFacts(guard_values), spec["spec_id"]
        raise ValueError("resume_feature without drift is not a frozen scenario")

    if business == "record_plan":
        declared = facts.get("declared_event_type")
        required = facts.get("required_event_type")
        specs = _bindings_for(target["entity_type"], state, "record_plan")
        if specs and declared == required:
            (spec,) = specs
            command = _command_for_spec(
                spec, aggregate_id=entity_id, expected_version=version,
                idempotency_key=key,
            )
            return command, GuardFacts(guard_values), spec["spec_id"]
        if not specs:
            # No such edge from this state at all -- the terminal probes.
            # The engine's terminal check fires before the registry is
            # consulted, so the command's parameters never reach a spec.
            command = TransitionCommand(
                aggregate_type=target["entity_type"],
                aggregate_id=entity_id,
                command_type="record_plan",
                command_parameters={
                    "target_state": "awaiting_plan_review",
                    "effect_outcome": None,
                },
                actor_type=operation["actor_type"],
                evidence_source_types=(operation["evidence_source_type"],),
                evidence_schema_versions=("dal.evidence.plan/1.0",),
                decision_action=None,
                reason_code=None,
                expected_version=version,
                idempotency_key=key,
            )
            return command, GuardFacts(guard_values), None
        # The event the caller declares is not the one this state requires:
        # no registry row matches, and the engine must say so.
        command = TransitionCommand(
            aggregate_type=target["entity_type"],
            aggregate_id=entity_id,
            command_type="record_plan",
            command_parameters={"target_state": None, "effect_outcome": None},
            actor_type=operation["actor_type"],
            evidence_source_types=(operation["evidence_source_type"],),
            evidence_schema_versions=("dal.evidence.plan/1.0",),
            decision_action=None,
            reason_code=None,
            expected_version=version,
            idempotency_key=key,
        )
        return command, GuardFacts(guard_values), None

    if business == "record_plan_ready":
        # Same evidence commitment as record_plan; the scenario names the
        # business event, the registry names the command.
        return resolve_business_command(
            {
                **fixture,
                "operation_sequence": [
                    {
                        **operation,
                        "input": {
                            **operation["input"],
                            "action_sequence": [{"command": "record_plan"}],
                        },
                    }
                ],
            }
        )

    if business == "record_deployment":
        requested_from = facts["requested_from"]
        requested_to = facts["requested_to"]
        specs = _bindings_for(target["entity_type"], requested_from, "record_deployment")
        if not specs:
            # The requested edge does not exist in the registry at all.
            command = TransitionCommand(
                aggregate_type=target["entity_type"],
                aggregate_id=entity_id,
                command_type="record_deployment",
                command_parameters={
                    "target_state": requested_to,
                    "effect_outcome": None,
                },
                actor_type=operation["actor_type"],
                evidence_source_types=(operation["evidence_source_type"],),
                evidence_schema_versions=(),
                decision_action=None,
                reason_code=None,
                expected_version=version,
                idempotency_key=key,
            )
            return command, GuardFacts(guard_values), None
        raise ValueError(
            f"record_deployment {requested_from}->{requested_to} resolves; "
            "that is not the frozen illegal_edge scenario"
        )

    if business == "cancel_recovery":
        specs = specs_for("cancel_recovery", state)
        # Cancellation is a human act taken on a registered device
        # (§2.3.1 actor_evidence_bindings), whatever the operation envelope
        # claims; the binding is the contract, not the fixture's actor field.
        spec = specs[0]
        if len(specs) > 1:
            # Blocked cases resolve by the case's own stop reason.
            reason = _recovery_case_reason(fixture)
            narrowed = [
                s for s in specs if s["allowed_reason_codes"] == [reason]
            ]
            spec = narrowed[0] if narrowed else specs[0]
        command = _command_for_spec(
            spec, aggregate_id=entity_id, expected_version=version,
            idempotency_key=key, decision_action="cancel_recovery",
            reason_code=(
                spec["allowed_reason_codes"][0]
                if spec["allowed_reason_codes"] else None
            ),
        )
        return command, GuardFacts(guard_values), spec["spec_id"]

    if business == "start_recovery":
        if facts["current_approval_epoch"] != facts["approval_epoch"]:
            # The approval the case was opened under is no longer current:
            # starting would execute against a stale policy (§3.6).
            (spec,) = [
                s for s in specs_for("block_recovery_start", state)
                if s["allowed_reason_codes"] == ["RECOVERY_POLICY_STALE"]
            ]
            command = _command_for_spec(
                spec, aggregate_id=entity_id, expected_version=version,
                idempotency_key=key, reason_code="RECOVERY_POLICY_STALE",
            )
            return command, GuardFacts(guard_values), spec["spec_id"]
        (spec,) = specs_for("start_recovery", state)
        command = _command_for_spec(
            spec, aggregate_id=entity_id, expected_version=version,
            idempotency_key=key,
        )
        return command, GuardFacts(guard_values), spec["spec_id"]

    if business == "block_after_investigation_failure":
        (spec,) = [
            s for s in specs_for("block_recovery_investigation", state)
            if s["allowed_reason_codes"] == ["RECOVERY_READBACK_UNKNOWN"]
        ]
        command = _command_for_spec(
            spec, aggregate_id=entity_id, expected_version=version,
            idempotency_key=key, reason_code="RECOVERY_READBACK_UNKNOWN",
        )
        return command, GuardFacts(guard_values), spec["spec_id"]

    if business in ("block_recovery_execution", "block_recovery_verification"):
        (spec,) = [
            s for s in specs_for("block_recovery", state)
            if s["allowed_reason_codes"] == ["RECOVERY_EFFECT_UNKNOWN"]
        ]
        command = _command_for_spec(
            spec, aggregate_id=entity_id, expected_version=version,
            idempotency_key=key, reason_code="RECOVERY_EFFECT_UNKNOWN",
        )
        return command, GuardFacts(guard_values), spec["spec_id"]

    if business == "reinvestigate_recovery":
        reason = _recovery_case_reason(fixture)
        (spec,) = [
            s for s in specs_for("reinvestigate_recovery", state)
            if s["allowed_reason_codes"] == [reason]
        ]
        command = _command_for_spec(
            spec, aggregate_id=entity_id, expected_version=version,
            idempotency_key=key, decision_action="reinvestigate_recovery",
            reason_code=reason,
        )
        return command, GuardFacts(guard_values), spec["spec_id"]

    if business == "propose_replacement_plan":
        reason = _recovery_case_reason(fixture)
        (spec,) = [
            s for s in specs_for("replace_recovery_proposal", state)
            if s["allowed_reason_codes"] == [reason]
        ]
        command = _command_for_spec(
            spec, aggregate_id=entity_id, expected_version=version,
            idempotency_key=key, decision_action="replace_recovery_proposal",
            reason_code=reason,
        )
        return command, GuardFacts(guard_values), spec["spec_id"]

    if business == "record_plan" or business == "record_plan_ready":
        raise AssertionError("handled above")

    raise KeyError(f"no resolver reading for business command {business!r}")


#: The stop reason a seeded recovery case carries into a scenario. Scenarios
#: never seed a reason, so a blocked case in a scenario readback is the
#: stale-policy case the `start_revoked` chain would have produced.
_SCENARIO_CASE_REASON = "RECOVERY_POLICY_STALE"


def _recovery_case_reason(fixture: dict[str, Any]) -> str:
    return _SCENARIO_CASE_REASON
