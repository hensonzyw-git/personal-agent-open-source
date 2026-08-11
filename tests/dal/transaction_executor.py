"""Executes frozen DAL-010 transaction-atomicity fixtures against a real engine.

`DAL-T-TX-001` proves the transaction boundary is real: an operation that
writes five members (aggregate, business_event, transition_receipt, audit,
notification_outbox) in one serializable unit, and fails at any one of them,
must leave the database exactly where it started. The oracle for every variant
is zero-write, state ``planning -> planning``, no receipt, no event — the
partial writes before the failure are not durable.

The executor does not simulate the rollback. It writes real rows into a real
SQLite transaction, raises at the injected ``error`` member, and lets
``run_write_transaction`` discard the unit. The write set is measured from the
database before and after, so a transaction that silently kept its partial
writes is visible as a non-empty write set and fails the oracle.

Arrangement and judgement stay apart: seeding builds the feature at the
fixture's pre-state; the write set is measured independently of what the
transaction attempted. The persisted-content check confirms no receipt row
survives and the feature row is unchanged — a transaction that rolled back the
data rows but left a receipt or bumped the version would still pass the write
set if the receipt table were not in the counted set, so the content check is
the independent second opinion.

Test-only module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


class TransactionMemberError(RuntimeError):
    """The injected failure at one transaction member.

    This is the error the contract's ``injected_results`` declares: one member
    returns ``error`` from the database, and the transaction must discard
    everything written before it. It is raised inside the transaction body so
    ``run_write_transaction`` rolls back the unit.
    """


#: The five members a TX-001 operation writes, in the order the fixtures
#: declare them. Each maps to one physical write the executor performs inside
#: the transaction.
_TRANSACTION_MEMBERS: tuple[str, ...] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
    "notification_outbox",
)


def _seed_feature(engine: Any, fixture_body: dict[str, Any]) -> None:
    """Build the feature at the fixture's pre-state.

    Arrangement only: the feature exists at ``planning`` v7, exactly as the
    fixture declares. The transaction under test will attempt to move it; the
    rollback must leave it here.
    """
    from tests.dal.factories import feature_row

    first = fixture_body["operation_sequence"][0]
    target = first["input"]["target"]
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id=target["entity_id"],
                version=target["version"],
                state=target["state"],
            )
        )


def _write_member(
    session: Any,
    member: str,
    target: dict[str, Any],
    idempotency_key: str,
) -> None:
    """Perform the physical write for one transaction member.

    Each member writes a real row into the table it names. The rows are
    syntactically valid placeholders — the content does not matter, only that
    the write happened and must be rolled back. A member that writes nothing
    would let a broken transaction pass by never staging any work to discard.
    """
    from tests.dal.factories import EMPTY_SHA256

    from personal_agent_dal.storage.machine_models import TransitionReceipt
    from personal_agent_dal.storage.models import (
        AuditEvent,
        Event,
        OutboxEvent,
    )

    feature_id = target["entity_id"]
    now = utc_now()

    if member == "aggregate":
        # Bump the feature's state and version — the write the rollback must
        # undo. Using a raw UPDATE so the change is flushable within the
        # transaction without a separate ORM load.
        session.execute(
            text(
                "UPDATE features SET state = 'coding', version = version + 1, "
                "updated_at = :now WHERE feature_id = :fid"
            ).bindparams(now=now, fid=feature_id)
        )
    elif member == "business_event":
        session.add(
            Event(
                event_id=f"event-{idempotency_key}",
                schema_version="dal.event/1.0",
                event_type="feature.state_changed",
                aggregate_type="feature",
                aggregate_id=feature_id,
                aggregate_version=target["version"] + 1,
                command_id=None,
                causation_id=None,
                correlation_id=None,
                actor_type="service",
                actor_id="workflow-service",
                occurred_at=now,
                encrypted_payload=None,
                payload_sha256=EMPTY_SHA256,
            )
        )
    elif member == "transition_receipt":
        session.add(
            TransitionReceipt(
                receipt_id=f"receipt-{idempotency_key}",
                idempotency_key=idempotency_key,
                aggregate_type="feature",
                aggregate_id=feature_id,
                aggregate_version=target["version"] + 1,
                spec_id="SM-START-PROVIDER",
                command_type="start_provider",
                from_state=target["state"],
                to_state="coding",
                receipt_code="APPLIED",
                receipt_schema_version="dal.receipt.transition.feature/1.0",
                request_payload_sha256=EMPTY_SHA256,
                event_id=None,
                recorded_at=now,
            )
        )
    elif member == "audit":
        session.add(
            AuditEvent(
                event_id=f"audit-{idempotency_key}",
                trace_id=f"trace-{feature_id}",
                event_type="transaction.member_written",
                redacted_summary=f"member={member}",
                prev_hash=None,
                event_hash=EMPTY_SHA256,
                created_at=now,
            )
        )
    elif member == "notification_outbox":
        session.add(
            OutboxEvent(
                outbox_id=f"outbox-{idempotency_key}",
                aggregate_type="feature",
                aggregate_id=feature_id,
                aggregate_version=target["version"] + 1,
                topic="feature.state_changed",
                payload_sha256=EMPTY_SHA256,
                delivery_state="pending",
                available_at=now,
                attempt_count=0,
                created_at=now,
            )
        )
    else:  # pragma: no cover — the fixture's action_sequence is the only source
        raise TransactionMemberError(f"unknown transaction member: {member!r}")


def _injected_error_member(operation: dict[str, Any]) -> str | None:
    """The member whose injected result is ``error``, or None if all staged.

    The fixture declares one ``injected_results`` entry per member. A status
    of ``error`` means the database reports a failure writing that member —
    the transaction must roll back. The executor raises at that member.
    """
    injected = operation["input"].get("injected_results") or []
    for entry in injected:
        if entry["status"] == "error":
            return entry["member"]
    return None


def _state_snapshot(engine: Any) -> dict[str, Any]:
    """Everything a TX-001 operation could change, as plain values."""
    tables = set(inspect(engine).get_table_names())
    counts: dict[str, int] = {}
    feature: dict[str, Any] | None = None
    with engine.connect() as connection:
        for table in (
            "features",
            "events",
            "transition_receipts",
            "audit_events",
            "outbox_events",
        ):
            if table in tables:
                counts[table] = connection.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 — schema names
                ).scalar_one()
        if "features" in tables:
            row = connection.execute(
                text(
                    "SELECT state, version FROM features LIMIT 1"
                )
            ).first()
            if row is not None:
                feature = {"state": row[0], "version": row[1]}
    return {"counts": counts, "feature": feature}


def _observed_writes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The write classes derived from what the database actually changed."""
    labels: set[str] = set()

    _TABLE_LABELS: dict[str, str] = {
        "events": "business_event",
        "transition_receipts": "transition_receipt",
        "audit_events": "audit",
        "outbox_events": "notification_outbox",
    }
    for table, label in _TABLE_LABELS.items():
        if after["counts"].get(table, 0) > before["counts"].get(table, 0):
            labels.add(label)

    if before["feature"] is not None and after["feature"] is not None:
        if (
            before["feature"]["state"] != after["feature"]["state"]
            or before["feature"]["version"] != after["feature"]["version"]
        ):
            labels.add("aggregate")

    return sorted(labels)


