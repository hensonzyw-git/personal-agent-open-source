"""The pre-write exact duplicate check, and the override that can release it.

This is a *heuristic for humans*, not a safety mechanism, and the distinction
governs the whole module. It exists to catch Henson typing the same expense
twice; it does not replace Host idempotency, the Feishu `client_token`, or
unknown-commit recovery, and design 7.6 rule 8 forbids using it as a substitute
during recovery -- it runs only before an execution first reaches `prepared`.

The match is exact, on the values that will actually be stored (design 7.7):
`occurred_on + amount_cny + name + category`. No normalisation, no similarity,
no amount ranges. Family scope is shown with each candidate because it helps
Henson judge, but it is deliberately *not* part of the key: two entries that
differ only by scope are still worth pausing over.

**Why the model cannot forge an override.** The override is not a flag the
caller asserts. It names a `duplicate_check_id` that only this server creates,
stored with the intent fingerprint it was raised for, a short expiry, and the
exact candidate set that was shown. Releasing re-runs the check and requires the
candidate set to still be identical, so an override cannot be replayed onto a
different entry, reused after the ledger changed, or invented -- there is no
value a model could emit that satisfies a row it never caused to exist.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from personal_agent_core.crypto import KeyRing
from personal_agent_core.manifest import canonical_json
from personal_data_mcp.storage.models import DuplicateCheck


#: Long enough for Henson to read a card and answer, short enough that a stale
#: confirmation cannot be replayed much later.
OVERRIDE_TTL: Final[timedelta] = timedelta(minutes=15)

_TABLE: Final[str] = "duplicate_checks"
_COLUMN: Final[str] = "encrypted_candidate_record_ids"


@dataclass(frozen=True)
class Candidate:
    """One existing row that matches, reduced to what a decision needs."""

    record_id: str
    name: str
    amount_cny: Decimal
    category: str
    #: Shown for judgement only. It never decides whether to prompt.
    is_family_expense: bool | None = None

    def card(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "name": self.name,
            "amount_cny": str(self.amount_cny),
            "category": self.category,
            "is_family_expense": self.is_family_expense,
        }


@dataclass(frozen=True)
class DuplicateFinding:
    check_id: str
    candidates: tuple[Candidate, ...]


class DuplicateIntent(Protocol):
    name: str
    amount_cny: Decimal
    occurred_on: Any
    category: str


class DuplicateRow(DuplicateIntent, Protocol):
    record_id: str


def _family_scope(value: object) -> bool | None:
    scope = getattr(value, "is_family_expense", None)
    return scope if isinstance(scope, bool) else None


def intent_fingerprint(entry: DuplicateIntent) -> str:
    """Bind a check to the exact entry it was raised for.

    Scope is included here even though it is not part of the *match* key: an
    override must not carry over to an entry that differs in any way, including
    one Henson re-stated as a family expense.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "name": entry.name,
                "amount_cny": str(entry.amount_cny),
                "occurred_on": entry.occurred_on.isoformat(),
                "category": entry.category,
                "is_family_expense": _family_scope(entry),
            }
        ).encode("utf-8")
    ).hexdigest()


def find_exact_duplicates(
    entry: DuplicateIntent, rows: list[DuplicateRow]
) -> tuple[Candidate, ...]:
    """Every ledger row that exactly matches what is about to be written.

    Comparison is on final stored values only. `rows` comes from a full-year
    scan, so rows Henson created by hand in Feishu are included -- checking only
    what the Agent wrote would miss the most likely duplicate of all.
    """
    matches = [
        row
        for row in rows
        if row.occurred_on == entry.occurred_on
        and row.amount_cny is not None
        and row.amount_cny == entry.amount_cny
        and row.name == entry.name
        and row.category == entry.category
    ]
    return tuple(
        Candidate(
            record_id=row.record_id,
            name=row.name,
            amount_cny=row.amount_cny,
            category=row.category or "",
            is_family_expense=_family_scope(row),
        )
        for row in matches
    )


def candidate_set_hash(candidates: tuple[Candidate, ...]) -> str:
    """A stable hash of exactly which rows were shown.

    Sorted, so the hash reflects the *set*: a provider returning the same rows
    in a different order must not invalidate a decision Henson already made.
    """
    return hashlib.sha256(
        canonical_json(sorted(c.record_id for c in candidates)).encode("utf-8")
    ).hexdigest()


