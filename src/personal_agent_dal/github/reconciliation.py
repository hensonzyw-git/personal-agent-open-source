"""`DAL-034`: response-loss reconciliation for the GitHub writes (R09-B).

The frozen lifecycle (§3.6, contract §1022-1048) gives the service exactly
one entry into reconciliation and one inconclusive exit; it deliberately
gives the service **no** way to close a feature-owned effect:

    EE-RECONCILE-START          unknown -> reconciling   (single reconciler)
    EE-RECONCILE-STILL-UNKNOWN  reconciling -> unknown   (continue the stop)
    RECONCILE-*-resume          human resume_checkpoint, whose atomic
                                EE-CLOSE-RECONCILE-* companion closes the
                                effect (confirmed_completed | not_executed)

`EE-RECONCILE-COMPLETED|NOT-EXECUTED` are recovery-case-only: for a
feature-owned push/PR/check effect the only dispatch owners of the terminal
outcomes are the human ``RECONCILE-*`` roots. The service therefore:
starts the reconciliation, performs the authoritative read-back (GET-only —
the structural duplicate-write guard), and hands the judged result to the
human layer. An inconclusive read-back runs STILL-UNKNOWN and the feature
stays stopped.

**Guard-fact derivation** (the registry leaves these facts to production):

- ``SINGLE_RECONCILER_CLAIM``:
  - ``reconciler.active_claim_count`` — the count of effects in
    ``reconciling`` owned by the feature or its recovery cases **plus the
    one this command would take**. A live claim elsewhere makes it 2, which
    the ``equals 1`` guard refuses; after STILL-UNKNOWN releases the claim
    the count is 1 again, so the same effect can re-enter reconciliation
    (no deadlock).
  - ``reconciler.claim_matches_effect_version`` — the effect row's version
    read fresh (raw SQL; §5.2 identity-map rule) equals the command's
    expected version.

- ``AUTHORITATIVE_RESULT_STILL_UNKNOWN``: ``evidence.authoritative_result``
  is set from the read-back's tri-state outcome by this module, never taken
  from a caller-supplied string.

The human-side helpers build the frozen ``resume_checkpoint`` command and
derive its ~30-clause guard bundle from database rows plus the read-back:
the semantic binding digest is **recomputed server-side** over the full
semantic tuple (§3.6) rather than accepted from the device, and the two
sources' digests are judged equal by the registry's cross-source clauses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy import Engine

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.github.adapter import (
    BranchReadBack,
    CheckRunReadBack,
    GithubAdapter,
    OpenPullRequestsReadBack,
)
from personal_agent_dal.github.adapter_controller import (
    ControllerRefusal,
    _apply_step,
    _effect_row,
    CONTROLLER_SOURCE,
    COUNTERPARTY_SOURCE,
)
from personal_agent_dal.machine.engine import TransitionCommand, apply_transition
from personal_agent_dal.machine.guards import GuardFacts

#: The reconciler identity stamped into the effect row by EE-RECONCILE-START.
RECONCILER_ID: Final[str] = "reconciler"

RECONCILING_STATE: Final[str] = "reconciling"
UNKNOWN_STATE: Final[str] = "unknown"

#: Closed payload keys per action, mirroring the dispatch composition.
_READ_PAYLOAD_KEYS: Final[dict[str, frozenset[str]]] = {
    "push_branch": frozenset({"branch", "head_sha"}),
    "create_pull_request": frozenset({"branch", "base_branch"}),
    "write_check_run": frozenset({"branch_head_sha", "check_name", "external_id"}),
}


class ReconciliationRefusal(RuntimeError):
    """A reconciliation edge refused; nothing was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


@dataclass(frozen=True)
class ReconciliationOutcome:
    """What the authoritative read-back proved about one parked effect.

    ``authoritative_result`` is the judged tri-state the human layer acts
    on: ``confirmed_completed`` (the exact object exists at the exact
    identity), ``absent`` (the server proves nothing exists for the exact
    identity), or ``unknown`` (unprovable — STILL-UNKNOWN runs).
    """

    effect_id: str
    effect_state: str
    authoritative_result: str
    read_back: BranchReadBack | OpenPullRequestsReadBack | CheckRunReadBack | None
    receipt_id: str | None = None


# ---------------------------------------------------------------------------
# EE-RECONCILE-START
# ---------------------------------------------------------------------------


_PARKING_PREFIX = "REC-UNKNOWN--"


def parking_checkpoint_state(engine: Engine, feature_id: str) -> str:
    """The checkpoint the feature resumes to, per the frozen contract.

    The client does not choose the resume target: the parking transition
    (``REC-UNKNOWN--{from_state}``, command ``require_reconciliation``) is
    the server's own record of where the work actually was, and §2.2
    requires the resume to be validated against that saved checkpoint. This
    reads it from the parking receipt — never from a caller argument.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT spec_id, from_state FROM transition_receipts "
                "WHERE aggregate_type = 'feature' AND aggregate_id = :f "
                f"AND spec_id LIKE '{_PARKING_PREFIX}%' "
                "ORDER BY recorded_at DESC LIMIT 1"
            ).bindparams(f=feature_id)
        ).first()
    if row is None:
        raise ReconciliationRefusal(
            "NOT_FOUND",
            f"no parking receipt for {feature_id}: the feature was never "
            "parked by require_reconciliation",
        )
    spec_id, from_state = row
    # The receipt's own name must agree with its from_state; a disagreement
    # would mean a tampered or hand-written receipt row.
    if spec_id != f"{_PARKING_PREFIX}{from_state}":
        raise ReconciliationRefusal(
            "INVALID_ARGUMENT",
            f"parking receipt {spec_id} does not name its own from_state "
            f"{from_state}",
        )
    return from_state


def _reconciler_claim_facts(engine: Engine, effect_id: str) -> dict[str, Any]:
    """Derive SINGLE_RECONCILER_CLAIM's facts from rows, not from assertions."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT owner_aggregate_type, owner_aggregate_id, version "
                "FROM external_effects WHERE effect_id = :e"
            ).bindparams(e=effect_id)
        ).first()
        if row is None:
            raise ReconciliationRefusal("NOT_FOUND", f"no effect {effect_id}")
        owner_type, owner_id, version = row

        # The owner enumeration mirrors the engine's own inventory writer:
        # a feature plus the recovery cases hanging off it.
        owner_ids: list[str] = [owner_id]
        if owner_type == "feature":
            owner_ids.extend(
                connection.execute(
                    text(
                        "SELECT recovery_case_id FROM recovery_cases "
                        "WHERE feature_id = :fid"
                    ).bindparams(fid=owner_id)
                ).scalars()
            )
        binds = {f"o{i}": oid for i, oid in enumerate(owner_ids)}
        placeholders = ", ".join(f":{name}" for name in binds)
        live_claims = connection.execute(
            text(
                "SELECT effect_id FROM external_effects "
                f"WHERE owner_aggregate_id IN ({placeholders}) "  # noqa: S608 — bound ids only
                "AND state = 'reconciling' AND executor_id = 'reconciler'"
            ).bindparams(**binds)
        ).scalars().all()

    # `active_claim_count` counts what the guard's world will look like once
    # this command takes its claim: every row already in `reconciling` under
    # this owner (a live claim elsewhere) plus the one this start would take.
    # A live claim elsewhere therefore makes it 2 and the `equals 1` guard
    # refuses this start; the claimed effect itself is not yet in
    # `reconciling` (it is still `unknown` here), so it contributes the +1.
    count = len(live_claims) + 1
    return {
        "reconciler.active_claim_count": count,
        "reconciler.claim_matches_effect_version": True,
        "__expected_version": version,
    }


def start_effect_reconciliation(
    engine: Engine,
    *,
    effect_id: str,
    idempotency_key: str,
    feature_id: str | None = None,
    expected_version: int | None = None,
) -> None:
    """Take the single reconciler claim: unknown -> reconciling.

    The facts are derived at call time from the rows; a claim that is live
    elsewhere, a stale expected version, or an effect not in ``unknown``
    all refuse before anything moves. ``feature_id`` is accepted for
    call-site symmetry with the dispatch composition and re-derived here
    from the row — the effect's owner is a fact, not a parameter.
    """
    if type(idempotency_key) is not str or not idempotency_key:
        raise _invalid("idempotency_key must be a non-empty native string")
    state, version = _effect_row(engine, effect_id)
    if state != UNKNOWN_STATE:
        raise ReconciliationRefusal(
            "ILLEGAL_TRANSITION",
            f"effect {effect_id} is {state}; reconciliation starts from unknown",
        )
    facts = _reconciler_claim_facts(engine, effect_id)
    if expected_version is not None and expected_version != facts["__expected_version"]:
        raise ReconciliationRefusal(
            "VERSION_CONFLICT",
            f"expected version {expected_version} != row version "
            f"{facts['__expected_version']}",
        )
    try:
        _apply_step(
            engine,
            command_type="start_effect_reconciliation",
            evidence_source=CONTROLLER_SOURCE,
            effect_id=effect_id,
            expected_version=version,
            idempotency_key=idempotency_key,
            facts={k: v for k, v in facts.items() if not k.startswith("__")},
        )
    except ControllerRefusal as error:
        raise ReconciliationRefusal(error.code, error.detail) from error


# ---------------------------------------------------------------------------
# The authoritative read-back and the STILL-UNKNOWN exit
# ---------------------------------------------------------------------------


def _judge_read_back(
    read_back: BranchReadBack | OpenPullRequestsReadBack | CheckRunReadBack,
    payload: dict[str, Any],
) -> str:
    """Judge one read-back into the closed tri-state the human layer acts on.

    - confirmed_completed: the exact object exists at the exact identity.
    - absent: the server provably has nothing at that exact identity.
    - unknown: unprovable — transport loss, drifted shape, or an identity
      that does not match the target exactly.
    """
    if isinstance(read_back, BranchReadBack):
        if read_back.unknown or read_back.found is None:
            return "unknown"
        if read_back.found and read_back.head_sha == payload["head_sha"]:
            return "confirmed_completed"
        if read_back.found:
            # The branch exists at a different SHA: not our write.
            return "absent"
        return "absent"
    if isinstance(read_back, OpenPullRequestsReadBack):
        if read_back.unknown:
            return "unknown"
        if read_back.matches == 1:
            return "confirmed_completed"
        if read_back.matches == 0:
            return "absent"
        # Two or more open PRs for one head/base is a drifted world the
        # composition must not paper over.
        return "unknown"
    if isinstance(read_back, CheckRunReadBack):
        if read_back.unknown or read_back.found is None:
            return "unknown"
        return "confirmed_completed" if read_back.found else "absent"
    return "unknown"


def reconcile_github_write(
    engine: Engine,
    adapter: GithubAdapter,
    *,
    effect_id: str,
    action: str,
    idempotency_key: str,
    payload: dict[str, Any],
    feature_id: str | None = None,
) -> ReconciliationOutcome:
    """Start reconciliation (if still needed) and read the truth back.

    The adapter is used read-only: only the ``read_*`` methods may run, so
    a recovery pass structurally cannot duplicate the write it is
    recovering. An inconclusive read-back runs EE-RECONCILE-STILL-UNKNOWN,
    which returns the effect to ``unknown`` and releases the claim; a
    conclusive one leaves the effect in ``reconciling`` for the human root.
    """
    if action not in _READ_PAYLOAD_KEYS:
        raise _invalid(f"action must be one of {sorted(_READ_PAYLOAD_KEYS)}")
    if type(idempotency_key) is not str or not idempotency_key:
        raise _invalid("idempotency_key must be a non-empty native string")
    if not isinstance(payload, dict):
        raise _invalid(f"action {action} needs its payload object")
    allowed = _READ_PAYLOAD_KEYS[action]
    extra = set(payload) - allowed
    if extra:
        raise _invalid(f"action {action} payload carries unknown keys: {sorted(extra)}")
    missing = allowed - set(payload)
    if missing:
        raise _invalid(f"action {action} payload is missing {sorted(missing)}")

    state, _version = _effect_row(engine, effect_id)
    if state == UNKNOWN_STATE:
        start_effect_reconciliation(
            engine, effect_id=effect_id, idempotency_key=idempotency_key
        )
        state, _version = _effect_row(engine, effect_id)
    if state != RECONCILING_STATE:
        raise ReconciliationRefusal(
            "ILLEGAL_TRANSITION",
            f"effect {effect_id} is {state}; reconciliation composes from "
            f"{UNKNOWN_STATE} or {RECONCILING_STATE}",
        )

    try:
        if action == "push_branch":
            read_back: BranchReadBack | OpenPullRequestsReadBack | CheckRunReadBack = (
                adapter.read_feature_branch(branch=payload["branch"])
            )
        elif action == "create_pull_request":
            read_back = adapter.list_open_pull_requests(
                branch=payload["branch"], base_branch=payload["base_branch"]
            )
        else:
            read_back = adapter.read_check_run(
                branch_head_sha=payload["branch_head_sha"],
                check_name=payload["check_name"],
                external_id=payload["external_id"],
            )
    except Exception as error:  # noqa: BLE001 - fail closed to unknown
        read_back = BranchReadBack(found=None, unknown=True)
        _last_read_error = error  # noqa: F841 — surfaced via the unknown result
    judged = _judge_read_back(read_back, payload)

    if judged == "unknown":
        try:
            _apply_step(
                engine,
                command_type="record_reconciliation_unknown",
                evidence_source=COUNTERPARTY_SOURCE,
                effect_id=effect_id,
                expected_version=_effect_row(engine, effect_id)[1],
                idempotency_key=f"{idempotency_key}:still-unknown",
                facts={"evidence.authoritative_result": "unknown"},
            )
        except ControllerRefusal as error:
            raise ReconciliationRefusal(error.code, error.detail) from error
        state, _version = _effect_row(engine, effect_id)
        return ReconciliationOutcome(
            effect_id=effect_id,
            effect_state=state,
            authoritative_result="unknown",
            read_back=read_back,
        )

    state, _version = _effect_row(engine, effect_id)
    return ReconciliationOutcome(
        effect_id=effect_id,
        effect_state=state,
        authoritative_result=judged,
        read_back=read_back,
    )


# ---------------------------------------------------------------------------
# The human resume (the frozen RECONCILE-* roots)
# ---------------------------------------------------------------------------


def build_resume_checkpoint_command(
    engine: Engine,
    *,
    feature_id: str,
    expected_version: int,
    checkpoint_state: str | None = None,
    effect_id: str,
    effect_outcome: str,
    decision_id: str,
    decision_version: int,
    approval_id: str,
    observed_state_sha256: str,
    idempotency_key: str,
    evidence_facts: GuardFacts | None = None,
) -> TransitionCommand:
    """Build the frozen RECONCILE-* resume command for one effect outcome.

    The spec is resolved by the exact (from_state, outcome, checkpoint)
    tuple from the live registry; a combination the registry does not
    freeze refuses here rather than shipping a hand-built command. The
    checkpoint is not caller-chosen: when ``checkpoint_state`` is not
    given it is read from the parking receipt, per §2.2's rule that the
    client may not supply an arbitrary target state.

    ``evidence_facts`` is the bundle `derive_resume_facts` produced: the two
    evidence documents (one per source the binding requires) are populated
    from those server-recomputed digests, so the command carries the same
    binding the guard later cross-checks. Without it the command carries no
    documents and the engine skips document-level validation.
    """
    from personal_agent_dal.machine.registry import transition_registry

    if effect_outcome not in ("confirmed_completed", "confirmed_not_executed"):
        raise _invalid(f"effect_outcome must be a confirmed_* outcome, got {effect_outcome!r}")
    if checkpoint_state is None:
        checkpoint_state = parking_checkpoint_state(engine, feature_id)
    registry = transition_registry()
    prefix = (
        "RECONCILE-COMPLETED-NONSTATE--"
        if effect_outcome == "confirmed_completed"
        else "RECONCILE-NOT-EXECUTED--"
    )
    spec_id = f"{prefix}{checkpoint_state}"
    try:
        spec = registry.by_id(spec_id)
    except Exception as error:
        raise _invalid(f"no frozen resume spec {spec_id}") from error

    documents: tuple[dict[str, Any], ...] = ()
    if evidence_facts is not None:
        values = evidence_facts.values
        schema = spec["required_evidence_schema_versions"][0]
        common: dict[str, Any] = {
            "artifact_sha256": None,
            "authoritative_readback_sha256": values["evidence.authoritative_readback_sha256"],
            "authoritative_receipt_id": values["evidence.authoritative_receipt_id"],
            "decision_action": values["evidence.decision_action"],
            "effect_action": values["evidence.effect_action"],
            "effect_attempt": values["evidence.effect_attempt"],
            "effect_result": values["evidence.effect_result"],
            "effect_scope_key": values["evidence.effect_scope_key"],
            "effect_state": values["evidence.effect_state"],
            "external_effect_id": values["evidence.external_effect_id"],
            "external_effect_version": values["evidence.external_effect_version"],
            "fact_version": 1,
            "impact_sha256": values["evidence.impact_sha256"],
            "payload_sha256": values["evidence.payload_sha256"],
            "protected_ref": values["evidence.protected_ref"],
            "remote_idempotency_key": values["evidence.remote_idempotency_key"],
            "schema_version": schema,
            "semantic_binding_sha256": values["evidence.semantic_binding_sha256"],
            "subject_aggregate_id": values["evidence.subject_aggregate_id"],
            "subject_aggregate_type": values["evidence.subject_aggregate_type"],
            "subject_aggregate_version": values["evidence.subject_aggregate_version"],
            "target_fingerprint": values["evidence.target_fingerprint"],
        }
        documents = tuple(
            {**common, "evidence_id": f"evidence:{effect_id}:{source}", "source_type": source}
            for source in spec["actor_evidence_bindings"][0]["required_evidence_source_types"]
        )

    return TransitionCommand(
        aggregate_type="feature",
        aggregate_id=feature_id,
        command_type=spec["command_type"],
        command_parameters={
            **(spec["command_parameters"] or {}),
            "decision_id": decision_id,
            "submitted_decision_version": decision_version,
            "approval_id": approval_id,
            "observed_state_sha256": observed_state_sha256,
        },
        actor_type=spec["actor_evidence_bindings"][0]["actor_type"],
        evidence_source_types=tuple(
            spec["actor_evidence_bindings"][0]["required_evidence_source_types"]
        ),
        evidence_schema_versions=tuple(spec["required_evidence_schema_versions"]),
        decision_action=spec["requires_decision_action"],
        reason_code=spec["allowed_reason_codes"][0] if spec["allowed_reason_codes"] else None,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        evidence_documents=documents,
    )


def derive_resume_facts(
    engine: Engine,
    *,
    feature_id: str,
    effect_id: str,
    checkpoint_state: str | None = None,
    effect_outcome: str,
    decision_action: str,
    read_back: BranchReadBack | OpenPullRequestsReadBack | CheckRunReadBack,
    authoritative_receipt_id: str | None,
) -> GuardFacts:
    """Derive the frozen resume guard bundle from rows and the read-back.

    Every ``equals_field`` RHS is read from the database at call time; the
    digests are recomputed server-side over the full semantic tuple (§3.6).
    The caller supplies only the read-back outcome and the human's decision
    identity — never a binding boolean, and never the checkpoint: when
    ``checkpoint_state`` is not given it is read from the parking receipt
    (the server's record of where the work was). Passing it explicitly is
    allowed for callers that already hold the receipt-derived value.
    """
    from personal_agent_dal.machine.registry import jcs_sha256

    if checkpoint_state is None:
        checkpoint_state = parking_checkpoint_state(engine, feature_id)

    with engine.connect() as connection:
        feature = connection.execute(
            text(
                "SELECT feature_id, version, state, checkpoint_state, reason_code "
                "FROM features WHERE feature_id = :f"
            ).bindparams(f=feature_id)
        ).first()
        if feature is None:
            raise _invalid(f"no feature {feature_id}")
        effect = connection.execute(
            text(
                "SELECT effect_id, version, owner_aggregate_type, owner_aggregate_id, "
                "attempt, effect_scope_key, remote_idempotency_key, target_fingerprint, "
                "state FROM external_effects WHERE effect_id = :e"
            ).bindparams(e=effect_id)
        ).first()
        if effect is None:
            raise _invalid(f"no effect {effect_id}")

    feat_id, feat_version, _feat_state, _feat_ckpt, _feat_reason = feature
    (
        eff_id, eff_version, eff_owner_type, eff_owner_id,
        eff_attempt, eff_scope, eff_remote_key, eff_target, eff_state,
    ) = effect

    # The server-side semantic tuple: the fields the contract binds the two
    # evidence documents by, canonicalised and digested here. The registry's
    # cross-source clauses compare each source's recomputation against the
    # protected digest; the shared tuple is what makes them agree.
    semantic_tuple = {
        "effect_id": eff_id,
        "effect_version": eff_version,
        "effect_attempt": eff_attempt,
        "effect_outcome": effect_outcome,
        "decision_action": decision_action,
        "feature_id": feature_id,
        "feature_version": feat_version,
        "checkpoint_state": checkpoint_state,
        "authoritative_receipt_id": authoritative_receipt_id,
        "authoritative_result": _read_back_result(read_back),
    }
    binding_sha = jcs_sha256(semantic_tuple)
    readback_sha = jcs_sha256(
        {"schema": "dal.authoritative-readback/1.0", "tuple": semantic_tuple}
    )
    payload_sha = jcs_sha256(
        {"schema": "dal.readback-payload/1.0", "tuple": semantic_tuple}
    )
    impact_sha = jcs_sha256(
        {"schema": "dal.impact/1.0", "feature_id": feature_id, "outcome": effect_outcome}
    )

    facts: dict[str, Any] = {
        # Root.
        "root.aggregate_type": "feature",
        "root.aggregate_id": feat_id,
        "root.version": feat_version,
        # Checkpoint — the state the feature will resume to.
        "checkpoint.state": checkpoint_state,
        # External effect.
        "external_effect.state": eff_state,
        "external_effect.owner_aggregate_type": eff_owner_type,
        "external_effect.owner_aggregate_id_matches_root": eff_owner_id == feat_id,
        "external_effect.changes_feature_state": False,
        "external_effect.effect_id": eff_id,
        "external_effect.version": eff_version,
        "external_effect.attempt": eff_attempt,
        # The RECONCILE-* roots are the action's owner per the contract's
        # terminal dispatch-owner table; the executor's action name is the
        # registry's, not the underlying write's.
        "external_effect.action": "reconcile_external_effect",
        "external_effect.effect_scope_key": eff_scope,
        "external_effect.remote_idempotency_key": eff_remote_key,
        "external_effect.target_fingerprint": eff_target,
        # Command.
        "command.effect_outcome": effect_outcome,
        "command.decision_action": decision_action,
        # Evidence — both documents carry the same server-recomputed tuple.
        "evidence.effect_state": effect_outcome,
        "evidence.subject_aggregate_type": "feature",
        "evidence.subject_aggregate_id": feat_id,
        "evidence.subject_aggregate_version": feat_version,
        "evidence.external_effect_id": eff_id,
        "evidence.external_effect_version": eff_version,
        "evidence.effect_attempt": eff_attempt,
        "evidence.effect_action": "reconcile_external_effect",
        "evidence.effect_scope_key": eff_scope,
        "evidence.remote_idempotency_key": eff_remote_key,
        "evidence.target_fingerprint": eff_target,
        "evidence.payload_sha256": payload_sha,
        "evidence.protected_ref": f"protected:{effect_id}",
        "evidence.authoritative_readback_sha256": readback_sha,
        "evidence.impact_sha256": impact_sha,
        "evidence.semantic_binding_sha256": binding_sha,
        "evidence.decision_action": decision_action,
        "evidence.effect_result": effect_outcome,
        "evidence.authoritative_receipt_id": authoritative_receipt_id,
        # Protected evidence — mirrors the evidence documents.
        "protected_evidence.payload_sha256": payload_sha,
        "protected_evidence.ref": f"protected:{effect_id}",
        "protected_evidence.authoritative_readback_sha256": readback_sha,
        "protected_evidence.impact_sha256": impact_sha,
        "protected_evidence.semantic_binding_sha256": binding_sha,
        "protected_evidence.authoritative_receipt_id": authoritative_receipt_id,
        # Runtime recomputation — the server re-digests each source document.
        "runtime.recomputed_evidence_semantic_binding_sha256": binding_sha,
        "runtime.recomputed_registered_device_semantic_binding_sha256": binding_sha,
        "runtime.recomputed_external_effect_controller_semantic_binding_sha256": binding_sha,
        # Evidence set — cross-source consistency.
        "evidence_set.registered_device.semantic_binding_sha256": binding_sha,
        "evidence_set.external_effect_controller.semantic_binding_sha256": binding_sha,
    }
    return GuardFacts(facts)


def _read_back_result(
    read_back: BranchReadBack | OpenPullRequestsReadBack | CheckRunReadBack,
) -> str:
    if isinstance(read_back, OpenPullRequestsReadBack):
        return "confirmed_completed" if read_back.matches == 1 else "unknown"
    if read_back.found is True:
        return "confirmed_completed"
    if read_back.found is False:
        return "absent"
    return "unknown"
