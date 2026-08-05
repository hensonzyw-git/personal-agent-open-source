"""Closing the loop on an operation that ended at `needs_manual_review`.

DEV-040 §13.2, option B of Henson's 2026-08-04 decision. `needs_manual_review`
is where the system parks an operation whose accounting outcome it cannot
establish by itself. Until now that was a dead end: the state is terminal, the
push run on 2026-08-04 found no way for a person to act on the card, and the
breakpoint drill the same day added four more stranded rows.

**What this is not.** It is not a new terminal edge, and it does not let anyone
declare the write succeeded. Option A already makes the *automatic* path
self-heal: an unknown commit now stays recoverable and recovery projects
Finance's real state onto it. What is left for a human is the residue -- the
cases where Finance genuinely has no execution, so no amount of re-reading will
produce an answer, and the only remaining fact source is the ledger itself.

So the resolution is recorded the way `cancel_requested` and `client_detached`
are recorded: a flag beside the state, never instead of it. The operation stays
terminal, recovery still skips it, and `state` continues to mean exactly what it
meant before -- what the *system* could prove. `manual_resolution` means only
what a person reported seeing. Keeping the two apart is the point: if a human
observation could overwrite `state`, then a mistaken tap would be indistinguish-
able from verified evidence forever after, which is the failure this whole
subsystem exists to prevent.

Changing one's mind is refused rather than merged. A second, different answer to
"is the record in the ledger?" means one of the two readings was wrong, and that
is a discrepancy for a person to look at again -- not something to silently
overwrite with whichever tap came last.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from personal_agent.api import events
from personal_agent.storage.models import MANUAL_RESOLUTIONS, Operation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


#: A person's report about the ledger, appended to the Timeline as a permanent
#: fact. Presentation state rather than dialogue, so -- like `DUPLICATE_DECISION`
#: -- it is deliberately absent from `MODEL_VISIBLE_EVENT_TYPES`: the model must
#: never read a human's manual reconciliation as an instruction.
MANUAL_REVIEW_RESOLVED = "manual_review_resolved"


@dataclass(frozen=True)
class ResolutionOutcome:
    operation: Operation
    #: False when this call replayed an identical, already-recorded resolution.
    recorded: bool


def resolve_manual_review(
    session,
    keyring: KeyRing,
    *,
    operation_id: str,
    device_id: str,
    resolution: str,
    now: datetime,
) -> ResolutionOutcome:
    """Record what a person found in the ledger for one reviewed operation."""
    if resolution not in MANUAL_RESOLUTIONS:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=(
                f"resolution must be one of {sorted(MANUAL_RESOLUTIONS)}"
            ),
        )

    operation = (
        session.query(Operation)
        .filter(Operation.operation_id == operation_id)
        .filter(Operation.api_request.has(device_id=device_id))
        .one_or_none()
    )
    if operation is None:
        # Same answer for "no such operation" and "not this device's operation":
        # a caller must not be able to probe for the existence of either.
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="no such operation for this device",
        )

    if operation.state != "needs_manual_review":
        raise AppError(
            ErrorCode.UNSUPPORTED_OPERATION,
            internal_detail=(
                "only an operation that ended at needs_manual_review can carry "
                f"a manual resolution; this one is {operation.state}"
            ),
        )

    if operation.manual_resolution is not None:
        if operation.manual_resolution == resolution:
            # A replayed tap, or the same answer from a second device. Nothing
            # to record and nothing to complain about.
            return ResolutionOutcome(operation=operation, recorded=False)
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail=(
                "this operation was already resolved as "
                f"{operation.manual_resolution}; two different readings of the "
                "same ledger is a discrepancy to re-check, not an overwrite"
            ),
        )

    operation.manual_resolution = resolution
    operation.manual_resolved_at = now
    session.flush()
    return ResolutionOutcome(operation=operation, recorded=True)


def append_resolution_event(
    session,
    keyring: KeyRing,
    *,
    operation: Operation,
    conversation_id: str,
    session_id: str,
    turn_id: str,
    resolution: str,
    now: datetime,
) -> str | None:
    """Append the Timeline marker, exactly once per operation."""
    from personal_agent.storage.models import ConversationEvent

    already = (
        session.query(ConversationEvent)
        .filter(
            ConversationEvent.operation_id == operation.operation_id,
            ConversationEvent.event_type == MANUAL_REVIEW_RESOLVED,
        )
        .first()
    )
    if already is not None:
        return None
    return events.append_event(
        session,
        keyring,
        conversation_id=conversation_id,
        session_id=session_id,
        turn_id=turn_id,
        event_type=MANUAL_REVIEW_RESOLVED,
        content={"resolution": resolution},
        operation_id=operation.operation_id,
        now=now,
    )
