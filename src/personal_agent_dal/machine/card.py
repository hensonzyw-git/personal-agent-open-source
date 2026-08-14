"""Decision-card invalidation and authoritative projection loading (DAL-013)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import Engine, select

from personal_agent_core.timeutil import parse_rfc3339, to_rfc3339, utc_now
from personal_agent_dal.machine.transition_types import ReceiptCodes, TransitionRefused
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import Decision, DecisionCardProjection


CARD_FIELDS: Final[frozenset[str]] = frozenset(
    {"decision_id", "projection_version", "status", "expires_at", "superseded_by"}
)


def _card_rfc3339(value: datetime) -> str:
    """Preserve the card contract's second precision when no fraction exists."""

    return to_rfc3339(value).replace(".000000Z", "Z")


def _stale(detail: str, latest: dict[str, Any] | None) -> TransitionRefused:
    return TransitionRefused(
        ReceiptCodes.DECISION_STALE,
        detail,
        latest_projection=latest,
    )


def apply_decision_action(
    *,
    card: dict[str, Any],
    server_projection: dict[str, Any],
    server_now: datetime,
) -> dict[str, Any]:
    """Validate an action against the exact latest projection and return it."""

    if frozenset(card) != CARD_FIELDS:
        raise _stale("card field set is not closed", server_projection)
    if frozenset(server_projection) != CARD_FIELDS:
        raise _stale("server projection field set is not closed", server_projection)
    if card.get("decision_id") != server_projection.get("decision_id"):
        raise _stale("card belongs to a different decision", server_projection)
    if server_projection.get("superseded_by") is not None:
        raise _stale("decision was superseded", server_projection)
    if server_projection.get("status") != "open":
        raise _stale("decision is no longer open", server_projection)

    expires_at = server_projection.get("expires_at")
    if expires_at is not None and server_now >= parse_rfc3339(expires_at):
        raise _stale("decision has expired", server_projection)
    if card.get("projection_version") != server_projection.get("projection_version"):
        raise _stale("card is behind the current projection", server_projection)
    return server_projection


def load_and_apply_decision_action(
    engine: Engine,
    *,
    card: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load the latest projection from the decision store, then validate it."""

    now = now or utc_now()
    decision_id = card.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        raise _stale("card has no decision identity", None)

    sessions = session_factory(engine)
    with sessions() as session:
        row = session.execute(
            select(DecisionCardProjection, Decision)
            .join(Decision, Decision.decision_id == DecisionCardProjection.decision_id)
            .where(DecisionCardProjection.decision_id == decision_id)
            .order_by(DecisionCardProjection.projection_version.desc())
            .limit(1)
        ).first()
        if row is None:
            raise _stale("decision projection does not exist", None)
        projection, decision = row
        latest = {
            "decision_id": decision.decision_id,
            "projection_version": projection.projection_version,
            "status": decision.status,
            "expires_at": (
                _card_rfc3339(decision.expires_at)
                if decision.expires_at is not None
                else None
            ),
            "superseded_by": decision.superseded_by,
        }
        if not projection.actionable:
            raise _stale("decision projection is not actionable", latest)
    return apply_decision_action(card=card, server_projection=latest, server_now=now)
