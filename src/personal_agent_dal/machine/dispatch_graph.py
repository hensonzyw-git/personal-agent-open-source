"""`DAL-T-GRAPH-001`: controller-dispatch graph decision (DAL-025, G3).

Freeze package `DAL-Controller编排冻结包_v0.1.md` (§3–§7) closes the
controller-side composition — the graph orchestration — as a machine artifact.
This module is the offline pure decision that replays those rules: it maps

    (state, entity_type, facts, seam, stream, attempted_resulting_command,
     provider_attempted)

onto a `GraphDispatchEvaluation` — the node to enter, the handler sequence to
compose, the resulting command to hand to the transition executor, and the
transition the feature takes. It performs no I/O: the adapter subprocess, the
handler verdicts and the persistence all belong to the controller.

Two layers of rule and two distinct failure vocabularies are deliberate (§4.3):

- **Dispatch table** — the 23-state row decides *what the controller does*: the
  node, orchestration action, handler sequence and resulting command. A real
  command_type absent from its state's dispatch row is an *unknown dispatch
  tuple* (`CONTRACT_SCHEMA_INVALID`, controller-side).
- **Transition registry** — the 68-command registry decides *whether a command
  is legal at all*. A fabricated command that no transition spec names is an
  *illegal transition* (`ILLEGAL_TRANSITION`, registry-side).

They must not collapse: one is "a real command in the wrong state", the other
is "a command that does not exist". The fifteen `DAL-T-GRAPH-001` oracles freeze
both.

The trusted half (state, entity type, seam, receipt/cycle facts, the submitted
command, the provider-attempted flag) raises `DalError(INVALID_ARGUMENT)` on
drift; the provider stream is the adapter's output and is read shallowly and
defensively for routing signals only — deep validation belongs to
`consume_provider_stream`, which runs first inside every provider composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

#: The target aggregate is a `feature`; its transition receipt schema is the
#: frozen `dal.transition-receipt/1.0` (engine.RECEIPT_SCHEMAS["feature"]).
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: §4.1 / §4.2 write sets: the four-write accept set and the full seven-write
#: block set, frozen in the transition registry.
BASE_WRITES: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
)
FULL_WRITES: Final[tuple[str, ...]] = BASE_WRITES + (
    "decision_create",
    "decision_projection",
    "notification_outbox",
)

BLOCK_EVENT: Final[tuple[str, ...]] = ("feature.blocked",)
BLOCK_STATE: Final[str] = "needs_human"

#: §3 node taxonomy: closed partition of the 23 feature states. The boundary is
#: "can the controller advance on facts it already holds", so an external
#: read-back (provider) is `provider`, a controller-side verdict is
#: `deterministic`, a human/external approval is `gate`, and the two end states
#: are `terminal`.
NODE_BY_STATE: Final[dict[str, str]] = {
    "intake": "deterministic", "planning": "provider",
    "awaiting_plan_review": "gate", "approved": "deterministic",
    "coding": "provider", "verifying": "deterministic", "reviewing": "provider",
    "fixing": "provider", "verified": "deterministic", "awaiting_merge": "gate",
    "merged": "gate", "deployed": "gate", "completed": "terminal",
    "blocked_requirement": "gate", "blocked_usage": "gate", "blocked_auth": "gate",
    "blocked_test": "gate", "blocked_external_prerequisite": "gate",
    "blocked_unknown": "gate", "reconciliation_required": "gate",
    "needs_human": "gate", "paused": "gate", "cancelled": "terminal",
}

#: §4.2 deterministic orchestration action and resulting command per state. The
#: action names the controller-side validator/gate; the resulting command is the
#: handoff to the transition executor (only when no provider is attempted).
DETERMINISTIC_ORCHESTRATION: Final[dict[str, str]] = {
    "intake": "validate_feature_shape",
    "approved": "approval_validity_and_sha_binding",
    "verified": "merge_candidate_sha_binding",
    "verifying": "test_receipt_gate",
}
DETERMINISTIC_RESULT: Final[dict[str, str]] = {
    "intake": "start_plan",
    "approved": "start_provider",
    "verified": "record_merge_candidate",
    "verifying": "record_verification/pass",
}

#: The 68 real command_type names, mirrored from the frozen transition registry
#: (244 feature + 21 recovery_case + 11 external_effect specs). Membership here
#: — and nowhere else — is the §4.3 fabricated-command gate.
REAL_COMMAND_TYPES: Final[frozenset[str]] = frozenset({
    "accept_current_fact", "accept_deploy_result", "accept_merge_result",
    "approve_plan", "approve_recovery", "block_feature", "block_recovery",
    "block_recovery_investigation", "block_recovery_start", "block_unknown",
    "cancel_feature", "cancel_recovery", "claim_external_effect",
    "complete_after_deploy", "complete_verified_recovery", "complete_without_deploy",
    "continue_fix", "create_feature", "open_recovery", "open_recovery_case",
    "pause_feature", "rearm_external_effect", "record_deploy_approval",
    "record_deployment", "record_effect_dispatch", "record_effect_not_executed",
    "record_effect_unknown", "record_fix_result", "record_managed_merge",
    "record_merge_approval", "record_merge_candidate", "record_observed_deployment",
    "record_observed_merge", "record_plan", "record_provider_result",
    "record_reconciled_completed", "record_reconciled_not_executed",
    "record_reconciliation_unknown", "record_recovery_execution",
    "record_recovery_proposal", "record_review/findings", "record_review/pass",
    "record_unapproved_observed_deployment", "record_unapproved_observed_merge",
    "record_verification/blocked", "record_verification/fixable_fail",
    "record_verification/pass", "reinvestigate_recovery",
    "release_expired_effect_claim", "replace_recovery_proposal", "replan",
    "request_revision", "require_reconciliation", "resume_after_auth",
    "resume_after_prerequisite", "resume_checkpoint", "resume_frozen_route",
    "resume_with_budget", "retry_from_checkpoint", "start_approved_fallback",
    "start_effect_reconciliation", "start_managed_deploy", "start_managed_merge",
    "start_plan", "start_provider", "start_recovery", "supply_requirement",
    "verify_recovery",
})

#: §4.1 per-state dispatch-row resulting commands. A real command_type absent
#: from its state's row is an "unknown dispatch tuple" (`CONTRACT_SCHEMA_INVALID`),
#: distinct from a fabricated command the registry rejects as ILLEGAL_TRANSITION.
ROW_COMMANDS: Final[dict[str, frozenset[str]]] = {
    "intake": frozenset({"start_plan"}),
    "planning": frozenset({"record_plan"}),
    "awaiting_plan_review": frozenset({"approve_plan", "request_revision"}),
    "approved": frozenset({"start_provider"}),
    "coding": frozenset({"record_provider_result"}),
    "verifying": frozenset({"record_verification/pass", "record_verification/fixable_fail",
                            "record_verification/blocked"}),
    "reviewing": frozenset({"record_review/pass", "record_review/findings"}),
    "fixing": frozenset({"record_fix_result", "block_feature"}),
    "verified": frozenset({"record_merge_candidate"}),
    "awaiting_merge": frozenset({"record_merge_approval", "record_observed_merge",
                                 "record_unapproved_observed_merge", "start_managed_merge",
                                 "record_managed_merge"}),
    "merged": frozenset({"complete_without_deploy", "record_deploy_approval",
                         "start_managed_deploy", "record_observed_deployment",
                         "record_deployment", "record_unapproved_observed_deployment"}),
    "deployed": frozenset({"complete_after_deploy"}),
    "completed": frozenset(),
    "blocked_requirement": frozenset({"supply_requirement"}),
    "blocked_usage": frozenset({"resume_frozen_route", "start_approved_fallback"}),
    "blocked_auth": frozenset({"resume_after_auth"}),
    "blocked_test": frozenset({"continue_fix"}),
    "blocked_external_prerequisite": frozenset({"resume_after_prerequisite"}),
    "blocked_unknown": frozenset({"resume_checkpoint", "require_reconciliation"}),
    "reconciliation_required": frozenset({"resume_checkpoint", "accept_merge_result",
                                          "accept_deploy_result", "open_recovery"}),
    "needs_human": frozenset({"retry_from_checkpoint", "replan", "resume_with_budget",
                              "continue_fix", "accept_current_fact", "open_recovery_case",
                              "complete_verified_recovery"}),
    "paused": frozenset({"resume_checkpoint"}),
    "cancelled": frozenset(),
}

#: §6 adapter seam. `injected` (G3) must never spawn a subprocess; `subprocess`
#: (G4) must bind a preflight receipt before the adapter may run.
SEAMS: Final[frozenset[str]] = frozenset({"injected", "subprocess"})

#: The closed `controller_facts` shape the controller reads from the database.
FACT_FIELDS: Final[frozenset[str]] = frozenset({
    "cancel_receipt_present",
    "has_findings_receipt_since_plan",
    "preflight_receipt_present",
    "prior_findings_receipts",
    "record_plan_receipt_present",
    "review_fix_cycle_count",
})
BOOL_FACT_FIELDS: Final[frozenset[str]] = frozenset({
    "cancel_receipt_present",
    "has_findings_receipt_since_plan",
    "preflight_receipt_present",
    "record_plan_receipt_present",
})
NON_NEGATIVE_INT_FACT_FIELDS: Final[frozenset[str]] = frozenset({
    "prior_findings_receipts",
    "review_fix_cycle_count",
})

REVIEW_LOOP_LIMIT: Final[int] = 3


@dataclass(frozen=True)
class DispatchDecision:
    """What the controller does next for this state.

    `handler_sequence` and `round` are the provider composition; the other
    fields mirror the frozen `expected_dispatch` oracle shape exactly.
    """

    node_type: str
    orchestration_action: str | None
    handler_sequence: tuple[str, ...]
    resulting_command: str | None
    round: int | None
    seam: str

    def to_dict(self) -> dict[str, Any]:
        """The `expected_dispatch` shape the oracle freezes and the test judges."""
        return {
            "node_type": self.node_type,
            "orchestration_action": self.orchestration_action,
            "handler_sequence": list(self.handler_sequence),
            "resulting_command": self.resulting_command,
            "round": self.round,
            "seam": self.seam,
        }


@dataclass(frozen=True)
class DispatchTransition:
    """The feature transition the decision causes.

    `coverage_ref` is the threat-vector label (`BLK-CONTRACT--planning`,
    `SM-CANCEL--planning`, …) or `None` for a path with no named coverage
    family. `receipt` is `None` exactly for the zero-write decisions.
    """

    state_trace: tuple[str, ...]
    final_state: str
    final_entity_type: str
    final_reason_code: str | None
    final_reason_owner: str | None
    event_trace: tuple[str, ...]
    allowed_write_set: tuple[str, ...]
    receipt: OperationReceipt | None
    coverage_ref: str | None


@dataclass(frozen=True)
class GraphDispatchEvaluation:
    """The complete observable result of the pure dispatch decision."""

    dispatch: DispatchDecision
    transition: DispatchTransition


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate(
    state: Any,
    entity_type: Any,
    facts: Any,
    seam: Any,
    stream: Any,
    attempted_resulting_command: Any,
    provider_attempted: Any,
) -> None:
    """Validate the trusted half; raise `DalError(INVALID_ARGUMENT)` on drift."""
    if not isinstance(state, str) or state not in NODE_BY_STATE:
        raise _invalid("state is not a known feature state")
    if entity_type != "feature":
        raise _invalid("dispatch target must be a feature")

    if not isinstance(facts, Mapping) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("controller facts shape is not closed")
    for field in BOOL_FACT_FIELDS:
        if not isinstance(facts[field], bool):
            raise _invalid(f"controller fact {field} must be a boolean")
    for field in NON_NEGATIVE_INT_FACT_FIELDS:
        if not _is_non_negative_int(facts[field]):
            raise _invalid(f"controller fact {field} must be a non-negative integer")

    if not isinstance(seam, str) or seam not in SEAMS:
        raise _invalid("seam must be injected or subprocess")

    # The stream is the normalized adapter output; its container shape is
    # trusted, its contents are not.
    if stream is not None and not isinstance(stream, list):
        raise _invalid("injected provider stream must be a list or null")

    if attempted_resulting_command is not None and not isinstance(
        attempted_resulting_command, str
    ):
        raise _invalid("attempted resulting command must be a string or null")
    if not isinstance(provider_attempted, bool):
        raise _invalid("provider_attempted must be a boolean")


def _round(facts: Mapping[str, Any]) -> int:
    # §5.2: round = whether a record_review/findings receipt exists since the
    # current plan's record_plan receipt. `prior_findings_receipts` (an earlier
    # plan) must not steer this.
    return 2 if facts["has_findings_receipt_since_plan"] else 1


def _first_event(stream: Sequence[Any] | None) -> dict[str, Any] | None:
    """The first normalized provider event, or `None` if absent or malformed.

    The dispatch graph reads the stream only for routing signals; deep
    validation is `consume_provider_stream`'s job. A non-dict first element
    (anything but the `["cancelled"]` sentinel, handled earlier) carries no
    routing signal, so it reads as no signal rather than raising.
    """
    if not stream or not isinstance(stream[0], dict):
        return None
    return stream[0]


def _applied_receipt() -> OperationReceipt:
    return OperationReceipt(
        ReceiptCode.APPLIED, schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA
    )


def _evaluation(
    *,
    node_type: str,
    orchestration: str | None,
    handlers: Sequence[str],
    rnd: int | None,
    resulting: str | None,
    seam: str,
    trace: Sequence[str],
    events: Sequence[str],
    receipts: Sequence[Any],
    writes: Sequence[str],
    reason: str | None,
    coverage: str | None,
    entity_type: str,
) -> GraphDispatchEvaluation:
    """Assemble the frozen dataclass evaluation from the derived pieces."""
    transition = DispatchTransition(
        state_trace=tuple(trace),
        final_state=trace[-1],
        final_entity_type=entity_type,
        final_reason_code=reason,
        final_reason_owner="feature" if reason is not None else None,
        event_trace=tuple(events),
        allowed_write_set=tuple(writes),
        receipt=_applied_receipt() if receipts else None,
        coverage_ref=coverage,
    )
    decision = DispatchDecision(
        node_type=node_type,
        orchestration_action=orchestration,
        handler_sequence=tuple(handlers),
        resulting_command=resulting,
        round=rnd,
        seam=seam,
    )
    return GraphDispatchEvaluation(dispatch=decision, transition=transition)


def dispatch_decision(
    *,
    state: str,
    entity_type: str,
    facts: Mapping[str, Any],
    seam: str,
    stream: Sequence[Any] | None,
    attempted_resulting_command: str | None,
    provider_attempted: bool,
) -> GraphDispatchEvaluation:
    """Decide the graph composition and the feature transition for one state.

    The decision order is the frozen §4/§5/§6 order: classify the node, compute
    its baseline orchestration, then run the universal legality gates
    (fabricated command → `ILLEGAL_TRANSITION`; real-but-misrouted command →
    `CONTRACT_SCHEMA_INVALID`), then the provider-node threat vectors
    (cancel / empty / seam gate / drift / independence reuse / loop limit), then
    the resulting-command transition. Every branch fails closed: a provider
    contract break blocks to `needs_human` with the seven-write set.
    """
    _validate(
        state, entity_type, facts, seam, stream,
        attempted_resulting_command, provider_attempted,
    )

    node_type = NODE_BY_STATE[state]

    handlers: list[str] = []
    orchestration: str | None = None
    resulting: str | None = None
    rnd: int | None = None

    if node_type == "terminal":
        orchestration, resulting = None, None
    elif node_type == "deterministic":
        orchestration = DETERMINISTIC_ORCHESTRATION.get(state)
        resulting = None if provider_attempted else DETERMINISTIC_RESULT.get(state)
    elif node_type == "provider":
        if state == "planning":
            orchestration = "planning_compose"
            handlers = ["consume_provider_stream", "plan_cross_fields"]
        elif state == "reviewing":
            rnd = _round(facts)
            if rnd == 2:
                orchestration = "reviewing_compose_round_2"
                handlers = ["consume_provider_stream", "post_fix_verdict", "open_finding_set"]
            else:
                orchestration = "reviewing_compose_round_1"
                handlers = ["consume_provider_stream", "review_independence", "review_disposition"]
        else:
            orchestration = "coding_or_fixing_forward_ref"
        resulting = {"planning": "record_plan", "reviewing": "record_review/pass"}.get(state)

    # §4.3 universal fabricated-command gate: a command_type the 68-command
    # registry does not name is ILLEGAL_TRANSITION (registry-side).
    if attempted_resulting_command is not None and attempted_resulting_command not in REAL_COMMAND_TYPES:
        return _evaluation(
            node_type=node_type, orchestration=None, handlers=(),
            rnd=None, resulting=None, seam=seam, trace=(state,),
            events=(), receipts=(), writes=(), reason=None,
            coverage="ILLEGAL_TRANSITION--planning", entity_type=entity_type,
        )

    # §4.3 unknown dispatch tuple: a real command absent from this state's row
    # is CONTRACT_SCHEMA_INVALID (controller-side).
    if attempted_resulting_command is not None and attempted_resulting_command not in ROW_COMMANDS[state]:
        return _evaluation(
            node_type=node_type, orchestration="fail_closed_unknown_tuple", handlers=(),
            rnd=None, resulting=None, seam=seam, trace=(state,),
            events=(), receipts=(), writes=(), reason=None,
            coverage="CONTRACT_SCHEMA_INVALID--planning", entity_type=entity_type,
        )

    # A deterministic node cannot be driven by a provider attempt: reject with
    # its own gate action, zero writes.
    if node_type == "deterministic" and provider_attempted:
        return _evaluation(
            node_type=node_type, orchestration=orchestration, handlers=(),
            rnd=None, resulting=None, seam=seam, trace=(state,),
            events=(), receipts=(), writes=(), reason=None,
            coverage=None, entity_type=entity_type,
        )

    if node_type == "provider":
        # Cancelled: close without transition when a cancel receipt already
        # exists, otherwise block (no receipt means the cancel is unproven).
        if stream == ["cancelled"]:
            if facts["cancel_receipt_present"]:
                return _evaluation(
                    node_type=node_type, orchestration="close_without_transition", handlers=(),
                    rnd=None, resulting=None, seam=seam, trace=(state,),
                    events=(), receipts=(), writes=(), reason=None,
                    coverage="SM-CANCEL--planning", entity_type=entity_type,
                )
            return _evaluation(
                node_type=node_type, orchestration=orchestration, handlers=handlers,
                rnd=rnd, resulting=None, seam=seam, trace=(state, BLOCK_STATE),
                events=BLOCK_EVENT, receipts=(_applied_receipt(),), writes=FULL_WRITES,
                reason="PROVIDER_CONTRACT_FAILURE", coverage="BLK-CONTRACT--planning",
                entity_type=entity_type,
            )

        # Empty response: the provider returned no event to route on.
        if stream is not None and len(stream) == 0:
            return _evaluation(
                node_type=node_type, orchestration=orchestration, handlers=handlers,
                rnd=rnd, resulting=None, seam=seam, trace=(state, BLOCK_STATE),
                events=BLOCK_EVENT, receipts=(_applied_receipt(),), writes=FULL_WRITES,
                reason="PROVIDER_CONTRACT_FAILURE", coverage="BLK-CONTRACT--planning",
                entity_type=entity_type,
            )

        # §6 seam gate: a subprocess seam must bind a preflight receipt first.
        if seam == "subprocess" and not facts["preflight_receipt_present"]:
            return _evaluation(
                node_type=node_type, orchestration=orchestration, handlers=(),
                rnd=rnd, resulting=None, seam=seam, trace=(state, "blocked_auth"),
                events=BLOCK_EVENT, receipts=(_applied_receipt(),), writes=FULL_WRITES,
                reason="AUTH_REQUIRED", coverage="BLK-AUTH--planning",
                entity_type=entity_type,
            )

        first = _first_event(stream)

        # Drift: the provider's result contradicts the facts it was built on.
        if first is not None and first.get("drift"):
            return _evaluation(
                node_type=node_type, orchestration=orchestration, handlers=handlers,
                rnd=rnd, resulting=None, seam=seam, trace=(state, BLOCK_STATE),
                events=BLOCK_EVENT, receipts=(_applied_receipt(),), writes=FULL_WRITES,
                reason="PROVIDER_CONTRACT_FAILURE", coverage="BLK-CONTRACT--planning",
                entity_type=entity_type,
            )

        # Independence reuse: a reviewer may not adopt a reused disposition.
        if state == "reviewing" and first is not None and first.get("independence_reuse"):
            return _evaluation(
                node_type=node_type, orchestration="reviewing_compose_round_1",
                handlers=("consume_provider_stream", "review_independence", "review_disposition"),
                rnd=1, resulting=None, seam=seam, trace=(state,),
                events=(), receipts=(), writes=(), reason=None,
                coverage=None, entity_type=entity_type,
            )

        # Loop limit: the fix cycle has exhausted its guard.
        if state == "fixing" and facts["review_fix_cycle_count"] >= REVIEW_LOOP_LIMIT:
            return _evaluation(
                node_type=node_type, orchestration="fixing_compose_forward_ref", handlers=(),
                rnd=None, resulting="block_feature", seam=seam, trace=(state, BLOCK_STATE),
                events=BLOCK_EVENT, receipts=(_applied_receipt(),), writes=FULL_WRITES,
                reason="REVIEW_LOOP_LIMIT", coverage="BLK-LOOP--fixing",
                entity_type=entity_type,
            )

        # Reviewing disposition: round 2 binds the post-fix verdict, round 1 the
        # independence disposition. Either may route to findings or pass.
        if state == "reviewing":
            if rnd == 2:
                verdict = first.get("verdict", "verified") if first is not None else "verified"
                resulting = "record_review/pass" if verdict == "verified" else "record_review/findings"
            else:
                disposition = first.get("disposition", "approve") if first is not None else "approve"
                gaps = bool(first.get("acceptance_gaps")) if first is not None else False
                resulting = "record_review/findings" if (disposition == "request_changes" or gaps) else "record_review/pass"

    # The resulting-command transitions the feature. `record_plan` is the full
    # seven-write accept; the two review commands are the four-write accept.
    if resulting == "record_plan":
        return _evaluation(
            node_type=node_type, orchestration=orchestration, handlers=handlers,
            rnd=rnd, resulting="record_plan", seam=seam,
            trace=(state, "awaiting_plan_review"), events=("plan.ready",),
            receipts=(_applied_receipt(),), writes=FULL_WRITES, reason=None,
            coverage=None, entity_type=entity_type,
        )
    if resulting == "record_review/pass":
        return _evaluation(
            node_type=node_type, orchestration=orchestration, handlers=handlers,
            rnd=rnd, resulting="record_review/pass", seam=seam,
            trace=(state, "verified"), events=("review.completed",),
            receipts=(_applied_receipt(),), writes=BASE_WRITES, reason=None,
            coverage=None, entity_type=entity_type,
        )
    if resulting == "record_review/findings":
        return _evaluation(
            node_type=node_type, orchestration=orchestration, handlers=handlers,
            rnd=rnd, resulting="record_review/findings", seam=seam,
            trace=(state, "fixing"), events=("fix.requested",),
            receipts=(_applied_receipt(),), writes=BASE_WRITES, reason=None,
            coverage=None, entity_type=entity_type,
        )

    # No resulting command: a terminal/gate state, a deterministic handoff whose
    # transition belongs to the main transition registry, or a provider state
    # with nothing further to do. Zero writes, no receipt, no coverage family.
    return _evaluation(
        node_type=node_type, orchestration=orchestration, handlers=handlers,
        rnd=rnd, resulting=None, seam=seam, trace=(state,),
        events=(), receipts=(), writes=(), reason=None,
        coverage=None, entity_type=entity_type,
    )
