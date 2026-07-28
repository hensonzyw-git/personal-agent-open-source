"""The production compact Session state a boundary classifier is allowed to see.

Design §6.1 permits the semantic classifier exactly two inputs: the current
message, and a bounded, de-identified account of the open Session produced by a
*trusted* provider. This module is that provider, and the trust comes from where
each field is read:

- `topic_summary` is the active Checkpoint's `goal`. That value has already
  passed the §8.4 validators -- non-empty, grounded in real source events, no
  invented amounts or record ids, no credential-shaped strings -- and
  `Compactor.active_checkpoint` re-validates it on every read, so a tampered or
  stale Checkpoint yields nothing rather than a plausible sentence.
- `domain` is derived from the tools the Session's own operations actually
  used. It is a fact about the store, not a model judgement.
- `task_state` is derived from operation states and the Checkpoint's open items,
  in a fixed order.

When there is no verified Checkpoint there is no trusted semantic state, so the
provider refuses. `SessionManager` treats a refusal exactly like a classifier
timeout: continue the current Session. That is the intended shape -- a Session
too short to have been compacted has no automatic boundary, and the cost of
merging two topics is some irrelevance, while the cost of splitting one is real
lost context.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text as sql_text

from personal_agent.context.compactor import Compactor
from personal_agent.context.session_manager import (
    MAX_DOMAIN_CHARS,
    MAX_TOPIC_SUMMARY_CHARS,
    CompactSessionState,
)
from personal_agent.storage.models import (
    TERMINAL_OPERATION_STATES,
    ContextSession,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


_TERMINAL_SQL: Final[str] = ", ".join(
    f"'{state}'" for state in sorted(TERMINAL_OPERATION_STATES)
)


class CheckpointCompactStateProvider:
    """Derives the classifier's view of a Session from verified state only."""

    def __init__(self, compactor: Compactor, keyring: KeyRing) -> None:
        self._compactor = compactor
        self._keyring = keyring

    def compact_state(
        self, db, *, session: ContextSession
    ) -> CompactSessionState:
        verified = self._compactor.active_checkpoint(
            db, self._keyring, session_id=session.session_id
        )
        if verified is None:
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="no verified checkpoint for this Session",
            )
        payload, _row = verified
        goal = payload.get("goal")
        summary = goal.get("value", "") if isinstance(goal, dict) else ""
        summary = summary.strip() if isinstance(summary, str) else ""
        if not summary or len(summary) > MAX_TOPIC_SUMMARY_CHARS:
            # No truncation. A shortened topic summary is still a summary the
            # classifier would act on, and the safe answer is to not classify.
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="checkpoint goal is unusable as a topic summary",
            )
        open_items = payload.get("open_items")
        has_open_items = isinstance(open_items, list) and bool(open_items)
        return CompactSessionState(
            topic_summary=summary,
            domain=self._domain(db, session.session_id),
            task_state=self._task_state(
                db, session.session_id, has_open_items=has_open_items
            ),
        )

    # -- derived facts ---------------------------------------------------

    def _domain(self, db, session_id: str) -> str | None:
        """The single business domain this Session's tools belong to.

        Two domains in one Session is not an error and not a boundary: it is
        simply not a usable signal, so it reports `None` instead of picking one.
        """
        rows = db.execute(
            sql_text(
                "SELECT DISTINCT o.tool FROM operations AS o "
                "JOIN conversation_events AS e "
                "  ON e.operation_id = o.operation_id "
                "WHERE e.session_id = :sid AND o.tool IS NOT NULL"
            ),
            {"sid": session_id},
        ).scalars().all()
        domains = {
            str(tool).split(".", 1)[0]
            for tool in rows
            if isinstance(tool, str) and "." in tool
        }
        if len(domains) != 1:
            return None
        domain = domains.pop()
        return domain if domain and len(domain) <= MAX_DOMAIN_CHARS else None

    def _task_state(
        self, db, session_id: str, *, has_open_items: bool
    ) -> str:
        """Fixed precedence: running beats blocked beats open beats finished."""
        states = db.execute(
            sql_text(
                "SELECT DISTINCT o.state FROM operations AS o "
                "JOIN conversation_events AS e "
                "  ON e.operation_id = o.operation_id "
                "WHERE e.session_id = :sid"
            ),
            {"sid": session_id},
        ).scalars().all()
        states = {str(state) for state in states}
        if states - TERMINAL_OPERATION_STATES:
            return "active"
        if "needs_manual_review" in states:
            # A human still has to touch the ledger; the task is not finished
            # and the user's next message may well be about it.
            return "blocked"
        if has_open_items:
            return "active"
        if "succeeded" in states:
            return "completed"
        return "unknown"
