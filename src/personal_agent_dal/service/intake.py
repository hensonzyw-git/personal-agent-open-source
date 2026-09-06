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
interrupted intake recovers instead of half-creating. The job's ``feature_id``
is not unique, so the core looks up an existing job for the feature first and
only enqueues when none exists; this also makes the whole intake idempotent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Engine, select

from personal_agent_core.manifest import sha256_of
from personal_agent_dal.machine.engine import (
    TransitionCommand,
    apply_transition,
)
from personal_agent_dal.machine.transition_types import ReceiptCodes
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.worker_models import WorkerJob
from personal_agent_dal.worker.queue import enqueue_job

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
    job_id: str
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
    """The first job already enqueued for this feature, if any."""
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.execute(
            select(WorkerJob)
            .where(WorkerJob.feature_id == feature_id)
            .order_by(WorkerJob.created_at)
            .limit(1)
        ).scalar_one_or_none()
    return row.job_id if row is not None else None


def intake_task(
    engine: Engine,
    *,
    task_description: str,
    repository_id: str,
    base_sha: str,
    toolchain_ref: str,
    branch_name: str | None = None,
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
    outcome = apply_transition(engine, command)
    if outcome.receipt_code != ReceiptCodes.APPLIED:
        raise IntakeRefusal(outcome.receipt_code, outcome.receipt_code)
    if outcome.to_state != _INGEST_STATE:
        raise IntakeRefusal(
            "unexpected_state", f"expected {_INGEST_STATE}, got {outcome.to_state}"
        )

    # Idempotent-enqueue with recovery: enqueue only when this feature has no
    # job yet. A replay of a completed first run finds the job; a replay after
    # an interrupted first run (feature created, enqueue failed) enqueues it now.
    job_id = _existing_job_id(engine, feature_id)
    if job_id is None:
        job_id = enqueue_job(
            engine,
            feature_id=feature_id,
            repository_id=repository_id,
            base_sha=base_sha,
            branch_name=derived_branch,
            toolchain_ref=toolchain_ref,
        )

    return IntakeOutcome(
        feature_id=feature_id,
        job_id=job_id,
        feature_state=outcome.to_state,
        duplicate=outcome.duplicate,
    )
