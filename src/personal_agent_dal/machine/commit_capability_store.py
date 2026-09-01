"""Persistent issue/consume for the one-time commit capability (R09-A3).

The pure gates in :mod:`personal_agent_dal.machine.commit_capability` are
eligibility predicates; this module is the durable half that a controller
actually calls. DAL-004 §5 fixes the mechanics: issue, consume and receipt
persistence go through a **capability version CAS**; consuming a capability,
writing the external-effect intent, the audit append and the outbox row are
**one atomic transaction**; and a replayed request returns the **original
receipt**, never a second effect.

Composition per call (all inside one :func:`run_write_transaction` unit):

- issue: the pure gate judges first — a malformed binding never touches the
  database — then the row is inserted under the issue idempotency key's
  unique constraint, and issue intent/audit/outbox are written atomically.
  A replay returns the original issue receipt without re-judging.
- consume: the pure gate judges the presented intent against the freshly
  read row (a raw SELECT, never the identity map — CLAUDE.md §5.2), then
  a single compare-and-swap moves ``issued -> consumed`` with the consuming
  identity and ``consumed_at`` in one statement. The winner also writes the
  consume intent, audit and outbox; the loser re-reads a dead row and gets
  ``CAPABILITY_STALE``. A replay returns the original consume receipt.

The block landing (live capability + tampered presentation) is also
persistent: the feature's ``needs_human`` landing and its seven-write set
belong to the engine transition, which the controller applies through the
frozen registry — this module records the block verdict's own evidence rows
(intent, audit, outbox) and returns the pure gate's block evaluation
unchanged. It never invents a transition spec id: no row here claims
``BLK-POLICY--verified`` was applied by the engine unless the caller applied
it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import commit_capability
from personal_agent_dal.machine.commit_capability import (
    CommitCapabilityEvaluation,
)
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from sqlalchemy import Engine

from personal_agent_dal.storage.machine_models import (
    CommitCapability,
    ExternalEffect,
)
from personal_agent_dal.storage.models import OutboxEvent

SCHEMA_VERSION: Final[str] = "dal.commit-capability-row/1.0"
POLICY_VERSION: Final[str] = "dal-policy/1.0"

#: Intent scopes: one capability per (feature, key). The unique constraint
#: lives on the idempotency columns; the scope key follows the engine's
#: ``<aggregate_id>:<spec_id>`` shape with this module's local qualifier.
ISSUE_EFFECT_SCOPE: Final[str] = "commit-capability:issue"
CONSUME_EFFECT_SCOPE: Final[str] = "commit-capability:consume"
BLOCK_EFFECT_SCOPE: Final[str] = "commit-capability:block"

ISSUE_EVENT_TYPE: Final[str] = "commit_capability.issued"
CONSUME_EVENT_TYPE: Final[str] = "commit_capability.consumed"
BLOCK_EVENT_TYPE: Final[str] = "commit_capability.blocked"

#: The outbox topic carries the concrete event, not a generic channel: the
#: ``outbox_events`` unique constraint ``(aggregate_type, aggregate_id,
#: aggregate_version, topic)`` admits one pending row per topic per
#: aggregate version, so a generic topic would silently drop the second
#: lifecycle event (issue, then consume, then block) of one feature.
OUTBOX_TOPIC_PREFIX: Final[str] = "commit_capability."

#: The redacted audit summaries. Free text in the audit chain is the leak
#: path; these are fixed phrases, never quotes of presented content.
ISSUE_SUMMARY: Final[str] = "commit capability issued for a verified feature"
CONSUME_SUMMARY: Final[str] = "commit capability consumed by the git executor"
BLOCK_SUMMARY: Final[str] = (
    "commit capability presentation diverged; feature blocked for human"
)


@dataclass(frozen=True)
class CapabilityIssueOutcome:
    """The observable result of one persistent issue."""

    receipt: OperationReceipt
    capability_id: str
    replayed: bool


@dataclass(frozen=True)
class CapabilityConsumeOutcome:
    """The observable result of one persistent consume attempt."""

    receipt: OperationReceipt
    capability_id: str
    #: The pure gate's verdict: ``stale`` (zero writes), ``blocked`` (the
    #: seven-write block landing) or ``go`` (the winner's applied path).
    verdict: str
    replayed: bool
    violations: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _row_to_binding(row: CommitCapability) -> dict[str, Any]:
    """The pure gate's binding view of a persisted row."""
    return {
        "capability_id": row.capability_id,
        "approval_id": row.approval_id,
        "lease_epoch": row.lease_epoch,
        "base_sha": row.base_sha,
        "result_sha": row.result_sha,
        "allowed_paths": json.loads(row.allowed_paths_json),
        "trailers": json.loads(row.trailers_json),
        "idempotency_key": row.issue_idempotency_key,
        "expires_at": row.expires_at,
        "max_uses": row.max_uses,
        "capability_epoch": row.capability_epoch,
    }


