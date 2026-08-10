"""`OP-DB-CONTRACT-001`: the database contract operations (DAL-008).

One command, one receipt. The frozen operation spec carries a whole
`action_sequence` per command, so the receipt belongs to the command and the
handlers below contribute writes and events to it rather than each emitting one
of their own.

Three properties of this module exist because getting them wrong is expensive
and invisible:

- **A refusal writes nothing.** `cas_conflict` and `idempotency_unique` both
  have an empty `allowed_write_set`. The whole unit runs inside one real
  transaction (`create_database_engine` emits its own `BEGIN`), so a handler
  that raises leaves the database exactly as it found it — no partial write, no
  audit row, no compensating delete needed.
- **Current state is read through Core, not the ORM.** `Session.get()` returns
  the identity-map copy, which cannot see what another session has already
  committed. Every pre-CAS read and every conflict check here selects columns
  through the Core table (CLAUDE.md §5.2).
- **The two conflict codes stay distinct.** `VERSION_CONFLICT` says re-read and
  retry; `IDEMPOTENCY_CONFLICT` says this key is already spent on different
  content and retrying will never help.

Handlers share one signature, `(session, action, context)`, including the ones
that ignore an argument. A dispatcher whose functions have drifting signatures
cannot be checked statically, and the adapter that bridges the difference is
where the bug hides.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final

from sqlalchemy import Engine, delete, inspect, select, text, update
from sqlalchemy.orm import Session

from personal_agent_core.crypto import KeyRing
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import check_integrity
from personal_agent_core.timeutil import parse_rfc3339, utc_now

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import (
    OperationReceipt,
    ReceiptCode,
)
from personal_agent_dal.storage import db
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.models import (
    Event,
    Feature,
    MigrationReceipt,
    OperationEvent,
    OperationReceiptRow,
    RetentionTombstone,
)


OPERATION_SPEC_ID: Final[str] = "OP-DB-CONTRACT-001"
COMMAND_TYPE: Final[str] = "apply_database_contract"
POLICY_VERSION: Final[str] = "dal-policy/1.0"
SERVICE_ACTOR: Final[str] = "workflow-service"
FEATURE_SCHEMA_VERSION: Final[str] = "dal.feature-state/1.0"
EVENT_SCHEMA_VERSION: Final[str] = "dal.event/1.0"

#: The table the retention operation prunes. `events` is the one table in this
#: revision that grows without bound and can be rebuilt from the aggregate and
#: the audit trail, which is exactly what makes it the right thing to expire
#: (design draft §8). Audit, approvals and external effects are never expired.
RETENTION_TABLE: Final[str] = "events"

#: The sealed column, per decision D2/E1.
SEALED_TABLE: Final[str] = "events"
SEALED_COLUMN: Final[str] = "encrypted_payload"


@dataclass(frozen=True)
class OperationOutcome:
    """What one command did: its receipt, its writes and its events.

    `writes` carries the oracle's write-class vocabulary rather than table
    names — `schema` is not a table, and one class can span a delete and its
    tombstone. The mapping is fixed in design draft §3.
    """

    receipt: OperationReceipt
    writes: tuple[str, ...]
    events: tuple[str, ...]
    entity_state: str


@dataclass(frozen=True)
class SealOutcome:
    """The result of one sealed-column round trip."""

    event_id: str
    roundtrip_matches: bool


@dataclass
class ActionContext:
    """Everything a handler may read, plus a scratchpad shared between them."""

    engine: Engine
    facts: dict[str, Any]
    target: dict[str, Any]
    keyring: KeyRing | None
    now: datetime
    operation_id: str
    idempotency_key: str
    actor_type: str
    evidence_source_type: str
    request_payload_sha256: str
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    """One action's contribution to the command's outcome."""

    writes: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    audit_summary: str | None = None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_digest(payload: Any) -> str:
    return _sha256_text(canonical_json(payload))


def _sealed_id(table_name: str, row_id: str) -> str:
    """A tombstone's stable, non-reversing reference to a deleted row.

    The identifiers are already opaque, so a digest of `(table, row_id)` is
    enough to answer "was this row deleted" without keeping anything that could
    be read back — which would defeat the deletion it records.
    """
    return _payload_digest({"table": table_name, "row_id": row_id})


# --------------------------------------------------------------------------
# Action handlers. Uniform signature: (session, action, context).
# --------------------------------------------------------------------------


def _handle_update_aggregate(
    session: Session, action: dict[str, Any], context: ActionContext
) -> ActionResult:
    """Compare-and-swap the feature aggregate, or refuse."""
    expected_version = action["expected_version"]
    changed = _cas_feature(
        session,
        feature_id=context.target["entity_id"],
        expected_version=expected_version,
        new_state=action.get("new_state", "planning"),
        now=context.now,
    )
    if not changed:
        raise DalError(
            DalErrorCode.STALE_VERSION,
            internal_detail=(
                f"compare-and-swap missed on expected version {expected_version}"
            ),
        )
    return ActionResult(
        writes=("feature_state",), audit_summary="feature state advanced"
    )


def _handle_insert_operation_receipt(
    session: Session, action: dict[str, Any], context: ActionContext
) -> ActionResult:
    """Claim an idempotency key, or refuse if it is spent on other content."""
    key = action["idempotency_key"]
    submitted = context.facts.get(
        "submitted_payload_sha256", context.request_payload_sha256
    )
    existing = _existing_receipt_digest(session, key)
    if existing is not None and existing != submitted:
        raise DalError(
            DalErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail="idempotency key reused with different content",
        )
    if existing is not None:
        # A true replay: the key and the content both match, so the original
        # receipt already represents this request. Nothing more to write.
        return ActionResult()
    session.add(
        OperationReceiptRow(
            operation_id=new_id(),
            idempotency_key=key,
            operation_spec_id=OPERATION_SPEC_ID,
            command_type=COMMAND_TYPE,
            actor_type=context.actor_type,
            evidence_source_type=context.evidence_source_type,
            receipt_code=ReceiptCode.APPLIED.value,
            receipt_schema_version=OperationReceipt(ReceiptCode.APPLIED).schema_version,
            request_payload_sha256=submitted,
            response_payload_sha256=None,
            recorded_at=context.now,
        )
    )
    session.flush()
    return ActionResult(
        writes=("operation_receipt",), audit_summary="idempotency key claimed"
    )


def _handle_apply_retention(
    session: Session, action: dict[str, Any], context: ActionContext
) -> ActionResult:
    """Delete everything strictly older than the cutoff, and tombstone it."""
    cutoff = parse_rfc3339(action["cutoff"])
    table = Event.__table__
    doomed = list(
        session.execute(
            select(table.c.event_id).where(table.c.occurred_at < cutoff)
        ).scalars()
    )
    if doomed:
        session.execute(delete(table).where(table.c.event_id.in_(doomed)))
    for event_id in doomed:
        session.add(
            RetentionTombstone(
                tombstone_id=new_id(),
                table_name=RETENTION_TABLE,
                sealed_id=_sealed_id(RETENTION_TABLE, event_id),
                cutoff=cutoff,
                policy_version=POLICY_VERSION,
                executed_at=context.now,
                operation_id=context.operation_id,
            )
        )
    context.scratch["deleted_count"] = len(doomed)
    return ActionResult(
        writes=("retention_tombstone",),
        events=("database.retention_applied",),
        audit_summary=f"retention removed {len(doomed)} row(s) from {RETENTION_TABLE}",
    )


def _handle_write_encrypted_record(
    session: Session, action: dict[str, Any], context: ActionContext
) -> ActionResult:
    """Seal a value into the AEAD column, bound to the row that holds it."""
    if context.keyring is None:
        raise DalError(
            DalErrorCode.CONFIG_UNAVAILABLE,
            internal_detail="a sealed write needs a key ring",
        )
    plaintext = action["plaintext_canary"]
    row = _seal_event(session, keyring=context.keyring, plaintext=plaintext,
                      now=context.now)
    context.scratch["sealed_event_id"] = row.event_id
    context.scratch["sealed_plaintext"] = plaintext
    return ActionResult(
        writes=("encrypted_record",), audit_summary="sealed record written"
    )


def _handle_read_encrypted_record(
    session: Session, action: dict[str, Any], context: ActionContext
) -> ActionResult:
    """Read the sealed value back and prove the round trip, or fail closed."""
    if context.keyring is None:
        raise DalError(
            DalErrorCode.CONFIG_UNAVAILABLE,
            internal_detail="a sealed read needs a key ring",
        )
    event_id = context.scratch.get("sealed_event_id")
    if event_id is None:
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT,
            internal_detail="read_encrypted_record without a preceding write",
        )
    session.flush()
    row = session.get(Event, _event_seq(session, event_id))
    if row is None:
        raise DalError(
            DalErrorCode.INTERNAL_ERROR,
            internal_detail="sealed row vanished between write and read",
        )
    recovered = open_event_payload(row, keyring=context.keyring)
    if recovered != context.scratch.get("sealed_plaintext"):
        # Never report a round trip that did not happen: a mismatch here means
        # the AAD binding, the key or the envelope is wrong, and returning
        # APPLIED would certify encryption that does not work.
        raise DalError(
            DalErrorCode.INTERNAL_ERROR,
            internal_detail="sealed round trip did not return the original value",
        )
    return ActionResult(
        events=("database.encrypted_roundtrip",),
        audit_summary="sealed record round-tripped",
    )


#: The dispatcher. Every handler takes `(session, action, context)`, including
#: those that ignore `action`; adapting signatures at the call site is what
#: lets one function quietly stop matching the others.
_ACTION_HANDLERS: Final[
    dict[str, Callable[[Session, dict[str, Any], ActionContext], ActionResult]]
] = {
    "update_aggregate": _handle_update_aggregate,
    "insert_operation_receipt": _handle_insert_operation_receipt,
    "apply_retention": _handle_apply_retention,
    "write_encrypted_record": _handle_write_encrypted_record,
    "read_encrypted_record": _handle_read_encrypted_record,
}


# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------


def _cas_feature(
    session: Session,
    *,
    feature_id: str,
    expected_version: int,
    new_state: str,
    now: datetime,
) -> bool:
    """The one compare-and-swap. Returns whether this caller won.

    A Core `UPDATE ... WHERE version = :expected` is deliberate: it never
    consults the identity map, so it competes against the database's current
    row rather than against whatever this session happens to remember.
    """
    table = Feature.__table__
    result = session.execute(
        update(table)
        .where(table.c.feature_id == feature_id)
        .where(table.c.version == expected_version)
        .values(state=new_state, version=expected_version + 1, updated_at=now)
    )
    return result.rowcount == 1


def _existing_receipt_digest(session: Session, idempotency_key: str) -> str | None:
    """The stored request digest for a key, read straight from the database."""
    table = OperationReceiptRow.__table__
    return session.execute(
        select(table.c.request_payload_sha256).where(
            table.c.idempotency_key == idempotency_key
        )
    ).scalar_one_or_none()


def _event_seq(session: Session, event_id: str) -> int | None:
    table = Event.__table__
    return session.execute(
        select(table.c.seq).where(table.c.event_id == event_id)
    ).scalar_one_or_none()


def _seal_event(
    session: Session, *, keyring: KeyRing, plaintext: str, now: datetime
) -> Event:
    """Insert one event whose payload is sealed and bound to its own row id."""
    event_id = new_id()
    payload = plaintext.encode("utf-8")
    row = Event(
        event_id=event_id,
        schema_version=EVENT_SCHEMA_VERSION,
        event_type="feature.created",
        aggregate_type="feature",
        aggregate_id=event_id,
        aggregate_version=1,
        command_id=None,
        causation_id=None,
        correlation_id=None,
        actor_type="service",
        actor_id=SERVICE_ACTOR,
        occurred_at=now,
        encrypted_payload=keyring.encrypt(
            payload,
            table=SEALED_TABLE,
            column=SEALED_COLUMN,
            row_id=event_id,
        ),
        # Over the plaintext, so the digest survives a key rotation that
        # rewrites every byte of the envelope.
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )
    session.add(row)
    session.flush()
    return row


def open_event_payload(row: Event, *, keyring: KeyRing) -> str:
    """Open a sealed event payload, or fail closed.

    The AAD binds the value to its table, column and row id, so an envelope
    copied to another row does not open. That is the property that stops
    encryption at rest from becoming a value that travels.
    """
    if row.encrypted_payload is None:
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT,
            internal_detail="event carries no sealed payload",
        )
    return keyring.decrypt(
        row.encrypted_payload,
        table=SEALED_TABLE,
        column=SEALED_COLUMN,
        row_id=row.event_id,
    ).decode("utf-8")


def _emit_operation_event(
    session: Session, *, event_type: str, operation_id: str, now: datetime,
    detail: dict[str, Any],
) -> None:
    """Record one service-level operation event (decision G1(a))."""
    session.add(
        OperationEvent(
            operation_event_id=new_id(),
            event_type=event_type,
            operation_id=operation_id,
            occurred_at=now,
            detail=canonical_json(detail),
        )
    )


def _current_revision(engine: Engine) -> str | None:
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def _refusal(code: ReceiptCode, entity_state: str) -> OperationOutcome:
    return OperationOutcome(
        receipt=OperationReceipt(code), writes=(), events=(), entity_state=entity_state
    )


_REFUSAL_CODES: Final[dict[DalErrorCode, ReceiptCode]] = {
    DalErrorCode.STALE_VERSION: ReceiptCode.VERSION_CONFLICT,
    DalErrorCode.IDEMPOTENCY_CONFLICT: ReceiptCode.IDEMPOTENCY_CONFLICT,
    DalErrorCode.CONFIG_POLICY_DENIED: ReceiptCode.POLICY_DENIED,
    DalErrorCode.SCOPE_DENIED: ReceiptCode.POLICY_DENIED,
}


def _dedupe(labels: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for label in labels:
        seen.setdefault(label, None)
    return tuple(seen)


# --------------------------------------------------------------------------
# Command entry points
# --------------------------------------------------------------------------


def apply_database_contract(
    engine: Engine,
    command: dict[str, Any],
    *,
    keyring: KeyRing | None = None,
    now: datetime | None = None,
) -> OperationOutcome:
    """Execute one `OP-DB-CONTRACT-001` command and return its single receipt.

    A command containing `apply_migration` must contain nothing else. That is
    not a simplification: SQLite cannot enrol DDL and a data write in one
    transaction the way the contract's atomic write sets require, so mixing
    them would produce a unit that only looks atomic.
    """
    now = now or utc_now()
    payload = command["input"]
    kinds = [step["command"] for step in payload["action_sequence"]]
    if not kinds:
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT, internal_detail="empty action sequence"
        )

    if "apply_migration" in kinds:
        if len(kinds) != 1:
            raise DalError(
                DalErrorCode.INVALID_ARGUMENT,
                internal_detail="a migration action cannot share its command",
            )
        return _run_migration_command(engine, command, now=now)

    return _run_data_command(engine, command, keyring=keyring, now=now)


def _run_migration_command(
    engine: Engine, command: dict[str, Any], *, now: datetime
) -> OperationOutcome:
    payload = command["input"]
    action = payload["action_sequence"][0]
    from_revision = action.get("from_revision")
    to_revision = action["to_revision"]
    current = _current_revision(engine)

    already_applied = current == to_revision and current != from_revision
    if current != from_revision and not already_applied:
        raise DalError(
            DalErrorCode.STALE_VERSION,
            internal_detail="schema is not at the revision this migration expects",
        )

    if not already_applied:
        db.upgrade(engine, to_revision)
        reached = _current_revision(engine)
        if reached != to_revision:
            raise DalError(
                DalErrorCode.INTERNAL_ERROR,
                internal_detail="migration did not reach the requested revision",
            )
    check_integrity(engine)

    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        if already_applied and _migration_receipt_exists(session, to_revision):
            # The schema is where it should be and the bookkeeping is already
            # there. Re-running is a no-op, not a second application.
            raise DalError(
                DalErrorCode.IDEMPOTENCY_CONFLICT,
                internal_detail="this revision was already applied and recorded",
            )
        session.add(
            MigrationReceipt(
                migration_receipt_id=new_id(),
                from_revision=from_revision,
                to_revision=to_revision,
                applied_at=now,
                applied_by=SERVICE_ACTOR,
                integrity_check_result="ok",
            )
        )
        _emit_operation_event(
            session,
            event_type="database.migrated",
            operation_id=command["operation_id"],
            now=now,
            detail={"from_revision": from_revision, "to_revision": to_revision},
        )
        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=command["operation_id"],
            event_type="database.migrated",
            redacted_summary=f"schema moved to revision {to_revision}",
            now=now,
        )

    return OperationOutcome(
        receipt=OperationReceipt(ReceiptCode.APPLIED),
        writes=("schema", "migration_receipt", "audit"),
        events=("database.migrated",),
        entity_state="migrated",
    )


def _migration_receipt_exists(session: Session, to_revision: str) -> bool:
    table = MigrationReceipt.__table__
    return (
        session.execute(
            select(table.c.migration_receipt_id).where(
                table.c.to_revision == to_revision
            )
        ).first()
        is not None
    )


def _run_data_command(
    engine: Engine,
    command: dict[str, Any],
    *,
    keyring: KeyRing | None,
    now: datetime,
) -> OperationOutcome:
    payload = command["input"]
    entity_state = payload["target"]["state"]
    context = ActionContext(
        engine=engine,
        facts=payload.get("authoritative_facts", {}),
        target=payload["target"],
        keyring=keyring,
        now=now,
        operation_id=command["operation_id"],
        idempotency_key=command["idempotency_key"],
        actor_type=command["actor_type"],
        evidence_source_type=command["evidence_source_type"],
        request_payload_sha256=_payload_digest(payload),
    )

    sessions = session_factory(engine)
    try:
        with sessions() as session, session.begin():
            # The command's own key first: a replay of the whole command must
            # not re-run its handlers, and a key spent on other content must
            # not be re-used, whatever the actions say.
            replay = _existing_receipt_digest(session, context.idempotency_key)
            if replay is not None:
                if replay != context.request_payload_sha256:
                    raise DalError(
                        DalErrorCode.IDEMPOTENCY_CONFLICT,
                        internal_detail="command key reused with different content",
                    )
                return OperationOutcome(
                    receipt=OperationReceipt(ReceiptCode.APPLIED),
                    writes=(),
                    events=(),
                    entity_state=entity_state,
                )

            writes: list[str] = []
            events: list[str] = []
            summaries: list[str] = []
            for step in payload["action_sequence"]:
                handler = _ACTION_HANDLERS.get(step["command"])
                if handler is None:
                    raise DalError(
                        DalErrorCode.INVALID_ARGUMENT,
                        internal_detail=f"no handler for action {step['command']!r}",
                    )
                result = handler(session, step, context)
                writes.extend(result.writes)
                events.extend(result.events)
                if result.audit_summary:
                    summaries.append(result.audit_summary)

            session.add(
                OperationReceiptRow(
                    operation_id=context.operation_id,
                    idempotency_key=context.idempotency_key,
                    operation_spec_id=command["operation_spec_id"],
                    command_type=COMMAND_TYPE,
                    actor_type=context.actor_type,
                    evidence_source_type=context.evidence_source_type,
                    receipt_code=ReceiptCode.APPLIED.value,
                    receipt_schema_version=OperationReceipt(
                        ReceiptCode.APPLIED
                    ).schema_version,
                    request_payload_sha256=context.request_payload_sha256,
                    response_payload_sha256=None,
                    recorded_at=now,
                )
            )
            writes.append("operation_receipt")

            for event_type in events:
                _emit_operation_event(
                    session,
                    event_type=event_type,
                    operation_id=context.operation_id,
                    now=now,
                    detail={"deleted_count": context.scratch.get("deleted_count")}
                    if event_type == "database.retention_applied"
                    else {},
                )

            append_audit_event(
                session,
                event_id=new_id(),
                trace_id=context.operation_id,
                event_type=COMMAND_TYPE,
                redacted_summary="; ".join(summaries) or COMMAND_TYPE,
                now=now,
            )
            writes.append("audit")

    except DalError as error:
        # The transaction is already rolled back: `create_database_engine`
        # emits a real `BEGIN`, so nothing survives and no compensating write
        # is needed. The receipt is the caller's answer and is not persisted.
        return _refusal(
            _REFUSAL_CODES.get(error.code, ReceiptCode.UNKNOWN), entity_state
        )

    return OperationOutcome(
        receipt=OperationReceipt(ReceiptCode.APPLIED),
        writes=_dedupe(writes),
        events=tuple(events),
        entity_state=entity_state,
    )


def update_feature_state(
    engine: Engine,
    *,
    feature_id: str,
    expected_version: int,
    new_state: str,
    now: datetime | None = None,
) -> OperationOutcome:
    """Advance one feature by compare-and-swap, or refuse with a conflict."""
    now = now or utc_now()
    sessions = session_factory(engine)
    try:
        with sessions() as session, session.begin():
            if not _cas_feature(
                session,
                feature_id=feature_id,
                expected_version=expected_version,
                new_state=new_state,
                now=now,
            ):
                raise DalError(
                    DalErrorCode.STALE_VERSION,
                    internal_detail="compare-and-swap missed",
                )
            append_audit_event(
                session,
                event_id=new_id(),
                trace_id=feature_id,
                event_type="feature.state_changed",
                redacted_summary=f"feature advanced to {new_state}",
                now=now,
            )
    except DalError as error:
        return _refusal(
            _REFUSAL_CODES.get(error.code, ReceiptCode.UNKNOWN), "migrated"
        )
    return OperationOutcome(
        receipt=OperationReceipt(ReceiptCode.APPLIED),
        writes=("feature_state", "audit"),
        events=(),
        entity_state="migrated",
    )


def seal_and_reread_event(
    engine: Engine,
    *,
    keyring: KeyRing,
    plaintext: str,
    now: datetime | None = None,
) -> SealOutcome:
    """Seal a value, read it back through a fresh session, and report honestly."""
    now = now or utc_now()
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        row = _seal_event(session, keyring=keyring, plaintext=plaintext, now=now)
        event_id = row.event_id

    with sessions() as session:
        seq = _event_seq(session, event_id)
        stored = session.get(Event, seq) if seq is not None else None
        matches = (
            stored is not None
            and open_event_payload(stored, keyring=keyring) == plaintext
        )
    return SealOutcome(event_id=event_id, roundtrip_matches=matches)
