"""Authoritative Decision Dock projection and persistence (DAL-013)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import Engine, select

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import parse_rfc3339, to_rfc3339, utc_now
from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import Decision, DecisionCardProjection
from personal_agent_dal.storage.models import OperationEvent, OperationReceiptRow


OPERATION_SPEC_ID: Final[str] = "OP-DOCK-001"
COMMAND_TYPE: Final[str] = "project_decision_dock"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "decision-store"
MAXIMUM_ITEMS: Final[int] = 5
BLOCKING_SCOPES: Final[frozenset[str]] = frozenset({"global", "local", "none"})
DECISION_STATUSES: Final[frozenset[str]] = frozenset(
    {"open", "resolved", "superseded"}
)
EXPIRY_WINDOW: Final[timedelta] = timedelta(minutes=15)
CANDIDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "decision_id",
        "root_id",
        "status",
        "safety_or_irreversible",
        "blocking_scope",
        "depends_on",
        "expires_at",
        "created_at",
    }
)


@dataclass(frozen=True)
class DockProjection:
    ordered_decision_ids: tuple[str, ...]
    ranks: dict[str, int]
    evictions: dict[str, str]


def _validate_candidate(candidate: dict[str, Any]) -> None:
    if frozenset(candidate) != CANDIDATE_FIELDS:
        raise ValueError("candidate field set is not closed")
    if not isinstance(candidate["decision_id"], str) or not candidate["decision_id"]:
        raise ValueError("decision_id must be a non-empty string")
    if not isinstance(candidate["root_id"], str) or not candidate["root_id"]:
        raise ValueError("root_id must be a non-empty string")
    if candidate["status"] not in DECISION_STATUSES:
        raise ValueError("decision status outside the frozen set")
    if not isinstance(candidate["safety_or_irreversible"], bool):
        raise ValueError("safety_or_irreversible must be boolean")
    if candidate["blocking_scope"] not in BLOCKING_SCOPES:
        raise ValueError("blocking_scope outside the frozen set")
    dependencies = candidate["depends_on"]
    if (
        not isinstance(dependencies, list)
        or any(not isinstance(dep, str) or not dep for dep in dependencies)
        or len(dependencies) != len(set(dependencies))
    ):
        raise ValueError("depends_on must contain unique non-empty ids")
    for field in ("created_at", "expires_at"):
        value = candidate[field]
        if field == "expires_at" and value is None:
            continue
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError(f"{field} must be RFC3339 UTC Z")
        parse_rfc3339(value)


def _rank_of(candidate: dict[str, Any], server_now: datetime) -> int:
    if candidate["safety_or_irreversible"]:
        return 0
    scope = candidate["blocking_scope"]
    if scope == "global":
        return 1
    if candidate["expires_at"] is not None and (
        parse_rfc3339(candidate["expires_at"]) - server_now
    ) <= EXPIRY_WINDOW:
        return 2
    if scope == "local":
        return 3
    return 4


def project_decision_dock(
    candidates: list[dict[str, Any]],
    *,
    server_now: datetime,
    maximum_items: int = MAXIMUM_ITEMS,
) -> DockProjection:
    """Pure closed-schema projection; performs no writes."""

    if not isinstance(maximum_items, int) or isinstance(maximum_items, bool):
        raise ValueError("maximum_items must be an integer")
    if not 1 <= maximum_items <= MAXIMUM_ITEMS:
        raise ValueError("maximum_items is outside the frozen bound")
    for candidate in candidates:
        _validate_candidate(candidate)
    ids = [candidate["decision_id"] for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate decision_id values must be unique")
    by_id = {candidate["decision_id"]: candidate for candidate in candidates}

    def dependency_resolved(dep: str) -> bool:
        target = by_id.get(dep)
        return target is not None and target["status"] in {"resolved", "superseded"}

    evictions: dict[str, str] = {}
    filtered: list[dict[str, Any]] = []
    for candidate in candidates:
        # Resolved/superseded/expired rows are authority for dependencies but
        # are never themselves part of the actionable frontier.
        if candidate["status"] != "open":
            continue
        expires_at = candidate["expires_at"]
        if expires_at is not None and server_now >= parse_rfc3339(expires_at):
            continue
        if any(not dependency_resolved(dep) for dep in candidate["depends_on"]):
            evictions[candidate["decision_id"]] = "dependency"
        else:
            filtered.append(candidate)

    def sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        expires = (
            parse_rfc3339(candidate["expires_at"])
            if candidate["expires_at"] is not None
            else datetime.max.replace(tzinfo=server_now.tzinfo)
        )
        return (
            _rank_of(candidate, server_now),
            expires,
            parse_rfc3339(candidate["created_at"]),
            candidate["decision_id"].encode("utf-8"),
        )

    ordered = sorted(filtered, key=sort_key)
    seen_root: set[str] = set()
    kept: list[dict[str, Any]] = []
    for candidate in ordered:
        if candidate["root_id"] in seen_root:
            evictions[candidate["decision_id"]] = "same_root"
            continue
        seen_root.add(candidate["root_id"])
        kept.append(candidate)

    projection = kept[:maximum_items]
    for candidate in kept[maximum_items:]:
        evictions[candidate["decision_id"]] = "maximum_items"
    return DockProjection(
        ordered_decision_ids=tuple(item["decision_id"] for item in projection),
        ranks={item["decision_id"]: _rank_of(item, server_now) for item in projection},
        evictions=evictions,
    )


def _validate_command(command: dict[str, Any]) -> None:
    if command.get("operation_spec_id") != OPERATION_SPEC_ID:
        raise DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail="wrong Dock spec")
    if command.get("actor_type") != SERVICE_ACTOR:
        raise DalError(DalErrorCode.ACTOR_NOT_ALLOWED)
    if command.get("evidence_source_type") != EVIDENCE_SOURCE:
        raise DalError(DalErrorCode.SCOPE_DENIED)
    payload = command.get("input")
    if not isinstance(payload, dict):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)
    facts = payload.get("authoritative_facts")
    if facts != {"source": EVIDENCE_SOURCE}:
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT,
            internal_detail="Dock facts must name only the decision-store source",
        )
    if payload.get("action_sequence") != [
        {"command": "rank_decisions"},
        {"command": COMMAND_TYPE, "maximum_items": MAXIMUM_ITEMS},
    ]:
        raise DalError(DalErrorCode.INVALID_ARGUMENT)


def _load_candidates(session: Any, feature_id: str) -> tuple[list[dict[str, Any]], dict[str, Decision]]:
    decisions = list(
        session.execute(
            select(Decision)
            .where(Decision.feature_id == feature_id)
            .order_by(Decision.created_at, Decision.decision_id)
        ).scalars()
    )
    by_id = {decision.decision_id: decision for decision in decisions}
    candidates: list[dict[str, Any]] = []
    for decision in decisions:
        try:
            dependencies = json.loads(decision.depends_on_json)
        except (TypeError, json.JSONDecodeError):
            raise DalError(
                DalErrorCode.INTERNAL_ERROR,
                internal_detail="stored decision dependencies are invalid",
            ) from None
        candidates.append(
            {
                "decision_id": decision.decision_id,
                "root_id": decision.root_id,
                # ``consumed`` is a storage lifecycle detail. For the closed
                # Dock projection vocabulary it is terminal and dependency-
                # satisfying, therefore equivalent to ``resolved``.
                "status": (
                    "resolved" if decision.status == "consumed" else decision.status
                ),
                "safety_or_irreversible": decision.safety_or_irreversible,
                "blocking_scope": decision.blocking_scope,
                "depends_on": dependencies,
                "expires_at": (
                    to_rfc3339(decision.expires_at)
                    if decision.expires_at is not None
                    else None
                ),
                "created_at": to_rfc3339(decision.created_at),
            }
        )
    return candidates, by_id


def _projection_body(projection: DockProjection) -> dict[str, Any]:
    return {
        "ordered_decision_ids": list(projection.ordered_decision_ids),
        "ranks": projection.ranks,
        "evictions": projection.evictions,
    }


def _projection_from_event(detail: str) -> DockProjection:
    body = json.loads(detail)
    return DockProjection(
        ordered_decision_ids=tuple(body["ordered_decision_ids"]),
        ranks={key: int(value) for key, value in body["ranks"].items()},
        evictions=dict(body["evictions"]),
    )


def apply_decision_dock(
    engine: Engine,
    command: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[DockProjection, OperationReceipt]:
    """Load the current decision store and atomically replace its Dock frontier."""

    _validate_command(command)
    now = now or utc_now()
    payload = command["input"]
    request_digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    feature_id = payload["target"]["entity_id"]
    sessions = session_factory(engine)

    def work(session: Any) -> tuple[DockProjection, OperationReceipt]:
        existing = session.execute(
            select(OperationReceiptRow).where(
                OperationReceiptRow.idempotency_key == command["idempotency_key"]
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not hmac.compare_digest(existing.request_payload_sha256, request_digest):
                raise DalError(DalErrorCode.IDEMPOTENCY_CONFLICT)
            detail = session.execute(
                select(OperationEvent.detail).where(
                    OperationEvent.operation_id == existing.operation_id,
                    OperationEvent.event_type == "decision.created",
                )
            ).scalar_one()
            return _projection_from_event(detail), OperationReceipt(ReceiptCode.APPLIED)

        candidates, decisions = _load_candidates(session, feature_id)
        try:
            projection = project_decision_dock(candidates, server_now=now)
        except ValueError as error:
            raise DalError(
                DalErrorCode.INTERNAL_ERROR, internal_detail=str(error)
            ) from None

        decision_ids = set(decisions)
        existing_projections = list(
            session.execute(
                select(DecisionCardProjection).where(
                    DecisionCardProjection.decision_id.in_(decision_ids)
                )
            ).scalars()
        ) if decision_ids else []
        current_rows = {
            (row.decision_id, row.decision_version): row
            for row in existing_projections
        }
        visible = set(projection.ordered_decision_ids)
        changed_rows: set[tuple[str, int]] = set()
        for row in existing_projections:
            if row.actionable:
                row.actionable = False
                row.display_state = "not_actionable"
                row.projection_version += 1
                changed_rows.add((row.decision_id, row.decision_version))
        for decision_id in projection.ordered_decision_ids:
            decision = decisions[decision_id]
            key = (decision_id, decision.decision_version)
            row = current_rows.get(key)
            if row is None:
                session.add(
                    DecisionCardProjection(
                        projection_id=new_id(),
                        decision_id=decision_id,
                        decision_version=decision.decision_version,
                        projection_version=1,
                        actionable=True,
                        display_state="needs_human",
                        dock_rank=projection.ranks[decision_id],
                        created_at=now,
                    )
                )
            else:
                if key not in changed_rows:
                    row.projection_version += 1
                row.actionable = True
                row.display_state = "needs_human"
                row.dock_rank = projection.ranks[decision_id]

        response_body = _projection_body(projection)
        response_digest = hashlib.sha256(
            canonical_json(response_body).encode("utf-8")
        ).hexdigest()
        session.add(
            OperationReceiptRow(
                operation_id=command["operation_id"],
                idempotency_key=command["idempotency_key"],
                operation_spec_id=OPERATION_SPEC_ID,
                command_type=COMMAND_TYPE,
                actor_type=SERVICE_ACTOR,
                evidence_source_type=EVIDENCE_SOURCE,
                receipt_code=ReceiptCode.APPLIED.value,
                receipt_schema_version=OperationReceipt(ReceiptCode.APPLIED).schema_version,
                request_payload_sha256=request_digest,
                response_payload_sha256=response_digest,
                recorded_at=now,
            )
        )
        session.add(
            OperationEvent(
                operation_event_id=new_id(),
                event_type="decision.created",
                operation_id=command["operation_id"],
                occurred_at=now,
                detail=canonical_json(response_body),
            )
        )
        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=command["operation_id"],
            event_type=COMMAND_TYPE,
            redacted_summary=f"decision dock projected {len(visible)} item(s)",
            now=now,
        )
        return projection, OperationReceipt(ReceiptCode.APPLIED)

    with sessions() as session:
        return run_write_transaction(session, lambda: work(session))