def _row_to_liveness(row: CommitCapability) -> dict[str, Any]:
    """The pure gate's lifecycle fields for a freshly read row."""
    return {
        "uses_consumed": row.uses_consumed,
        "consumed_by": row.consumed_by,
        "revoked_at": row.revoked_at,
    }


def _write_intent(
    session: Session,
    *,
    scope: str,
    idempotency_key: str,
    owner_feature_id: str,
    fingerprint: str,
    capability_id: str,
    now: datetime,
) -> None:
    """The external-effect intent row for one issue or consume."""
    session.add(
        ExternalEffect(
            effect_id=new_id(),
            version=1,
            origin="dal_dispatched",
            owner_aggregate_type="feature",
            owner_aggregate_id=owner_feature_id,
            effect_scope_key=scope,
            remote_idempotency_key=idempotency_key,
            target_fingerprint=fingerprint,
            state="intent_recorded",
            attempt=1,
            executor_id="workflow-service",
            executor_epoch=None,
            claim_expires_at=None,
            capability_id=capability_id,
            capability_epoch=None,
            receipt_refs_sha256=None,
            post_read_refs_sha256=None,
            impact_sha256=None,
            created_at=now,
            updated_at=now,
        )
    )


def _write_outbox(
    session: Session,
    *,
    owner_feature_id: str,
    event: str,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    session.add(
        OutboxEvent(
            outbox_id=new_id(),
            aggregate_type="feature",
            aggregate_id=owner_feature_id,
            aggregate_version=1,
            topic=f"{OUTBOX_TOPIC_PREFIX}{event}",
            payload_sha256=_digest(payload),
            delivery_state="pending",
            available_at=now,
            attempt_count=0,
            created_at=now,
        )
    )


def issue_commit_capability_row(
    engine: Engine,
    facts: dict[str, Any],
    *,
    repository_id: str,
) -> CapabilityIssueOutcome:
    """Judge, then persist one capability row; a replay returns the original.

    The pure gate runs first: a malformed binding raises before the
    transaction opens and never writes. ``repository_id`` is a
    composition-level parameter — the pure gate's closed facts shape has no
    repo concept (evidence §5 F-3 mapping), so it travels beside the facts,
    validated natively here before anything is written.

    The row insert rides the issue idempotency key's unique constraint,
    which is the replay fence; inside the fence the intent, audit and
    outbox rows are written in the same transaction, so an issue is never a
    bare capability row.
    """
    if type(repository_id) is not str or not repository_id:
        raise _invalid("repository_id must be a non-empty native string")
    commit_capability.issue_commit_capability(facts)
    binding = facts["binding"]
    target = facts["target"]
    issue_key = binding["idempotency_key"]
    now_dt = utc_now()
    request_sha = _digest({"facts": facts, "repository_id": repository_id})
    sessions = session_factory(engine)

    def _body(session: Session) -> CapabilityIssueOutcome:
        existing = session.scalars(
            select(CommitCapability).where(
                CommitCapability.issue_idempotency_key == issue_key
            )
        ).first()
        if existing is not None:
            _replay_guard(existing, facts["binding"], "issue")
            return CapabilityIssueOutcome(
                receipt=OperationReceipt(ReceiptCode.APPLIED),
                capability_id=existing.capability_id,
                replayed=True,
            )

        capability_id = binding["capability_id"]
        row = CommitCapability(
            capability_id=capability_id,
            schema_version=SCHEMA_VERSION,
            state_version=1,
            state="issued",
            action="commit_candidate",
            approval_id=binding["approval_id"],
            feature_id=target["entity_id"],
            task_id=binding["trailers"]["Task-Id"],
            repository_id=repository_id,
            allowed_paths_json=canonical_json(binding["allowed_paths"]),
            refs=None,
            artifact_or_diff_sha256=binding["trailers"]["Plan-Hash"],
            base_sha=binding["base_sha"],
            result_sha=binding["result_sha"],
            lease_epoch=binding["lease_epoch"],
            capability_epoch=binding["capability_epoch"],
            expires_at=binding["expires_at"],
            max_uses=binding["max_uses"],
            uses_consumed=0,
            policy_version=POLICY_VERSION,
            issue_idempotency_key=issue_key,
            trailers_json=canonical_json(binding["trailers"]),
            revoked_at=None,
            consumed_by=None,
            consumed_at=None,
            created_at=now_dt,
        )
        session.add(row)
        session.flush()

        _write_intent(
            session,
            scope=ISSUE_EFFECT_SCOPE,
            idempotency_key=f"{issue_key}:intent",
            owner_feature_id=target["entity_id"],
            fingerprint=request_sha,
            capability_id=capability_id,
            now=now_dt,
        )
        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=target["entity_id"],
            event_type=ISSUE_EVENT_TYPE,
            redacted_summary=ISSUE_SUMMARY,
            now=now_dt,
        )
        _write_outbox(
            session,
            owner_feature_id=target["entity_id"],
            event="issued",
            payload={
                "capability_id": capability_id,
                "feature_id": target["entity_id"],
                "capability_epoch": binding["capability_epoch"],
                "lease_epoch": binding["lease_epoch"],
            },
            now=now_dt,
        )
        return CapabilityIssueOutcome(
            receipt=OperationReceipt(ReceiptCode.APPLIED),
            capability_id=capability_id,
            replayed=False,
        )

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