def raise_check(
    session: Session,
    *,
    entry: DuplicateIntent,
    candidates: tuple[Candidate, ...],
    keyring: KeyRing,
    now: datetime,
    idempotency_key: str,
) -> DuplicateFinding:
    """Record a pending decision and return its id. Writes nothing to Feishu.

    The idempotency key is recorded because the id is *not* returned on the
    model-facing channel: the MCP call fails with a bare `POSSIBLE_DUPLICATE`,
    and the Agent looks the pending check up through the internal control plane
    using the key it sent.
    """
    check_id = str(uuid.uuid4())
    sealed = keyring.encrypt(
        json.dumps(
            sorted(c.record_id for c in candidates), ensure_ascii=False
        ).encode("utf-8"),
        table=_TABLE,
        column=_COLUMN,
        row_id=check_id,
    )
    session.add(
        DuplicateCheck(
            check_id=check_id,
            idempotency_key=idempotency_key,
            intent_fingerprint=intent_fingerprint(entry),
            encrypted_candidate_record_ids=sealed,
            status="awaiting_decision",
            created_at=now,
            expires_at=now + OVERRIDE_TTL,
        )
    )
    session.flush()
    return DuplicateFinding(check_id=check_id, candidates=candidates)


class OverrideRefused(RuntimeError):
    """An override does not authorise this write."""


def authorise_override(
    session: Session,
    *,
    check_id: str,
    entry: DuplicateIntent,
    current_candidates: tuple[Candidate, ...],
    keyring: KeyRing,
    now: datetime,
) -> None:
    """Accept a `write_anyway` decision, or refuse it. Never silently passes.

    Four independent conditions must hold, and each closes a different hole: the
    check must exist and still be awaiting a decision (no replay of a spent
    one), it must not have expired, it must have been raised for *this* intent
    (no carrying a confirmation to a different entry), and the candidate set
    must be unchanged since it was shown (design 7.7 -- if the ledger moved,
    Henson agreed to something else and must be asked again).
    """
    check = session.get(DuplicateCheck, check_id)
    if check is None:
        raise OverrideRefused("no such duplicate check")
    if check.status != "awaiting_decision":
        raise OverrideRefused(f"duplicate check already {check.status}")
    if check.expires_at <= now:
        raise OverrideRefused("duplicate check has expired")
    if check.intent_fingerprint != intent_fingerprint(entry):
        raise OverrideRefused("duplicate check was raised for a different entry")

    shown = json.loads(
        keyring.decrypt(
            check.encrypted_candidate_record_ids,
            table=_TABLE,
            column=_COLUMN,
            row_id=check_id,
        ).decode("utf-8")
    )
    if sorted(shown) != sorted(c.record_id for c in current_candidates):
        raise OverrideRefused(
            "the candidate set changed since the decision was made"
        )

    result = session.execute(
        update(DuplicateCheck)
        .where(
            DuplicateCheck.check_id == check_id,
            DuplicateCheck.status == "awaiting_decision",
            DuplicateCheck.expires_at > now,
            DuplicateCheck.intent_fingerprint == intent_fingerprint(entry),
        )
        .values(status="write_anyway", decided_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # Another request consumed, dismissed or expired this decision after the
        # checks above. A stale ORM identity must never turn that race into a
        # second authorisation.
        raise OverrideRefused("duplicate check was decided concurrently")
    session.expire(check)


def dismiss(
    session: Session, *, check_id: str, now: datetime
) -> None:
    """End the operation with no accounting side effect at all."""
    result = session.execute(
        update(DuplicateCheck)
        .where(
            DuplicateCheck.check_id == check_id,
            DuplicateCheck.status == "awaiting_decision",
            DuplicateCheck.expires_at > now,
        )
        .values(status="dismissed", decided_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        check = session.get(DuplicateCheck, check_id, populate_existing=True)
        if check is None:
            raise OverrideRefused("no such duplicate check")
        if check.expires_at <= now:
            raise OverrideRefused("duplicate check has expired")
        raise OverrideRefused(f"duplicate check already {check.status}")


def pending_check_for(
    session: Session, *, idempotency_key: str, now: datetime
) -> DuplicateCheck | None:
    """The check still awaiting a decision for this request, if any.

    A blocked write creates no execution row, so the same idempotency key can
    legitimately raise more than one check over time. The newest undecided one
    is the only one a decision can be about; an expired or already-decided check
    is deliberately not returned, so a stale id is never handed back as pending.
    """
    return session.scalars(
        select(DuplicateCheck)
        .where(
            DuplicateCheck.idempotency_key == idempotency_key,
            DuplicateCheck.status == "awaiting_decision",
            DuplicateCheck.expires_at > now,
        )
        .order_by(DuplicateCheck.created_at.desc(), DuplicateCheck.check_id.desc())
        .limit(1)
    ).one_or_none()


def expire_stale(session: Session, *, now: datetime) -> int:
    """Mark undecided checks past their expiry, so none lingers as pending."""
    result = session.execute(
        update(DuplicateCheck)
        .where(
            DuplicateCheck.status == "awaiting_decision",
            DuplicateCheck.expires_at <= now,
        )
        .values(status="expired", decided_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount
