"""`DAL-031`: the commit controller composition (R09-A3).

The production path from "the feature is verified" to "a candidate commit
exists, its capability is consumed, and the engine knows". This module is
the only composition of its parts, and the order is the safety property:

1. **Issue gate (pure)** — the trusted controller presents the binding; a
   malformed binding raises before anything touches the database.
2. **Issue persistence** — :func:`issue_commit_capability_row` writes the
   capability row plus intent/audit/outbox atomically; a replayed issue
   returns the original receipt.
3. **Fresh raw row read** — the executor's decision is judged against the
   database's real state, never the identity map (CLAUDE.md §5.2).
4. **Replay/liveness pre-checks** — a consumed-by-us row replays the
   original receipt without running git again; a dead row refuses stale
   with zero git calls.
5. **Executor subprocess** — outside every transaction (CLAUDE.md §5.2:
   never hold a transaction across a subprocess).
6. **Consume gate (pure) + CAS consume** — the presentation carries the
   executor's *actuals* (real touched paths, real result tree, trailers
   read back from the commit object); a diverged actual lands the block
   evidence rows through the store, and the capability stays unconsumed.
7. **Engine block transition** — on a block verdict, the controller also
   moves the feature ``verified -> needs_human`` through
   ``apply_transition`` with the frozen registry spec
   ``BLK-POLICY--verified`` (service actor, policy-engine evidence, no
   guard semantics). Store block evidence is a durable *pending* recovery
   record, never a completed landing: a retry finds the exact engine receipt
   or completes that transition without rerunning git.

Response-loss replay of the whole composition is answered at **two**
fences: the engine's idempotency-key replay returns the original block
receipt for a repeated block landing; the capability store's
``consumed_by`` identity replay returns the original consume receipt for
a repeated consume. Neither fence reruns git.

The controller passes the *binding* to the executor and the executor's
actuals to the gate. It never lets an executor self-report stand in for
git's read-back: every field the gate judges comes from the commit
object. The block landing writes a feature aggregate row — the
composition requires a real ``verified`` feature (seeded by the platform
before the controller runs); it never invents one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import commit_capability
from personal_agent_dal.machine import commit_capability_store
from personal_agent_dal.machine.commit_capability_store import (
    consume_commit_capability_row,
    issue_commit_capability_row,
)
from personal_agent_dal.machine.commit_executor import (
    CommitExecutorResult,
    run_candidate_commit,
)
from personal_agent_dal.machine.engine import TransitionCommand, apply_transition
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_dal.machine.registry import transition_registry
from personal_agent_dal.receipt import ReceiptCode
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    CommitCapability,
    ExternalEffect,
    TransitionReceipt,
)
from personal_agent_dal.storage.models import Feature

#: The block landing's evidence schema (frozen registry value for
#: BLK-POLICY--verified).
BLOCK_EVIDENCE_SCHEMA: str = "dal.evidence.policy-failure/1.0"
BLOCK_COMMAND_TYPE: str = "block_feature"
BLOCK_TARGET_STATE: str = "needs_human"
BLOCK_REASON: str = "POLICY_FAILURE"

#: The consume receipt's own idempotency key suffix, mirroring the engine's
#: ``<key>:effect`` shapes: the block transition's command key must be
#: stable across replays, so it derives from the issue key, not from the
#: run instant.
BLOCK_COMMAND_KEY: str = "block"


@dataclass(frozen=True)
class CommitControllerOutcome:
    """What one composed candidate-commit attempt did.

    ``phase`` names the last stage reached: ``issued`` (reserved), ``stale``
    (zero git), ``blocked`` (a commit exists; the gate blocked it; the
    feature is needs_human), ``consumed`` (a candidate commit exists and
    the capability is spent), or ``replayed`` (the original receipt, git
    not rerun). ``replayed`` is True only on the ``replayed`` phase.
    """

    phase: str
    capability_id: str
    commit_sha: str | None = None
    replayed: bool = False
    violations: tuple[str, ...] = field(default_factory=tuple)


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_epoch_seconds(value: object) -> bool:
    return type(value) is int and value >= 0


def _row_binding_view(row: CommitCapability) -> dict[str, Any]:
    """The executor's view of the persisted binding: the four trailers plus
    the base tree it must sit on. Read from the row, not from the caller's
    facts, so a drifted issue cannot redirect the executor."""
    import json

    return {
        "base_sha": row.base_sha,
        "trailers": json.loads(row.trailers_json),
    }


def _resolve_block_spec() -> dict[str, Any]:
    """The single registry spec for verified -> needs_human policy blocks,
    by exact tuple. An unknown or ambiguous resolution is a wiring defect,
    not a runtime condition to default away."""
    registry = transition_registry()
    matches = []
    for spec_id in registry.spec_ids:
        spec = registry.by_id(spec_id)
        if (
            spec["aggregate_type"] == "feature"
            and spec["from_state"] == commit_capability.VERIFIED_STATE
            and spec["command_type"] == BLOCK_COMMAND_TYPE
            and (spec["command_parameters"] or {}).get("target_state")
            == BLOCK_TARGET_STATE
            and spec["allowed_reason_codes"] == [BLOCK_REASON]
        ):
            matches.append(spec)
    if len(matches) != 1:
        raise _invalid(
            "block spec resolution is not unique: "
            f"{sorted(s['spec_id'] for s in matches)}"
        )
    return matches[0]


def _block_command(
    spec: dict[str, Any],
    *,
    entity_id: str,
    expected_version: int,
    idempotency_key: str,
) -> TransitionCommand:
    """The block command built from the spec's own binding, evidence and
    parameters (the same construction the frozen harness uses)."""
    binding = spec["actor_evidence_bindings"][0]
    return TransitionCommand(
        aggregate_type=spec["aggregate_type"],
        aggregate_id=entity_id,
        command_type=spec["command_type"],
        command_parameters=dict(spec["command_parameters"] or {}),
        actor_type=binding["actor_type"],
        evidence_source_types=tuple(binding["required_evidence_source_types"]),
        evidence_schema_versions=tuple(spec["required_evidence_schema_versions"]),
        decision_action=spec["requires_decision_action"],
        reason_code=(
            spec["allowed_reason_codes"][0] if spec["allowed_reason_codes"] else None
        ),
        expected_version=expected_version,
        idempotency_key=idempotency_key,
    )


def _apply_block_transition(
    engine,
    *,
    target: dict[str, Any],
    issue_key: str,
    now: int,
) -> None:
    """Land or replay the one engine transition for a durable block intent.

    Store evidence is a pending recovery record, not proof that the engine
    transition committed.  This helper is therefore used both directly after
    the store verdict and when a response-loss retry finds that pending record.
    """
    spec = _resolve_block_spec()
    command = _block_command(
        spec,
        entity_id=target["entity_id"],
        expected_version=target["version"],
        idempotency_key=f"{issue_key}:{BLOCK_COMMAND_KEY}",
    )
    block_outcome = apply_transition(
        engine, command, facts=GuardFacts({}), now=_utc_from_epoch(now)
    )
    if block_outcome.receipt_code != ReceiptCode.APPLIED.value:
        raise _invalid(
            "block transition did not land: "
            f"{block_outcome.receipt_code}/{block_outcome.evidence_validation_stage}"
        )
    if not block_outcome.duplicate and block_outcome.to_state != (
        commit_capability.BLOCK_STATE
    ):
        raise _invalid("block transition landed in an unexpected state")


def _has_block_receipt(engine, *, block_key: str) -> bool:
    sessions = session_factory(engine)
    with sessions() as session:
        return (
            session.scalars(
                sqlalchemy.select(TransitionReceipt).where(
                    TransitionReceipt.idempotency_key == block_key
                )
            ).first()
            is not None
        )


def _has_pending_block(
    engine,
    *,
    capability_id: str,
    feature_id: str,
    consumed_by: str,
) -> bool:
    """Find only this capability's durable, not-yet-engine-confirmed block."""
    sessions = session_factory(engine)
    with sessions() as session:
        return (
            session.scalars(
                sqlalchemy.select(ExternalEffect).where(
                    ExternalEffect.effect_scope_key
                    == commit_capability_store.BLOCK_EFFECT_SCOPE,
                    ExternalEffect.remote_idempotency_key == f"{consumed_by}:block",
                    ExternalEffect.capability_id == capability_id,
                    ExternalEffect.owner_aggregate_type == "feature",
                    ExternalEffect.owner_aggregate_id == feature_id,
                )
            ).first()
            is not None
        )


def _verify_live_verified_target(engine, target: dict[str, Any]) -> None:
    """Ensure a new capability is never issued for a missing/stale feature."""
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.execute(
            sqlalchemy.select(Feature.feature_id, Feature.state, Feature.version).where(
                Feature.feature_id == target["entity_id"]
            )
        ).first()
    if row is None or row.state != commit_capability.VERIFIED_STATE or (
        row.version != target["version"]
    ):
        raise _invalid("target is not the current verified feature")


def execute_candidate_commit(
    engine,
    repo_path,
    *,
    issue_facts: dict[str, Any],
    repository_id: str,
    now: int,
    current_epoch: int,
    current_lease_epoch: int,
    declared_paths: list[str],
    message: str,
    consumed_by: str,
) -> CommitControllerOutcome:
    """The full composition, in the frozen order above.

    ``declared_paths`` names the files whose new contents are already in
    the worktree; the executor stages exactly those paths and forms the
    candidate commit. Everything the consume gate judges arrives as the
    executor's git-read-back actuals: touched paths from the committed
    tree, the result tree from the commit object, the trailers parsed from
    the commit message bytes.
    """
    if not all(
        _is_epoch_seconds(value)
        for value in (now, current_epoch, current_lease_epoch)
    ):
        raise _invalid("controller times and epochs must be non-negative native integers")
    if type(consumed_by) is not str or not consumed_by:
        raise _invalid("consumed_by must be a non-empty native string")

    # --- 1. the pure issue gate (raises on malformed binding) --------
    commit_capability.issue_commit_capability(issue_facts)
    binding = issue_facts["binding"]
    target = issue_facts["target"]
    block_key = f"{binding['idempotency_key']}:{BLOCK_COMMAND_KEY}"

    # A completed block may legitimately have moved the feature away from
    # verified. Answer that exact receipt before enforcing the new-issue live
    # target precondition below.
    if _has_block_receipt(engine, block_key=block_key):
        sessions = session_factory(engine)
        with sessions() as session:
            row = session.scalars(
                sqlalchemy.select(CommitCapability).where(
                    CommitCapability.issue_idempotency_key
                    == binding["idempotency_key"]
                )
            ).first()
        if row is None:
            raise _invalid("block receipt exists without its capability row")
        return CommitControllerOutcome(
            phase="blocked",
            capability_id=row.capability_id,
            replayed=True,
        )
    _verify_live_verified_target(engine, target)

    # --- 2. issue persistence (its own replay fence) ------------------
    issue_commit_capability_row(engine, issue_facts, repository_id=repository_id)

    # --- 3. fresh raw read of the row (§5.2: identity-map safe) -------
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.scalars(
            sqlalchemy.select(CommitCapability).where(
                CommitCapability.capability_id == binding["capability_id"]
            )
        ).first()
    if row is None:
        raise _invalid("issued capability row vanished before execution")

    # --- 4. replay / liveness pre-checks (zero git calls) -------------
    if row.consumed_by == consumed_by and row.consumed_at is not None:
        return CommitControllerOutcome(
            phase="replayed",
            capability_id=row.capability_id,
            commit_sha=None,
            replayed=True,
        )
    block_key = f"{row.issue_idempotency_key}:{BLOCK_COMMAND_KEY}"
    if _has_block_receipt(engine, block_key=block_key):
        return CommitControllerOutcome(
            phase="blocked",
            capability_id=row.capability_id,
            commit_sha=None,
            replayed=True,
        )
    if _has_pending_block(
        engine,
        capability_id=row.capability_id,
        feature_id=issue_facts["target"]["entity_id"],
        consumed_by=consumed_by,
    ):
        _apply_block_transition(
            engine,
            target=issue_facts["target"],
            issue_key=row.issue_idempotency_key,
            now=now,
        )
        return CommitControllerOutcome(
            phase="blocked",
            capability_id=row.capability_id,
            commit_sha=None,
            replayed=True,
        )
    if (
        row.state != "issued"
        or row.uses_consumed >= row.max_uses
        or row.revoked_at is not None
        or now > row.expires_at
        or row.capability_epoch != current_epoch
        or row.lease_epoch != current_lease_epoch
    ):
        return CommitControllerOutcome(
            phase="stale",
            capability_id=row.capability_id,
        )

    # --- 5. the executor subprocess (no transaction held, §5.2) -------
    result = run_candidate_commit(
        repo_path,
        binding=_row_binding_view(row),
        declared_paths=declared_paths,
        message=message,
    )
    if result.refusal is not None:
        # A mechanical refusal forms no commit and judges nothing: the
        # capability stays issued and the feature stays verified. The
        # caller sees the refusal's fixed phrase and can re-issue later
        # (the issue key replays; the row is intact).
        return CommitControllerOutcome(
            phase="issued",
            capability_id=row.capability_id,
            commit_sha=None,
            violations=(f"executor_refused:{result.refusal.reason}",),
        )

    # --- 6. the consume gate + CAS over the executor's actuals --------
    presented = {
        "capability_id": row.capability_id,
        "approval_id": row.approval_id,
        "base_sha": row.base_sha,
        "result_sha": result.tree_sha,
        "touched_paths": list(result.touched_paths),
        "trailers": dict(result.trailers_readback),
        "idempotency_key": row.issue_idempotency_key,
    }
    capability_facts = {
        "capability_id": row.capability_id,
        "approval_id": row.approval_id,
        "lease_epoch": row.lease_epoch,
        "base_sha": row.base_sha,
        "result_sha": row.result_sha,
        "allowed_paths": _allowed_paths(row),
        "trailers": _trailers(row),
        "idempotency_key": row.issue_idempotency_key,
        "expires_at": row.expires_at,
        "max_uses": row.max_uses,
        "capability_epoch": row.capability_epoch,
        "uses_consumed": row.uses_consumed,
        "consumed_by": row.consumed_by,
        "revoked_at": row.revoked_at,
    }
    consume_facts = {
        "schema_version": commit_capability.CONSUME_FACTS_SCHEMA,
        "target": target,
        "capability": capability_facts,
        "presented": presented,
        "now": now,
        "current_epoch": current_epoch,
        "current_lease_epoch": current_lease_epoch,
    }
    consume_outcome = consume_commit_capability_row(
        engine, consume_facts, consumed_by=consumed_by
    )

    if consume_outcome.verdict == "blocked":
        # --- 7. the engine's block transition through the frozen registry.
        _apply_block_transition(
            engine,
            target=target,
            issue_key=row.issue_idempotency_key,
            now=now,
        )
        return CommitControllerOutcome(
            phase="blocked",
            capability_id=row.capability_id,
            commit_sha=result.commit_sha,
            replayed=False,
            violations=consume_outcome.violations,
        )

    if consume_outcome.verdict == "stale":
        # The capability died between the pre-check and the CAS (or the
        # pre-check's read raced another consumer). The candidate commit
        # exists as evidence; the capability stays where the store left it.
        return CommitControllerOutcome(
            phase="stale",
            capability_id=row.capability_id,
            commit_sha=result.commit_sha,
        )

    return CommitControllerOutcome(
        phase="consumed",
        capability_id=row.capability_id,
        commit_sha=result.commit_sha,
        replayed=False,
    )


def _utc_from_epoch(epoch: int):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _allowed_paths(row: CommitCapability) -> list[dict[str, Any]]:
    import json

    return json.loads(row.allowed_paths_json)


def _trailers(row: CommitCapability) -> dict[str, str]:
    import json

    return json.loads(row.trailers_json)