#: The binding fields whose sameness makes a replay the *same* request.
#: Both the issue facts and the consume facts carry them, at different
#: nesting; the two entry points hand their own view in.
_REPLAY_FIELDS: Final[tuple[str, ...]] = (
    "capability_id",
    "approval_id",
    "base_sha",
    "result_sha",
    "expires_at",
    "capability_epoch",
    "lease_epoch",
)


def _replay_guard(
    row: CommitCapability,
    presented: dict[str, Any],
    kind: str,
) -> None:
    """A key is bound to its content: a reused key with different content is
    a conflict, never a silent second judgement."""
    if row.schema_version != SCHEMA_VERSION:
        raise _invalid(f"{kind} replay found a row of another schema version")
    # The binding the row carries must be the binding this very request
    # presents; anything else means the key is being spent on new content.
    binding = _row_to_binding(row)
    for field in _REPLAY_FIELDS:
        if binding[field] != presented[field]:
            raise DalError(
                DalErrorCode.IDEMPOTENCY_CONFLICT,
                internal_detail=f"{kind} key reused with different {field}",
            )


#: The presented fields a consume replay must re-match: the presentation
#: carries only the binding-class fields, so the same-content fence is
#: those four plus the issue key the presentation names.
_CONSUME_REPLAY_FIELDS: Final[tuple[str, ...]] = (
    "capability_id",
    "approval_id",
    "base_sha",
    "result_sha",
)


def _consume_replay_guard(
    row: CommitCapability,
    facts: dict[str, Any],
) -> None:
    """The consume replay fence: the same effect/command identity replayed
    must present the same binding-class content, or the key is being spent
    on a new intent."""
    presented = facts["presented"]
    for field in _CONSUME_REPLAY_FIELDS:
        if presented[field] != getattr(row, field):
            raise DalError(
                DalErrorCode.IDEMPOTENCY_CONFLICT,
                internal_detail=f"consume key reused with different {field}",
            )
    if presented["idempotency_key"] != row.issue_idempotency_key:
        raise DalError(
            DalErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail="consume key reused with a different issue key",
        )


