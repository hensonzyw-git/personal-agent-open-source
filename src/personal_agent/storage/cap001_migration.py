"""The data half of the `CAP-001` Timeline migration (design §15).

The Alembic revision owns the DDL; this module owns everything that touches
rows, because the rules here are the ones worth reading and testing on their
own:

- the migration **refuses to start** while any operation is non-terminal. A
  half-finished write whose Timeline moved underneath it is not something to
  reconcile afterwards;
- it verifies a restorable encrypted backup *before* changing anything, so the
  rollback path in `CLAUDE.md` §7 is proven rather than assumed;
- it **never decrypts and re-encrypts** message content. The AAD binds an event
  to its own row id, and the row id does not change, so only relation fields are
  written. The migration first proves that assumption is true of this database
  and stops if it is not (§15.7) -- guessing here would corrupt the archive;
- every pre-migration `conversation_id` resolves only through an HMAC alias, so
  an old client id still works while the table holds no historical identifier
  in the clear; a sealed copy exists only for downgrade, including eventless
  legacy conversations;
- nothing it raises or returns contains message text, a conversation id or key
  material (§15.10).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Connection, text

from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent_core.crypto import CryptoError, KeyRing


EVENT_TABLE: Final[str] = "conversation_events"
CONTENT_COLUMN: Final[str] = "encrypted_content"
LEGACY_ID_COLUMN: Final[str] = "encrypted_legacy_conversation_id"
ALIAS_TABLE: Final[str] = "conversation_aliases"
ALIAS_LEGACY_ID_COLUMN: Final[str] = "encrypted_legacy_conversation_id"

#: States after which no further transition is allowed. Duplicated as a literal
#: tuple rather than imported from `models`, because a migration must keep
#: working against the schema of its own moment even if the model file moves on.
TERMINAL_STATES: Final[tuple[str, ...]] = (
    "succeeded",
    "failed_safe",
    "needs_manual_review",
    "cancelled_pre_submit",
)


class Cap001MigrationError(RuntimeError):
    """The migration cannot proceed safely.

    Messages here are read by an operator at a terminal and may be logged, so
    they carry counts, table names and HMAC fingerprints only.
    """


@dataclass(frozen=True)
class MigrationInputs:
    """What the operator must supply to migrate a database that has history."""

    keyring: KeyRing
    identifier_key: HmacKey | HmacKeyRing
    backup_path: Path | None = None


@dataclass(frozen=True)
class LegacyEvent:
    event_id: str
    conversation_id: str
    created_at: str
    operation_id: str | None


@dataclass(frozen=True)
class MigrationPlan:
    """The complete decision, computed before a single row is written."""

    canonical_id: str
    ordered_events: tuple[LegacyEvent, ...]
    legacy_conversation_ids: tuple[str, ...]
    session_by_conversation: dict[str, str] = field(default_factory=dict)
    turn_by_event: dict[str, str] = field(default_factory=dict)

    @property
    def event_count(self) -> int:
        return len(self.ordered_events)


# -- fingerprints ---------------------------------------------------------


def alias_hmac(
    identifier_key: HmacKey | HmacKeyRing, conversation_id: str
) -> str:
    """The stored form of a pre-migration conversation id.

    Domain-separated so the same secret cannot be made to produce the same digest
    for a different kind of identifier.
    """
    return _hmac(identifier_key, "timeline-alias", conversation_id)


def id_fingerprint(
    identifier_key: HmacKey | HmacKeyRing, purpose: str, value: str
) -> str:
    """A short, non-reversible identifier stand-in for trace and audit."""
    return _hmac(identifier_key, purpose, value)[:16]


def _hmac(key: HmacKey | HmacKeyRing, purpose: str, value: str) -> str:
    if not purpose or not value:
        raise Cap001MigrationError("fingerprint needs a purpose and a value")
    return hmac.new(
        key.secret,
        f"{purpose}\x1f{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# -- preflight ------------------------------------------------------------


def preflight(connection: Connection) -> int:
    """Refuse a database that is not quiescent; return the event count.

    Design §15.1 calls this maintenance mode. A `dispatching` operation whose
    Finance twin may already have written is the exact case where moving its
    Timeline row out from under the recovery scan would turn a recoverable
    write into an unexplainable one.
    """
    placeholders = ", ".join(f"'{state}'" for state in TERMINAL_STATES)
    pending = connection.execute(
        text(
            "SELECT COUNT(*) FROM operations "
            f"WHERE state NOT IN ({placeholders})"
        )
    ).scalar_one()
    if pending:
        raise Cap001MigrationError(
            f"{pending} operation(s) are not in a terminal state; finish or "
            "reconcile them before migrating"
        )
    return connection.execute(
        text(f"SELECT COUNT(*) FROM {EVENT_TABLE}")
    ).scalar_one()


def verify_restore_fixture(
    connection: Connection, backup_path: Path, *, keyring: KeyRing
) -> None:
    """Prove the pre-migration backup opens, matches and decrypts.

    A backup whose existence was never tested is not a rollback path. This
    refuses the live database itself, compares a complete logical digest of
    every application table, and decrypts every backed-up event with the same
    key ring the migration is about to rely on.
    """
    from personal_agent_core.sqlite import create_database_engine

    if not backup_path.exists():
        raise Cap001MigrationError("the pre-migration backup does not exist")
    live_database = connection.engine.url.database
    if live_database and live_database != ":memory:":
        try:
            if backup_path.samefile(Path(live_database)):
                raise Cap001MigrationError(
                    "the backup path is the live database, not a rollback copy"
                )
        except OSError as exc:
            raise Cap001MigrationError(
                "the backup path could not be compared with the live database"
            ) from exc
    live_digest = _archive_digest(connection)
    try:
        engine = create_database_engine(backup_path)
        with engine.connect() as backup:
            backup_digest = _archive_digest(backup)
            rows = backup.execute(
                text(
                    f"SELECT event_id, {CONTENT_COLUMN} FROM {EVENT_TABLE} "
                    "ORDER BY event_id"
                )
            ).all()
    except Cap001MigrationError:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure is a failed fixture
        raise Cap001MigrationError(
            f"the pre-migration backup is not readable: {type(exc).__name__}"
        ) from exc
    if not hmac.compare_digest(backup_digest, live_digest):
        raise Cap001MigrationError(
            "the backup archive does not match the live database"
        )
    for event_id, envelope in rows:
        _decrypt_content(keyring, event_id=event_id, envelope=envelope)


def _archive_digest(connection: Connection) -> str:
    """A deterministic, non-logged identity for the complete rollback state."""
    digest = hashlib.sha256()
    tables = connection.execute(
        text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ).all()
    for table_name, schema_sql in tables:
        quoted = '"' + table_name.replace('"', '""') + '"'
        digest.update(table_name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((schema_sql or "").encode("utf-8"))
        digest.update(b"\x00")
        # All project tables are ordinary SQLite rowid tables. Ordering by
        # rowid makes the digest deterministic without duplicating every
        # table's schema in migration code.
        for row in connection.execute(text(f"SELECT * FROM {quoted} ORDER BY rowid")):
            encoded = json.dumps(
                list(row),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def verify_event_aad(connection: Connection, keyring: KeyRing) -> None:
    """Stop unless every event decrypts under the AAD this migration assumes.

    §15.7 is explicit that a differing AAD contract must halt the migration
    rather than be guessed at. Run before any write, so a mismatch costs
    nothing; the same check runs again afterwards as §15.8's per-row proof.
    """
    rows = connection.execute(
        text(f"SELECT event_id, {CONTENT_COLUMN} FROM {EVENT_TABLE}")
    ).all()
    for event_id, envelope in rows:
        _decrypt_content(keyring, event_id=event_id, envelope=envelope)


def _decrypt_content(keyring: KeyRing, *, event_id: str, envelope: Any) -> bytes:
    try:
        parsed = json.loads(envelope) if isinstance(envelope, str) else envelope
        return keyring.decrypt(
            parsed,
            table=EVENT_TABLE,
            column=CONTENT_COLUMN,
            row_id=event_id,
        )
    except (CryptoError, ValueError, TypeError) as exc:
        # Deliberately no event id and no plaintext in the message.
        raise Cap001MigrationError(
            "an event did not decrypt under the AAD this migration assumes "
            f"({EVENT_TABLE}, {CONTENT_COLUMN}, event row id); the actual AAD "
            "contract differs and the migration must not guess"
        ) from exc


# -- planning -------------------------------------------------------------


def build_plan(connection: Connection) -> MigrationPlan:
    """Decide the whole merge before writing anything.

    The order is `(created_at, event_id)` exactly as §15.3 fixes it. Two events
    written in the same millisecond by two devices would otherwise be ordered by
    whatever SQLite returned that day, and the pagination contract would drift
    between the migration and a later re-run.
    """
    rows = connection.execute(
        text(
            "SELECT event_id, conversation_id, created_at, operation_id "
            f"FROM {EVENT_TABLE} ORDER BY created_at, event_id"
        )
    ).all()
    ordered = tuple(
        LegacyEvent(
            event_id=row[0],
            conversation_id=row[1],
            created_at=row[2],
            operation_id=row[3],
        )
        for row in rows
    )
    conversations = tuple(
        row[0]
        for row in connection.execute(
            text(
                "SELECT conversation_id FROM conversations "
                "ORDER BY created_at, conversation_id"
            )
        ).all()
    )
    session_by_conversation = {
        conversation_id: f"ses_{uuid.uuid4().hex}"
        for conversation_id in conversations
    }
    # One turn per operation, so a user message and the result of the operation
    # it started are deterministically the same turn (§15.6). An event with no
    # operation is its own turn.
    turn_by_operation: dict[str, str] = {}
    turn_by_event: dict[str, str] = {}
    for event in ordered:
        if event.operation_id is None:
            turn_by_event[event.event_id] = f"trn_{uuid.uuid4().hex}"
            continue
        turn = turn_by_operation.setdefault(
            event.operation_id, f"trn_{uuid.uuid4().hex}"
        )
        turn_by_event[event.event_id] = turn
    return MigrationPlan(
        canonical_id=f"tl_{uuid.uuid4().hex}",
        ordered_events=ordered,
        legacy_conversation_ids=conversations,
        session_by_conversation=session_by_conversation,
        turn_by_event=turn_by_event,
    )


# -- application ----------------------------------------------------------


def apply_plan(
    connection: Connection,
    plan: MigrationPlan,
    *,
    keyring: KeyRing,
    identifier_key: HmacKey | HmacKeyRing,
    now: datetime,
) -> None:
    """Create the canonical Timeline, the legacy Sessions and the aliases."""
    stamp = _rfc3339(now)
    connection.execute(
        text(
            "INSERT INTO conversations "
            "(conversation_id, created_at, last_event_at, next_sequence, "
            " is_canonical) "
            "VALUES (:cid, :created, NULL, 1, 1)"
        ),
        {"cid": plan.canonical_id, "created": stamp},
    )

    for conversation_id in plan.legacy_conversation_ids:
        # Every legacy conversation becomes one *closed* Session: those
        # segments are finished history and must never accept a new event.
        connection.execute(
            text(
                "INSERT INTO context_sessions "
                "(session_id, conversation_id, status, boundary_reason, "
                " parent_session_id, relation_kind, classifier_version, "
                " opened_at, closed_at, last_event_at) "
                "VALUES (:sid, :cid, 'closed', NULL, NULL, 'legacy', NULL, "
                " :opened, :closed, :last)"
            ),
            {
                "sid": plan.session_by_conversation[conversation_id],
                "cid": plan.canonical_id,
                "opened": stamp,
                "closed": stamp,
                "last": stamp,
            },
        )
        stored_alias = alias_hmac(identifier_key, conversation_id)
        sealed_alias = keyring.encrypt(
            conversation_id.encode("utf-8"),
            table=ALIAS_TABLE,
            column=ALIAS_LEGACY_ID_COLUMN,
            row_id=stored_alias,
        )
        connection.execute(
            text(
                "INSERT INTO conversation_aliases "
                "(alias_hmac, conversation_id, "
                " encrypted_legacy_conversation_id, created_at) "
                "VALUES (:alias, :cid, :legacy, :created)"
            ),
            {
                "alias": stored_alias,
                "cid": plan.canonical_id,
                "legacy": json.dumps(
                    sealed_alias, ensure_ascii=False, sort_keys=True
                ),
                "created": stamp,
            },
        )

    last_created: str | None = None
    for sequence, event in enumerate(plan.ordered_events, start=1):
        sealed = keyring.encrypt(
            event.conversation_id.encode("utf-8"),
            table=EVENT_TABLE,
            column=LEGACY_ID_COLUMN,
            row_id=event.event_id,
        )
        connection.execute(
            text(
                f"UPDATE {EVENT_TABLE} SET "
                "conversation_id = :cid, "
                "timeline_sequence = :seq, "
                "session_id = :sid, "
                "turn_id = :turn, "
                f"{LEGACY_ID_COLUMN} = :legacy "
                "WHERE event_id = :eid"
            ),
            {
                "cid": plan.canonical_id,
                "seq": sequence,
                "sid": plan.session_by_conversation[event.conversation_id],
                "turn": plan.turn_by_event[event.event_id],
                "legacy": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                "eid": event.event_id,
            },
        )
        last_created = event.created_at

    connection.execute(
        text(
            "UPDATE conversations SET next_sequence = :next, "
            "last_event_at = :last WHERE conversation_id = :cid"
        ),
        {
            "next": plan.event_count + 1,
            "last": last_created,
            "cid": plan.canonical_id,
        },
    )
    if plan.legacy_conversation_ids:
        connection.execute(
            text(
                "DELETE FROM conversations WHERE conversation_id <> :cid"
            ),
            {"cid": plan.canonical_id},
        )


def verify_upgrade(
    connection: Connection,
    plan: MigrationPlan,
    *,
    keyring: KeyRing,
    identifier_key: HmacKey | HmacKeyRing,
) -> None:
    """Re-derive the outcome from the database, not from the plan in memory."""
    count = connection.execute(
        text(f"SELECT COUNT(*) FROM {EVENT_TABLE}")
    ).scalar_one()
    if count != plan.event_count:
        raise Cap001MigrationError(
            f"event count changed during migration: {plan.event_count} before, "
            f"{count} after"
        )

    rows = connection.execute(
        text(
            "SELECT event_id, conversation_id, timeline_sequence, session_id, "
            f"turn_id, operation_id, {CONTENT_COLUMN}, {LEGACY_ID_COLUMN} "
            f"FROM {EVENT_TABLE} ORDER BY timeline_sequence"
        )
    ).all()
    expected_operations = {
        event.event_id: event.operation_id for event in plan.ordered_events
    }
    for index, row in enumerate(rows, start=1):
        (
            event_id,
            conversation_id,
            sequence,
            session_id,
            turn_id,
            operation_id,
            content,
            legacy,
        ) = row
        if sequence != index:
            raise Cap001MigrationError(
                "timeline_sequence is not a gapless strictly increasing run"
            )
        if conversation_id != plan.canonical_id:
            raise Cap001MigrationError(
                "an event was not moved onto the canonical Timeline"
            )
        if not session_id or not turn_id:
            raise Cap001MigrationError(
                "an event has no session or turn after migration"
            )
        if operation_id != expected_operations[event_id]:
            raise Cap001MigrationError(
                "an event's operation reference changed during migration"
            )
        # Content still opens: the migration must not have touched it.
        _decrypt_content(keyring, event_id=event_id, envelope=content)
        original = keyring.decrypt(
            json.loads(legacy) if isinstance(legacy, str) else legacy,
            table=EVENT_TABLE,
            column=LEGACY_ID_COLUMN,
            row_id=event_id,
        ).decode("utf-8")
        if alias_hmac(identifier_key, original) not in _alias_set(connection):
            raise Cap001MigrationError(
                "an event's legacy conversation id does not resolve to an alias"
            )

    ordered_ids = [row[0] for row in rows]
    if ordered_ids != [event.event_id for event in plan.ordered_events]:
        raise Cap001MigrationError(
            "the migrated order does not match the planned (created_at, "
            "event_id) order"
        )
    canonical = connection.execute(
        text("SELECT COUNT(*) FROM conversations WHERE is_canonical = 1")
    ).scalar_one()
    if canonical != 1:
        raise Cap001MigrationError(
            f"expected exactly one canonical Timeline, found {canonical}"
        )
    expected_aliases = {
        alias_hmac(identifier_key, conversation_id)
        for conversation_id in plan.legacy_conversation_ids
    }
    if _alias_set(connection) != expected_aliases:
        raise Cap001MigrationError(
            "legacy conversation aliases do not match the migration plan"
        )
    alias_rows = connection.execute(
        text(
            "SELECT alias_hmac, encrypted_legacy_conversation_id "
            "FROM conversation_aliases"
        )
    ).all()
    recovered_aliases = {
        alias_hmac(
            identifier_key,
            _decrypt_legacy_alias(
                keyring, alias=stored_alias, envelope=envelope
            ),
        )
        for stored_alias, envelope in alias_rows
    }
    if recovered_aliases != expected_aliases:
        raise Cap001MigrationError(
            "legacy conversation recovery material does not match its aliases"
        )


def _alias_set(connection: Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            text("SELECT alias_hmac FROM conversation_aliases")
        ).all()
    }


# -- downgrade ------------------------------------------------------------


def restore_legacy_conversations(
    connection: Connection, *, keyring: KeyRing, now: datetime
) -> None:
    """Rebuild the pre-`CAP-001` conversations from the sealed legacy ids.

    Events appended *after* the migration have no legacy id; they stay on the
    canonical conversation, which the older code reads as an ordinary
    conversation. The requirement §15.9 states is that the original history
    remains readable, not that derived checkpoints survive.
    """
    stamp = _rfc3339(now)
    alias_rows = connection.execute(
        text(
            "SELECT alias_hmac, encrypted_legacy_conversation_id, created_at "
            "FROM conversation_aliases "
            "WHERE encrypted_legacy_conversation_id IS NOT NULL"
        )
    ).all()
    last_by_conversation: dict[str, str] = {
        _decrypt_legacy_alias(keyring, alias=alias, envelope=envelope): created_at
        for alias, envelope, created_at in alias_rows
    }
    rows = connection.execute(
        text(
            f"SELECT event_id, {LEGACY_ID_COLUMN}, created_at "
            f"FROM {EVENT_TABLE} WHERE {LEGACY_ID_COLUMN} IS NOT NULL"
        )
    ).all()
    restored: dict[str, str] = {}
    for event_id, legacy, created_at in rows:
        original = keyring.decrypt(
            json.loads(legacy) if isinstance(legacy, str) else legacy,
            table=EVENT_TABLE,
            column=LEGACY_ID_COLUMN,
            row_id=event_id,
        ).decode("utf-8")
        restored[event_id] = original
        current = last_by_conversation.get(original)
        if current is None or created_at > current:
            last_by_conversation[original] = created_at

    for conversation_id, last_event_at in sorted(last_by_conversation.items()):
        connection.execute(
            text(
                "INSERT INTO conversations "
                "(conversation_id, created_at, last_event_at) "
                "VALUES (:cid, :created, :last)"
            ),
            {
                "cid": conversation_id,
                "created": stamp,
                "last": last_event_at,
            },
        )
    for event_id, conversation_id in restored.items():
        connection.execute(
            text(
                f"UPDATE {EVENT_TABLE} SET conversation_id = :cid "
                "WHERE event_id = :eid"
            ),
            {"cid": conversation_id, "eid": event_id},
        )
    # Keep the canonical row only when it owns events appended after CAP-001.
    # If every event was restored (including the eventless-history case), the
    # row is a migration artefact and the downgrade should restore the original
    # conversation set exactly.
    connection.execute(
        text(
            "DELETE FROM conversations WHERE is_canonical = 1 "
            "AND NOT EXISTS ("
            " SELECT 1 FROM conversation_events "
            " WHERE conversation_events.conversation_id = "
            " conversations.conversation_id"
            ")"
        )
    )


def _decrypt_legacy_alias(
    keyring: KeyRing, *, alias: str, envelope: Any
) -> str:
    try:
        parsed = json.loads(envelope) if isinstance(envelope, str) else envelope
        return keyring.decrypt(
            parsed,
            table=ALIAS_TABLE,
            column=ALIAS_LEGACY_ID_COLUMN,
            row_id=alias,
        ).decode("utf-8")
    except (CryptoError, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise Cap001MigrationError(
            "a legacy alias recovery envelope could not be decrypted"
        ) from exc


def _rfc3339(moment: datetime) -> str:
    from personal_agent_core.timeutil import to_rfc3339

    return to_rfc3339(moment)
