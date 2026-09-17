"""Intake: the source-agnostic producer that turns a task request into a Feature.

R09-B closed the loop's tail (state machine, GitHub executor, worker, checkpoint,
reconciliation), but nothing created a task: ``create_feature`` had no production
caller and ``enqueue_job`` was test-only. This module is the missing producer
half — it builds the frozen ``create_feature`` command (``SM-CREATE``), applies
it, and enqueues the job a Home Mac Worker can claim.

The core is deliberately **trigger-agnostic**. Phase A wires a single human entry
(the operator console ``intake`` verb); Phase B will read a GitHub issue and call
the same core. The core never assumes who issued the task. Either way it stops at
``intake`` + a pending ``Job`` — it plans nothing, writes nothing to a repo, and
opens no external side effect.

Contract facts this module is written against (all validated, not assumed):

- ``SM-CREATE`` (transition-spec-registry): ``command_type=create_feature``,
  ``aggregate_type=feature``, ``from_state=null``, ``to_state=intake``,
  ``command_parameters.target_state="intake"``, ``effect_outcome=null``,
  ``guard_id=null``, ``requires_decision_action=null`` — no guard, no approval
  token, no decision action. Only the actor/evidence binding is checked:
  actor ``service``, evidence source ``workflow-service``, evidence schema
  ``dal.evidence.feature/1.0``.
- ``apply_transition`` (engine.py) returns a ``TransitionOutcome``; it never
  raises for a refusal (``TransitionRefused`` is caught into an outcome with a
  non-``APPLIED`` ``receipt_code``). A replay returns ``duplicate=True`` with the
  original receipt's states.
- ``expected_version`` must be ``None`` on creation (engine.py compares
  ``command.expected_version != current_version`` where ``current_version`` is
  ``None`` for a fresh aggregate).
- ``enqueue_job`` (worker/queue.py) is the queue producer; ``base_sha`` must be a
  40-hex lowercase SHA (models.py ``_hex_of_length`` CHECK).

Partial-failure recovery: ``apply_transition`` commits its own transaction, then
``enqueue_job`` commits its own. If a first run created the feature but the
enqueue failed, the next run's ``apply_transition`` is a replay (``duplicate``)
and the core must then *enqueue* the missing job rather than refuse — so an
interrupted intake recovers instead of half-creating.

Idempotency is the intake episode key (F4, 2026-09-07 review):
``intake:{feature_id}`` — the same identity as the feature transition's
idempotency key, unique at the database (partial index on ``worker_jobs``).
The find-or-create runs inside ``enqueue_job``'s single write transaction, so
two concurrent identical intakes converge on one job instead of racing past a
separate lookup. A feature may legitimately gain further jobs later (a fix
cycle is different task text, hence a different ``feature_id`` and key);
deleting a job row frees its key, so recovery-after-delete re-enqueues. The
task body (F7) is persisted with the job in the same transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Engine

from personal_agent_core.manifest import sha256_of
from personal_agent_dal.machine.engine import (
    TransitionCommand,
    apply_transition,
)
from personal_agent_dal.machine.transition_types import ReceiptCodes
from personal_agent_dal.worker.queue import EnqueueConflict, enqueue_job

#: Frozen spec identity for the creation transition (transition-spec-registry).
_AGGREGATE_TYPE: Final[str] = "feature"
_COMMAND_TYPE: Final[str] = "create_feature"
_ACTOR_TYPE: Final[str] = "service"
_EVIDENCE_SOURCE: Final[str] = "workflow-service"
_EVIDENCE_SCHEMA: Final[str] = "dal.evidence.feature/1.0"
_COMMAND_PARAMETERS: Final[dict[str, object]] = {
    "effect_outcome": None,
    "target_state": "intake",
}
_INGEST_STATE: Final[str] = "intake"

_BASE_SHA_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{40}\Z")

#: The job's feature branch. The worker enforces this exact shape
#: (`poll_once.py` ``expected_branch = f"codex/feature-{feature_id}"``), so the
#: intake must produce it or a claimed job is refused. The branch is derived
#: server-side from the feature id — the operator never injects an arbitrary
#: branch (matches the R09-minimal "no arbitrary branch/body from request").
_BRANCH_PREFIX: Final[str] = "codex/feature-"


class IntakeRefusal(Exception):
    """A deterministic, fail-closed refusal with a stable code and message."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class IntakeOutcome:
    """What one intake produced."""

    feature_id: str
    job_id: str | None
    feature_state: str
    duplicate: bool


def _feature_id(repository_id: str, task_description: str, base_sha: str) -> str:
    """Content-addressed feature id: re-running the same intake is a replay."""
    return sha256_of(
        {
            "repository_id": repository_id,
            "task_description": task_description,
            "base_sha": base_sha,
        }
    )


def _validate_base_sha(base_sha: str) -> str:
    if not isinstance(base_sha, str) or not _BASE_SHA_RE.match(base_sha):
        raise IntakeRefusal("invalid", "base_sha must be a 40-char lowercase hex SHA")
    return base_sha