def consume_commit_capability_row(
    engine: Engine,
    facts: dict[str, Any],
    *,
    consumed_by: str,
) -> CapabilityConsumeOutcome:
    """Judge the executor's presentation against the fresh row, then CAS.

    The row is read with a raw SELECT inside the transaction (identity-map
    safe). The pure gate classifies: stale refuses with zero writes; a live
    tampered presentation lands the block evidence rows and returns the
    block evaluation; a clean presentation runs the single compare-and-swap
    that moves ``issued -> consumed``. Two racing consumes produce one
    winner; the loser's CAS matches zero rows, re-reads the now-dead row
    and gets the stale refusal.

    ``consumed_by`` is the consuming effect/command identity (DAL-004 §5's
    ``consumed_by_effect_or_command_id``) and the replay fence: the
    presentation's ``idempotency_key`` is the *issue* key carried for
    binding comparison, so the fence cannot ride it — a replayed consume
    is recognised by the identity already recorded on the row, and the
    same identity is the winner's intent/outbox idempotency key.
    """
    if type(consumed_by) is not str or not consumed_by:
        raise _invalid("consumed_by must be a non-empty native string")
    target = facts["target"]
    presented = facts["presented"]
    consume_key = consumed_by
    now_dt = utc_now()
    request_sha = _digest({"facts": facts, "consumed_by": consumed_by})
    sessions = session_factory(engine)

    def _body(session: Session) -> CapabilityConsumeOutcome:
        row = session.scalars(
            select(CommitCapability).where(
                CommitCapability.capability_id == presented["capability_id"]
            )
        ).first()
        if row is None:
            raise _invalid("consume names no persisted capability row")

        # Replay first: a replayed consume returns the original receipt
        # without re-judging and without a second effect row.
        if row.consumed_by == consume_key and row.consumed_at is not None:
            _consume_replay_guard(row, facts)
            return CapabilityConsumeOutcome(
                receipt=OperationReceipt(ReceiptCode.APPLIED),
                capability_id=row.capability_id,
                verdict="go",
                replayed=True,
            )

        # Fresh facts from the database's real state, not the caller's.
        consume_facts = {
            "schema_version": commit_capability.CONSUME_FACTS_SCHEMA,
            "target": target,
            "capability": {
                **_row_to_binding(row),
                **_row_to_liveness(row),
            },
            "presented": presented,
            "now": facts["now"],
            "current_epoch": facts["current_epoch"],
            "current_lease_epoch": facts["current_lease_epoch"],
        }
        evaluation = commit_capability.consume_commit_capability(consume_facts)

        if evaluation.receipt.code == ReceiptCode.CAPABILITY_STALE:
            return CapabilityConsumeOutcome(
                receipt=evaluation.receipt,
                capability_id=row.capability_id,
                verdict="stale",
                replayed=False,
            )

        if evaluation.violations:
            # The block landing: evidence rows now; the engine's block
            # transition (seven-write set) is the controller's separate,
            # registry-driven step and is not claimed here.
            #
            # The block replay fence first: a block does not consume the
            # capability, so the row above stays ``issued`` and the same
            # command identity replaying its block request re-enters this
            # branch instead of the consumed-replay branch at the top. The
            # facts digest is deterministic across replays (same row, same
            # presentation, same caller facts), so a matching fingerprint is
            # the same request: return the original verdict as a replay
            # rather than writing a second block intent (the unique
            # constraint would turn the replay into a crash). A different
            # digest under the same identity is the key being spent on new
            # content: conflict, never a silent second judgement.
            existing_block = session.scalars(
                select(ExternalEffect).where(
                    ExternalEffect.effect_scope_key == BLOCK_EFFECT_SCOPE,
                    ExternalEffect.remote_idempotency_key
                    == f"{consume_key}:block",
                )
            ).first()
            if existing_block is not None:
                if existing_block.target_fingerprint == request_sha:
                    return CapabilityConsumeOutcome(
                        receipt=evaluation.receipt,
                        capability_id=row.capability_id,
                        verdict="blocked",
                        replayed=True,
                        violations=(),
                    )
                raise DalError(
                    DalErrorCode.IDEMPOTENCY_CONFLICT,
                    internal_detail="block key reused with different content",
                )
            _write_intent(
                session,
                scope=BLOCK_EFFECT_SCOPE,
                idempotency_key=f"{consume_key}:block",
                owner_feature_id=target["entity_id"],
                fingerprint=request_sha,
                capability_id=row.capability_id,
                now=now_dt,
            )
            append_audit_event(
                session,
                event_id=new_id(),
                trace_id=target["entity_id"],
                event_type=BLOCK_EVENT_TYPE,
                redacted_summary=BLOCK_SUMMARY,
                now=now_dt,
            )
            _write_outbox(
                session,
                owner_feature_id=target["entity_id"],
                event="blocked",
                payload={
                    "capability_id": row.capability_id,
                    "feature_id": target["entity_id"],
                    "violation_count": len(evaluation.violations),
                },
                now=now_dt,
            )
            return CapabilityConsumeOutcome(
                receipt=evaluation.receipt,
                capability_id=row.capability_id,
                verdict="blocked",
                replayed=False,
                violations=evaluation.violations,
            )

        # The winner's CAS: exactly one statement moves the row to consumed
        # with the consuming identity and instant, guarded on the state
        # version it read. Zero rows matched means a concurrent consumer won
        # first; re-reading through the ORM would show this session's own
        # snapshot, so the loop re-runs against fresh state instead.
        result = session.execute(
            update(CommitCapability)
            .where(CommitCapability.capability_id == row.capability_id)
            .where(CommitCapability.state_version == row.state_version)
            .where(CommitCapability.state == "issued")
            .values(
                state_version=row.state_version + 1,
                state="consumed",
                uses_consumed=row.uses_consumed + 1,
                consumed_by=consume_key,
                consumed_at=facts["now"],
            )
        )
        if result.rowcount != 1:
            raise _SnapshotRetry()

        _write_intent(
            session,
            scope=CONSUME_EFFECT_SCOPE,
            idempotency_key=f"{consume_key}:intent",
            owner_feature_id=target["entity_id"],
            fingerprint=request_sha,
            capability_id=row.capability_id,
            now=now_dt,
        )
        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=target["entity_id"],
            event_type=CONSUME_EVENT_TYPE,
            redacted_summary=CONSUME_SUMMARY,
            now=now_dt,
        )
        _write_outbox(
            session,
            owner_feature_id=target["entity_id"],
            event="consumed",
            payload={
                "capability_id": row.capability_id,
                "feature_id": target["entity_id"],
                "consumed_by": consume_key,
            },
            now=now_dt,
        )
        return CapabilityConsumeOutcome(
            receipt=evaluation.receipt,
            capability_id=row.capability_id,
            verdict="go",
            replayed=False,
        )

    with sessions() as session:
        try:
            # Default retry discipline: a lost write lock (SQLite refuses a
            # snapshot upgrade when another session committed) re-runs the
            # body against fresh state, where the fresh row classifies as
            # stale through the pure gate.
            return run_write_transaction(session, lambda: _body(session))
        except _SnapshotRetry:
            # The CAS matched zero rows without an engine error. Re-read
            # the row's committed state and classify against it: the
            # capability is now dead, so the honest answer is the same
            # zero-write stale refusal a fresh caller would get — or, if
            # the winner was this very identity, the original receipt.
            return _stale_after_race(engine, facts, consumed_by)