def execute_transaction_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run the transaction, and measure the write set from the database.

    The transaction writes all five members in order. If the fixture injects
    an ``error`` at one member, the executor raises inside the transaction
    body and ``run_write_transaction`` rolls back the unit. The trace records
    the refusal; the write set — measured from the database — must be empty.
    """
    engine = create_database_engine(database)
    db.upgrade(engine)
    _seed_feature(engine, fixture_body)

    operation = fixture_body["operation_sequence"][0]
    target = operation["input"]["target"]
    idempotency_key = operation["idempotency_key"]
    error_member = _injected_error_member(operation)

    trace = ExecutionTrace(probe=probe)
    trace.state_trace.append(target["state"])

    before = _state_snapshot(engine)
    sessions = session_factory(engine)

    raised = False
    try:
        with sessions() as session:
            run_write_transaction(
                session,
                lambda: _run_transaction_body(
                    session, operation, target, idempotency_key, error_member
                ),
            )
    except TransactionMemberError:
        raised = True

    after = _state_snapshot(engine)
    trace.write_set.extend(_observed_writes(before, after))
    trace.declared_write_set.extend(trace.write_set)

    if raised:
        # The transaction was refused: no receipt, no state change. The oracle
        # expects exactly this — zero-write, planning -> planning, no receipt.
        # A rolled-back transaction produces no durable record; appending one
        # here would invent a receipt the contract does not have.
        trace.final_state = target["state"]
        trace.state_trace.append(target["state"])
        trace.final_entity_type = "feature"
    else:
        # Every member staged successfully — no frozen TX-001 variant reaches
        # here, but if one did the trace records the applied outcome and the
        # comparator fails it against the zero-write oracle.
        trace.receipts.append(
            ReceiptRecord(
                code="APPLIED",
                schema_version="dal.operation-receipt/1.0",
            )
        )
        post = after["feature"]
        trace.final_state = post["state"] if post else target["state"]
        trace.state_trace.append(trace.final_state)
        trace.final_entity_type = "feature"

    engine.dispose()
    return trace


def _run_transaction_body(
    session: Any,
    operation: dict[str, Any],
    target: dict[str, Any],
    idempotency_key: str,
    error_member: str | None,
) -> None:
    """Write every member in order, raising at the injected error member.

    The members are written in the fixture's declared order. Each writes a
    real row. If this member is the injected error, the raise happens *after*
    staging the write — so the row is in the transaction and the rollback
    must discard it. Raising before the write would let a broken transaction
    pass by never staging the failing member at all.
    """
    actions = operation["input"]["action_sequence"]
    for step in sorted(actions, key=lambda s: s["order"]):
        member = step["member"]
        _write_member(session, member, target, idempotency_key)
        if error_member is not None and member == error_member:
            raise TransactionMemberError(
                f"injected failure at member {member!r}"
            )


def transaction_persisted_divergences(
    database: Path, fixture_body: dict[str, Any]
) -> list[str]:
    """Divergences between the expected post-rollback state and what is.

    After a rolled-back transaction the database must be exactly where the
    fixture started: no new receipt, no new event, no new audit row, no new
    outbox row, and the feature still at its pre-state and pre-version. A
    transaction that rolled back the data rows but left a receipt — or one
    that bumped the version and then rolled back only some tables — is caught
    here, independently of the write-set labels.
    """
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        first = fixture_body["operation_sequence"][0]
        target = first["input"]["target"]
        feature_id = target["entity_id"]
        pre_state = target["state"]
        pre_version = target["version"]

        with engine.connect() as connection:
            # The feature must be unchanged.
            row = connection.execute(
                text(
                    "SELECT state, version FROM features WHERE feature_id = :fid"
                ).bindparams(fid=feature_id)
            ).first()
            if row is None:
                problems.append("feature row missing after rollback")
            else:
                if row[0] != pre_state:
                    problems.append(
                        f"feature state after rollback: expected {pre_state!r}, "
                        f"got {row[0]!r}"
                    )
                if row[1] != pre_version:
                    problems.append(
                        f"feature version after rollback: expected {pre_version}, "
                        f"got {row[1]}"
                    )

            # No transaction member may have persisted a row.
            for table, label in (
                ("events", "business_event"),
                ("transition_receipts", "transition_receipt"),
                ("audit_events", "audit"),
                ("outbox_events", "notification_outbox"),
            ):
                count = connection.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 — schema names
                ).scalar_one()
                if count != 0:
                    problems.append(
                        f"{label} rows after rollback: expected 0, got {count}"
                    )
    finally:
        engine.dispose()
    return problems