def _existing_job_id(engine: Engine, feature_id: str) -> str | None:
    """Deprecated lookup; the enqueue transaction now performs the
    find-or-create atomically under the unique intake key (F4, 2026-09-07
    review). Retained as a tombstone so an old import fails loudly instead of
    silently racing again."""
    raise NotImplementedError(
        "superseded by enqueue_job's intake-key find-or-create"
    )


def intake_task(
    engine: Engine,
    *,
    task_description: str,
    repository_id: str,
    base_sha: str,
    toolchain_ref: str,
    branch_name: str | None = None,
    pending_only: bool = False,
) -> IntakeOutcome:
    """Create a Feature at ``intake`` and enqueue a pending Job for it.

    Idempotent on the task identity (repository + description + base SHA): a
    re-run is a replay (``duplicate=True``) and returns the original feature and
    job rather than creating a second row. Fails closed on any refusal — a
    refusal produces nothing.
    """
    if not isinstance(task_description, str) or not task_description.strip():
        raise IntakeRefusal("invalid", "task_description must be non-empty")
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise IntakeRefusal("invalid", "repository_id must be non-empty")
    if not isinstance(toolchain_ref, str) or not toolchain_ref.strip():
        raise IntakeRefusal("invalid", "toolchain_ref must be non-empty")
    base_sha = _validate_base_sha(base_sha)

    feature_id = _feature_id(repository_id, task_description, base_sha)
    # Server-derived branch; the operator never supplies an arbitrary one.
    derived_branch = branch_name or f"{_BRANCH_PREFIX}{feature_id}"

    command = TransitionCommand(
        aggregate_type=_AGGREGATE_TYPE,
        aggregate_id=feature_id,
        command_type=_COMMAND_TYPE,
        command_parameters=dict(_COMMAND_PARAMETERS),
        actor_type=_ACTOR_TYPE,
        evidence_source_types=(_EVIDENCE_SOURCE,),
        evidence_schema_versions=(_EVIDENCE_SCHEMA,),
        decision_action=None,
        reason_code=None,
        expected_version=None,
        idempotency_key=f"intake:{feature_id}",
        evidence_documents=(),
    )
    if pending_only:
        # Same business transition, in the same transaction as durable intake
        # evidence. No WorkerJob exists until explicit execution confirmation.
        import hashlib
        from personal_agent_core.timeutil import utc_now
        from personal_agent_dal.machine.action_lifecycle import _transaction
        from personal_agent_dal.storage.worker_models import FeatureIntakeRequest
        from personal_agent_dal.storage.models import Feature
        def register(session):
            key = f"pending:{feature_id}"
            old = session.get(FeatureIntakeRequest, key)
            if old and old.toolchain_ref != toolchain_ref:
                raise IntakeRefusal('toolchain_conflict', 'registered toolchain differs')
            result = apply_transition(engine, command, transaction_session=session)
            if result.receipt_code != ReceiptCodes.APPLIED:
                raise IntakeRefusal(result.receipt_code, result.receipt_code)
            if not old:
                session.add(FeatureIntakeRequest(intake_key=key, feature_id=feature_id,
                    task_description=task_description,
                    task_description_sha256=hashlib.sha256(task_description.encode()).hexdigest(),
                    toolchain_ref=toolchain_ref, recorded_at=utc_now()))
            feature = session.get(Feature, feature_id)
            return IntakeOutcome(feature_id, None, feature.state, result.duplicate)
        return _transaction(engine, register)

    outcome = apply_transition(engine, command)
    if outcome.receipt_code != ReceiptCodes.APPLIED:
        raise IntakeRefusal(outcome.receipt_code, outcome.receipt_code)
    if outcome.to_state != _INGEST_STATE:
        raise IntakeRefusal(
            "unexpected_state", f"expected {_INGEST_STATE}, got {outcome.to_state}"
        )

    # Idempotent-enqueue with recovery, arbitrated by the unique intake key
    # inside one transaction (F4): a replay of a completed first run returns
    # the existing job; a replay after an interrupted first run (feature
    # created, enqueue failed) enqueues the missing job — deleting the row
    # freed the key. The task body is persisted in the same transaction (F7).
    try:
        job_id = enqueue_job(
            engine,
            feature_id=feature_id,
            repository_id=repository_id,
            base_sha=base_sha,
            branch_name=derived_branch,
            toolchain_ref=toolchain_ref,
            intake_key=f"intake:{feature_id}",
            task_description=task_description,
        )
    except EnqueueConflict as error:
        # The same task re-submitted under a different toolchain: the operator
        # explicitly changed the execution config, and silence would return
        # the old job as though nothing had. Refuse with a typed code.
        raise IntakeRefusal(
            "toolchain_conflict",
            "this task is already enqueued with a different toolchain_ref",
        ) from error

    return IntakeOutcome(
        feature_id=feature_id,
        job_id=job_id,
        feature_state=outcome.to_state,
        duplicate=outcome.duplicate,
    )