class _SnapshotRetry(Exception):
    """The CAS guard matched zero rows; the caller re-reads fresh state."""


def _stale_after_race(
    engine: Engine,
    facts: dict[str, Any],
    consumed_by: str,
) -> CapabilityConsumeOutcome:
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.scalars(
            select(CommitCapability).where(
                CommitCapability.capability_id
                == facts["presented"]["capability_id"]
            )
        ).first()
        if row is None:
            raise _invalid("consume names no persisted capability row")
        if row.consumed_by == consumed_by and row.consumed_at is not None:
            return CapabilityConsumeOutcome(
                receipt=OperationReceipt(ReceiptCode.APPLIED),
                capability_id=row.capability_id,
                verdict="go",
                replayed=True,
            )
        evaluation = commit_capability.consume_commit_capability(
            {
                "schema_version": commit_capability.CONSUME_FACTS_SCHEMA,
                "target": facts["target"],
                "capability": {
                    **_row_to_binding(row),
                    **_row_to_liveness(row),
                },
                "presented": facts["presented"],
                "now": facts["now"],
                "current_epoch": facts["current_epoch"],
                "current_lease_epoch": facts["current_lease_epoch"],
            }
        )
        if evaluation.receipt.code != ReceiptCode.CAPABILITY_STALE:
            # The only honest landing here is stale: the row we raced
            # against has already moved. Anything else means our own facts
            # were wrong before the race, and the pure gate has already
            # raised for that shape.
            raise _invalid("post-race row is not in a judged-dead state")
        return CapabilityConsumeOutcome(
            receipt=evaluation.receipt,
            capability_id=row.capability_id,
            verdict="stale",
            replayed=False,
        )
