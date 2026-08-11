"""The DAL audit chain.

Decision D4: the chain format is the Finance service's, field for field, so a
restore drill verifies both databases with one algorithm. The reasoning is
copied along with it — the chain detects accidental tampering, middle deletions
and gaps, and the anchor detects truncation of the tail. Neither is a defence
against an attacker who already holds root and the keys, and treating them as
one would be worse than not having them.

The append itself is deliberately not clever: it reads the tail, refuses to
proceed if the tail and its independent witness disagree, and never rebuilds a
missing witness from the table it is supposed to witness. Rebuilding would
bless a truncated prefix on the very next append.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import to_rfc3339

from personal_agent_dal.storage.models import AuditChainAnchor, AuditEvent


class AuditChainIntegrityError(RuntimeError):
    """The audit tail and its independent witness disagree."""


def compute_event_hash(
    *,
    prev_hash: str | None,
    trace_id: str,
    event_type: str,
    redacted_summary: str,
    created_at: datetime,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "prev_hash": prev_hash,
                "trace_id": trace_id,
                "event_type": event_type,
                "redacted_summary": redacted_summary,
                "created_at": to_rfc3339(created_at),
            }
        ).encode("utf-8")
    ).hexdigest()


def append_audit_event(
    session: Session,
    *,
    event_id: str,
    trace_id: str,
    event_type: str,
    redacted_summary: str,
    now: datetime,
) -> AuditEvent:
    """Append one row and advance the witness, inside the caller's transaction.

    `redacted_summary` is the only free-text field, and it is the caller's job
    to keep it free of payloads, secrets and resource identifiers — an audit
    trail that quotes what it is auditing becomes the leak it was meant to
    record.
    """
    previous = session.scalars(
        select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
    ).first()
    prev_hash = previous.event_hash if previous else None
    anchor = session.get(AuditChainAnchor, 1)

    if previous is None:
        if anchor is not None:
            raise AuditChainIntegrityError(
                "audit anchor exists but the audit trail is empty"
            )
    elif anchor is None or anchor.tail_hash != prev_hash:
        raise AuditChainIntegrityError(
            "audit tail does not match its independent anchor"
        )

    entry = AuditEvent(
        event_id=event_id,
        trace_id=trace_id,
        event_type=event_type,
        redacted_summary=redacted_summary,
        prev_hash=prev_hash,
        event_hash=compute_event_hash(
            prev_hash=prev_hash,
            trace_id=trace_id,
            event_type=event_type,
            redacted_summary=redacted_summary,
            created_at=now,
        ),
        created_at=now,
    )
    session.add(entry)
    session.flush()

    if anchor is None:
        session.add(
            AuditChainAnchor(
                anchor_id=1,
                event_count=1,
                tail_hash=entry.event_hash,
                updated_at=now,
            )
        )
    else:
        anchor.event_count += 1
        anchor.tail_hash = entry.event_hash
        anchor.updated_at = now
    session.flush()
    return entry


def verify_audit_chain(session: Session) -> list[str]:
    """Return the event ids, or witness codes, of every broken chain property."""
    broken: list[str] = []
    prev_hash: str | None = None
    events = list(
        session.scalars(select(AuditEvent).order_by(AuditEvent.sequence))
    )
    for entry in events:
        expected = compute_event_hash(
            prev_hash=prev_hash,
            trace_id=entry.trace_id,
            event_type=entry.event_type,
            redacted_summary=entry.redacted_summary,
            created_at=entry.created_at,
        )
        if entry.prev_hash != prev_hash or entry.event_hash != expected:
            broken.append(entry.event_id)
        prev_hash = entry.event_hash

    anchor = session.get(AuditChainAnchor, 1)
    if anchor is None:
        if events:
            broken.append("missing_anchor")
    elif anchor.event_count != len(events) or anchor.tail_hash != prev_hash:
        broken.append("anchor_mismatch")
    return broken
