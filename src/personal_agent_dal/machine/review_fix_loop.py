"""`DAL-030` (R09-A2): bounded review/fix loop — the round-level boundary.

The frozen Wave-3 evaluators each judge ONE review round: ``review_independence``
compares a single coder↔reviewer pair, ``review_disposition`` recomputes one
disposition, ``post_fix_verdict`` and ``open_finding_set`` validate one
post-fix verdict. None of them sees the loop. This module owns what only the
loop can decide:

- **round accounting** — whether another review round may open at all, from
  the controller-derived ``review_fix_cycle_count``;
- **the cross-round independence matrix** — every reviewer (the original R_1
  and each post-fix re-review) must be provably independent of *every* prior
  reviewer *and* every prior coder (the original candidate's coder and each
  fix's coder). ``coder 不自证`` and ``首 reviewer 不复核自己的结论`` are the
  two-round special cases; the matrix is the general rule (DAL021-024 §1.3-1.4:
  every review is a distinct fresh read-only context);
- **the exhaustion boundary** — max three rounds, exceeding → `needs_human`.

Round semantics (Henson's 2026-08-29 decision, convention **B**):
``review_fix_cycle_count`` counts **completed fix cycles** — a cycle k is
complete when fix k is applied and its deterministic verification succeeded
and the feature returned to `reviewing`. The review opened at count=k is
round k+1: k=0 opens the original review R_1 (whose outcome is recomputed by
``review_disposition`` on the dispatch graph's round-1 path), k≥1 opens the
post-fix re-review V_k (whose outcome this module's closer composes from
``validate_post_fix_verdict`` + ``derive_open_finding_set`` — their checks are
reused, never re-implemented). Three full review→fix→verify cycles run
automatically; the round-4 review entry is refused.

**Exhaustion landing (Henson's 2026-08-29 decision):** the refused round-4
entry blocks through the frozen `BLK-POLICY` row — `reviewing → needs_human`,
reason `POLICY_FAILURE`, owner `policy-engine`, `block_feature`, `APPLIED`, the
seven-write block set; the frozen recovery is `replan → planning` (a fresh
plan, never an in-place continuation). `REVIEW_LOOP_LIMIT` survives only as a
loop violation detail here and in the evidence, not as the reason code. The
frozen `BLK-LOOP` row (`fixing → needs_human`, reason `REVIEW_LOOP_LIMIT`,
``dispatch_graph.REVIEW_LOOP_LIMIT``) is untouched and keeps guarding the
human `continue_fix` path back into `fixing` at count ≥ 3. The two gates read
the same counter and guard different doors; they do not contradict.

Trusted-facts contract. Like ``patch_policy``, this module is a pure in-process
judge over facts the trusted controller derives from receipts, session
bindings and Git — never from provider claims: a provider may not self-report
round numbers, prior identities or verification outcomes. The closed facts
shapes raise `DalError(INVALID_ARGUMENT)` on any drift, including **forged
history** (a prior record whose reviewer reuses an earlier reviewer's or an
earlier coder's identity), because a malformed past is a controller bug, not a
policy outcome. Untrusted members of the closer's facts (``git_result``,
``reviewer_result``) are never judged here beyond shape — they are handed to
the two frozen evaluators, whose fail-closed block evaluations pass through
verbatim. No I/O; no persistence.

This module deliberately declares no operation spec id and registers nothing
in the frozen manifest or dispatch graph (the G2 guard family form, per the
R09-A1 precedent). The dispatch graph's frozen rows, oracles and the
``loop_limit_in_fixing`` variant are unchanged by this module. If the loop
gate is later promoted to a dispatchable operation or persisted as gate
evidence, that registration happens through the manifest tooling with an
explicit refreeze authorisation — never by an unregistered spec id in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine.open_finding_set import (
    OpenFindingSetEvaluation,
    derive_open_finding_set,
)
from personal_agent_dal.machine.post_fix_verdict import (
    PostFixVerdictEvaluation,
    validate_post_fix_verdict,
)
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

#: Mirrors ``dispatch_graph.REVIEW_LOOP_LIMIT`` (frozen at 3). Kept local so
#: this module stays free of the dispatch graph's import graph; the equality
#: of the two constants is enforced by test, not by import.
REVIEW_LOOP_LIMIT: Final[int] = 3

FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)

#: The opener's closed facts: the round target, round accounting, the
#: completed-cycle history, the anchor chain, the latest verification outcome,
#: the original coder's binding identity (the matrix must include the coder of
#: the candidate tree, not only the fix coders), and the proposed reviewer's
#: binding identity.
OPEN_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "target",
        "review_fix_cycle_count",
        "round_records",
        "round_anchors",
        "original_review_result_sha",
        "latest_verification_status",
        "original_coder_session_id",
        "original_coder_context_sha256",
        "original_coder_independence_key",
        "proposed_reviewer_session_id",
        "proposed_reviewer_context_sha256",
        "proposed_reviewer_independence_key",
        "proposed_reviewer_identity",
    }
)

#: The closer's closed facts: the round target, the same loop accounting, the
#: two frozen evaluators' own fact objects (validated by them, never here) and
#: the round's evidence (the Git result and the reviewer result, whose content
#: the sub-evaluators judge fail-closed). The closer judges only what the loop
#: can see; the sub-evaluators judge their own contracts.
CLOSE_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "target",
        "review_fix_cycle_count",
        "round_records",
        "round_anchors",
        "original_review_result_sha",
        "post_fix_verdict_facts",
        "open_finding_set_facts",
        "git_result",
        "reviewer_result",
    }
)

#: One completed fix cycle. ``verdict`` is the triggering review's outcome: a
#: completed cycle exists only after a `changes_requested` — a `verified`
#: review ends the loop and never enters this history. ``result_sha`` is fix
#: k's result tree r(k); ``verification_status`` is `succeeded` because an
#: unverified fix is not a completed cycle.
RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "cycle_no",
        "reviewer_session_id",
        "reviewer_context_sha256",
        "reviewer_independence_key",
        "coder_session_id",
        "coder_context_sha256",
        "coder_independence_key",
        "verdict",
        "result_sha",
        "verification_status",
    }
)

#: One round anchor, mirroring the frozen single-round shape
#: (``open_finding_set.ROUND_ANCHOR_FIELDS``): the tree re-review V_k anchors
#: to is r(k-1), the base of fix k's increment. Both fields carry r(k-1),
#: matching the frozen fixtures.
ANCHOR_FIELDS: Final[frozenset[str]] = frozenset(
    {"anchor_sha", "previous_result_sha"}
)

VERIFICATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"succeeded", "blocked", "failed"}
)
RECORD_VERDICTS: Final[frozenset[str]] = frozenset({"verified", "changes_requested"})

#: The frozen seven-write block set (the four-write base set plus the three
#: block-only writes), mirroring the family form. The legal four-write
#: transitions are never assembled here — they pass through verbatim from the
#: composed evaluators via ``_pass_through``.
BLOCK_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
    "decision_create",
    "decision_projection",
    "notification_outbox",
)

POLICY_REASON: Final[str] = "POLICY_FAILURE"
POLICY_OWNER: Final[str] = "policy-engine"
BLOCK_EVENT: Final[str] = "feature.blocked"
BLOCK_STATE: Final[str] = "needs_human"
REVIEWING_STATE: Final[str] = "reviewing"

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class ReviewFixLoopEvaluation:
    """The complete observable result of one loop-level decision.

    ``loop_violations`` carries this module's own policy findings (independence
    reuses, the exhaustion or verification-precondition detail);
    ``reasons`` carries a passed-through sub-evaluator's diagnostics verbatim
    and stays empty otherwise. On a zero-write refusal every trace field stays
    at its no-op value — the caller asserts exactly that.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, ...]
    final_state: str
    final_entity_type: str
    final_reason_code: str | None = None
    final_reason_owner: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    round_no: int | None = None
    loop_violations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _is_git_sha_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in _HEX for char in value)
    )


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_target(target: Any) -> None:
    if not isinstance(target, dict) or frozenset(target) != TARGET_FIELDS:
        raise _invalid("target shape is not closed")
    if (
        not isinstance(target.get("entity_id"), str)
        or not target["entity_id"]
        or target.get("entity_type") != "feature"
        or target.get("state") != REVIEWING_STATE
        or not _is_non_negative_int(target.get("version"))
    ):
        raise _invalid("review-fix-loop target must be a feature in reviewing")


def _validate_record(record: Any, index: int) -> None:
    """One completed-cycle record: closed shape and per-entry invariants."""
    if not isinstance(record, dict) or frozenset(record) != RECORD_FIELDS:
        raise _invalid(f"round_records[{index}] shape is not closed")
    if record["cycle_no"] != index + 1 or isinstance(record["cycle_no"], bool):
        raise _invalid(f"round_records[{index}] cycle_no must be exactly {index + 1}")
    for field in (
        "reviewer_session_id",
        "reviewer_independence_key",
        "coder_session_id",
        "coder_independence_key",
    ):
        if not _is_non_empty_str(record[field]):
            raise _invalid(f"round_records[{index}] {field} must be a non-empty string")
    for field in ("reviewer_context_sha256", "coder_context_sha256"):
        if not _is_sha256_hex(record[field]):
            raise _invalid(f"round_records[{index}] {field} must be 64 hex chars")
    if record["verdict"] not in RECORD_VERDICTS:
        raise _invalid(f"round_records[{index}] verdict is outside the closed set")
    if record["verdict"] == "verified":
        raise _invalid(
            f"round_records[{index}] records a verified review: a verified review "
            "ends the loop and never enters the completed-cycle history"
        )
    if record["verification_status"] not in VERIFICATION_STATUSES:
        raise _invalid(
            f"round_records[{index}] verification_status is outside the closed set"
        )
    if record["verification_status"] != "succeeded":
        raise _invalid(
            f"round_records[{index}] is not a completed cycle: verification "
            f"was {record['verification_status']!r}"
        )
    if not _is_git_sha_hex(record["result_sha"]):
        raise _invalid(f"round_records[{index}] result_sha must be a 40-char git sha")


def _validate_anchors(anchors: Any, count: int) -> list[dict[str, Any]]:
    """Closed anchor chain: len == count, every entry closed and well-formed."""
    if not isinstance(anchors, list) or len(anchors) != count:
        raise _invalid("round_anchors must be a list with one entry per completed cycle")
    checked: list[dict[str, Any]] = []
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict) or frozenset(anchor) != ANCHOR_FIELDS:
            raise _invalid(f"round_anchors[{index}] shape is not closed")
        for field in ANCHOR_FIELDS:
            if not _is_git_sha_hex(anchor[field]):
                raise _invalid(f"round_anchors[{index}] {field} must be a 40-char git sha")
        if anchor["anchor_sha"] != anchor["previous_result_sha"]:
            raise _invalid(
                f"round_anchors[{index}] anchor_sha and previous_result_sha diverge"
            )
        checked.append(anchor)
    return checked


def _validate_accounting(facts: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """Validate count, records and the anchor chain; return `(count, records)`."""
    count = facts["review_fix_cycle_count"]
    if not _is_non_negative_int(count):
        raise _invalid("review_fix_cycle_count must be a non-negative integer")
    records = facts["round_records"]
    if not isinstance(records, list) or len(records) != count:
        raise _invalid("round_records must have exactly one entry per completed cycle")
    for index, record in enumerate(records):
        _validate_record(record, index)
    _validate_anchors(facts["round_anchors"], count)
    if not _is_git_sha_hex(facts["original_review_result_sha"]):
        raise _invalid("original_review_result_sha must be a 40-char git sha")

    #: The anchor chain must equal the recorded result chain: anchor j points
    #: at r(j) — the previous cycle's result tree, or the original candidate
    #: tree for the first re-review.
    for index, anchor in enumerate(facts["round_anchors"]):
        expected = (
            facts["original_review_result_sha"]
            if index == 0
            else records[index - 1]["result_sha"]
        )
        if anchor["anchor_sha"] != expected:
            raise _invalid(
                f"round_anchors[{index}] does not match the recorded result chain"
            )
    return count, records


#: One binding identity: `(session_id, context_sha256, independence_key)`.
_IDENTITY = tuple[str, str, str]

_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "session",
    "context",
    "independence key",
)


def _identity_violations(
    proposed: _IDENTITY,
    identities: list[tuple[str, _IDENTITY]],
) -> list[str]:
    """Every field equality between one identity triple and a labelled history.

    All violations are collected so a refusal is auditable, not just binary.
    """
    violations: list[str] = []
    for role, prior in identities:
        for field, value, expected in zip(_IDENTITY_FIELDS, proposed, prior):
            if value == expected:
                violations.append(f"reuses the {role} {field}")
    return violations


def _history_identities(
    records: list[dict[str, Any]], facts: dict[str, Any]
) -> list[tuple[str, _IDENTITY]]:
    """The labelled identity history: original coder, then each cycle's pair."""
    identities: list[tuple[str, _IDENTITY]] = [
        (
            "original coder",
            (
                facts["original_coder_session_id"],
                facts["original_coder_context_sha256"],
                facts["original_coder_independence_key"],
            ),
        )
    ]
    for index, record in enumerate(records):
        identities.append(
            (
                f"cycle {index + 1} reviewer",
                (
                    record["reviewer_session_id"],
                    record["reviewer_context_sha256"],
                    record["reviewer_independence_key"],
                ),
            )
        )
        identities.append(
            (
                f"cycle {index + 1} coder",
                (
                    record["coder_session_id"],
                    record["coder_context_sha256"],
                    record["coder_independence_key"],
                ),
            )
        )
    return identities


def _history_reuse(records: list[dict[str, Any]], facts: dict[str, Any]) -> None:
    """A forged past is controller drift, not a policy outcome.

    Every recorded identity must differ (on all three binding fields) from
    every identity that preceded it — and within one cycle the coder must
    differ from that cycle's reviewer: coder 不自证. A history that already
    contains a reuse equality could not have been produced by the gates, so
    its presence is INVALID_ARGUMENT.
    """
    identities: list[tuple[str, _IDENTITY]] = [
        (
            "original coder",
            (
                facts["original_coder_session_id"],
                facts["original_coder_context_sha256"],
                facts["original_coder_independence_key"],
            ),
        )
    ]
    for index, record in enumerate(records):
        reviewer: _IDENTITY = (
            record["reviewer_session_id"],
            record["reviewer_context_sha256"],
            record["reviewer_independence_key"],
        )
        violations = _identity_violations(reviewer, identities)
        if violations:
            raise _invalid(
                f"round_records[{index}] reviewer " + "; ".join(violations)
            )
        coder: _IDENTITY = (
            record["coder_session_id"],
            record["coder_context_sha256"],
            record["coder_independence_key"],
        )
        violations = _identity_violations(
            coder, identities + [(f"cycle {index + 1} reviewer", reviewer)]
        )
        if violations:
            raise _invalid(f"round_records[{index}] coder " + "; ".join(violations))
        identities.append((f"cycle {index + 1} reviewer", reviewer))
        identities.append((f"cycle {index + 1} coder", coder))


def _proposed_reuse(
    records: list[dict[str, Any]], facts: dict[str, Any]
) -> tuple[str, ...]:
    """The cross-round independence matrix for the proposed reviewer.

    The proposed reviewer must differ (on all three binding fields) from the
    original coder and from every recorded reviewer and coder.
    """
    proposed: _IDENTITY = (
        facts["proposed_reviewer_session_id"],
        facts["proposed_reviewer_context_sha256"],
        facts["proposed_reviewer_independence_key"],
    )
    return tuple(
        f"proposed reviewer {violation}"
        for violation in _identity_violations(proposed, _history_identities(records, facts))
    )


def _policy_block(
    target: dict[str, Any], violations: tuple[str, ...]
) -> ReviewFixLoopEvaluation:
    """The frozen BLK-POLICY landing: reviewing → needs_human, APPLIED."""
    return ReviewFixLoopEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(REVIEWING_STATE, BLOCK_STATE),
        final_state=BLOCK_STATE,
        final_entity_type=target["entity_type"],
        final_reason_code=POLICY_REASON,
        final_reason_owner=POLICY_OWNER,
        declared_write_set=BLOCK_WRITE_SET,
        event_trace=(BLOCK_EVENT,),
        loop_violations=violations,
    )


def _pass_through(
    evaluation: PostFixVerdictEvaluation | OpenFindingSetEvaluation, round_no: int
) -> ReviewFixLoopEvaluation:
    """Project one sub-evaluator's outcome verbatim onto the loop evaluation.

    The receipt, traces, write set, reason code and owner are the frozen
    evaluators' own words — the loop adds only the round number. A legal
    outcome therefore carries the sub-evaluator's exact four-write transition
    (verified / fixing); a block carries its exact seven-write
    PROVIDER_CONTRACT_FAILURE landing with its reasons intact.
    """
    return ReviewFixLoopEvaluation(
        receipt=evaluation.receipt,
        state_trace=evaluation.state_trace,
        final_state=evaluation.final_state,
        final_entity_type=evaluation.final_entity_type,
        final_reason_code=evaluation.final_reason_code,
        final_reason_owner=evaluation.final_reason_owner,
        declared_write_set=evaluation.declared_write_set,
        event_trace=evaluation.event_trace,
        round_no=round_no,
        reasons=evaluation.reasons,
    )


def open_review_fix_round(facts: dict[str, Any]) -> ReviewFixLoopEvaluation:
    """Decide whether the next review round may open, and who may review.

    Judged in order: closed facts and the recorded chain (drift raises),
    the round budget (`count >= 3` → the BLK-POLICY block), the verification
    precondition (only a succeeded verification returns the feature to
    `reviewing`), then the cross-round independence matrix (any reuse is a
    clean zero-write refusal). A legal open writes nothing and moves nothing:
    it returns the round number the controller may compose.
    """
    if not isinstance(facts, dict) or frozenset(facts) != OPEN_FACT_FIELDS:
        raise _invalid("open facts shape is not closed")
    _validate_target(facts.get("target"))

    for field in (
        "original_coder_session_id",
        "original_coder_independence_key",
        "proposed_reviewer_session_id",
        "proposed_reviewer_independence_key",
        "proposed_reviewer_identity",
    ):
        if not _is_non_empty_str(facts[field]):
            raise _invalid(f"{field} must be a non-empty string")
    for field in (
        "original_coder_context_sha256",
        "proposed_reviewer_context_sha256",
    ):
        if not _is_sha256_hex(facts[field]):
            raise _invalid(f"{field} must be 64 hex chars")

    status = facts["latest_verification_status"]
    if status not in VERIFICATION_STATUSES:
        raise _invalid("latest_verification_status is outside the closed set")
    if status == "blocked":
        raise _invalid(
            "latest_verification_status is blocked, which contradicts a feature "
            "in reviewing"
        )

    count, records = _validate_accounting(facts)
    _history_reuse(records, facts)

    if count >= REVIEW_LOOP_LIMIT:
        return _policy_block(
            facts["target"],
            (
                f"review fix loop exhausted: {count} completed fix cycles, no "
                f"round may open automatically beyond {REVIEW_LOOP_LIMIT}",
            ),
        )

    if status == "failed":
        return _policy_block(
            facts["target"],
            ("the latest fix verification failed; no review round may open",),
        )

    violations = _proposed_reuse(records, facts)
    if violations:
        return ReviewFixLoopEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.POLICY_DENIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(REVIEWING_STATE,),
            final_state=REVIEWING_STATE,
            final_entity_type=facts["target"]["entity_type"],
            loop_violations=violations,
        )

    return ReviewFixLoopEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(REVIEWING_STATE,),
        final_state=REVIEWING_STATE,
        final_entity_type=facts["target"]["entity_type"],
        round_no=count + 1,
    )


def _current_anchor(facts: dict[str, Any], count: int) -> str:
    """r(count-1): the tree the round being closed anchored to."""
    if count == 1:
        return facts["original_review_result_sha"]
    return facts["round_records"][count - 2]["result_sha"]


def _sub_command(
    spec_id: str,
    first_action: str,
    target: dict[str, Any],
    sub_facts: dict[str, Any],
    git_result: dict[str, Any],
    reviewer_result: dict[str, Any],
) -> dict[str, Any]:
    """Assemble one frozen evaluator's command envelope.

    The sub-evaluators validate every field themselves; the synthesized
    operation/idempotency identifiers only need to be non-empty and
    deterministic — the controller's own keys travel on the controller's
    commands, not inside this judge.
    """
    return {
        "schema_version": "dal.test-operation-command/1.0",
        "operation_id": f"review_fix_loop_close/{first_action}",
        "idempotency_key": f"review_fix_loop_close/{first_action}",
        "operation_spec_id": spec_id,
        "actor_type": "service",
        "evidence_source_type": "review-controller",
        "input": {
            "schema_version": "dal.operation-input/1.0",
            "target": dict(target),
            "action_sequence": [{"command": first_action}, {"command": "record_review"}],
            "authoritative_facts": sub_facts,
            "injected_results": [git_result, reviewer_result],
        },
    }


def close_review_fix_round(facts: dict[str, Any]) -> ReviewFixLoopEvaluation:
    """Judge one post-fix re-review's outcome by composing the frozen gates.

    The post-fix verdict's structural invariants and the carry-forward open
    set are decided by ``validate_post_fix_verdict`` and
    ``derive_open_finding_set``; a block from either passes through verbatim.
    The loop adds only what they cannot see: the closed accounting, the
    budget (`count` must name a round that could have opened — re-reviews
    round 2 or 3), the binding of the verdict to the recorded fix result, and
    the current-round anchor agreement between the two sub-fact objects.
    """
    if not isinstance(facts, dict) or frozenset(facts) != CLOSE_FACT_FIELDS:
        raise _invalid("close facts shape is not closed")
    _validate_target(facts.get("target"))

    count, records = _validate_accounting(facts)
    if count < 1 or count >= REVIEW_LOOP_LIMIT:
        raise _invalid(
            f"closing requires a completable re-review round: count {count} names "
            "no post-fix review that the opener could have admitted"
        )

    for field in ("post_fix_verdict_facts", "open_finding_set_facts", "git_result",
                  "reviewer_result"):
        if not isinstance(facts[field], dict):
            raise _invalid(f"{field} must be an object")

    #: The verdict under judgment must be bound to the recorded fix result:
    #: re-review V_k reviews fix k's tree r(k).
    reviewer_result = facts["reviewer_result"]
    verdict = reviewer_result.get("verdict")
    if not isinstance(verdict, dict):
        raise _invalid("reviewer_result verdict must be an object")
    if verdict.get("result_sha") != records[count - 1]["result_sha"]:
        raise _invalid(
            "verdict result_sha is not bound to the recorded fix result tree"
        )

    #: Both sub-fact objects must anchor the same current round: r(count-1).
    current = _current_anchor(facts, count)
    expected_anchor = {"anchor_sha": current, "previous_result_sha": current}
    pfv_facts = facts["post_fix_verdict_facts"]
    openset_facts = facts["open_finding_set_facts"]
    for name, sub_facts in (
        ("post_fix_verdict_facts", pfv_facts),
        ("open_finding_set_facts", openset_facts),
    ):
        anchors = sub_facts.get("round_anchors") if isinstance(sub_facts, dict) else None
        if anchors != expected_anchor:
            raise _invalid(
                f"{name} round_anchors do not match the current round anchor"
            )

    pfv = validate_post_fix_verdict(
        _sub_command(
            "OP-FIXDIFF-001",
            "validate_post_fix_verdict",
            facts["target"],
            pfv_facts,
            facts["git_result"],
            reviewer_result,
        )
    )
    if pfv.final_state == BLOCK_STATE:
        return _pass_through(pfv, count + 1)

    openset = derive_open_finding_set(
        _sub_command(
            "OP-OPENSET-001",
            "derive_open_finding_set",
            facts["target"],
            openset_facts,
            facts["git_result"],
            reviewer_result,
        )
    )
    if openset.final_state == BLOCK_STATE:
        return _pass_through(openset, count + 1)

    #: Defensive fail-closed: both evaluators branch on the same declared
    #: verdict, so a disagreement cannot arise from conforming inputs — if it
    #: ever does, refuse rather than pick a winner.
    if openset.final_state != pfv.final_state:
        return _policy_block(
            facts["target"],
            ("the composed evaluators disagree on the round outcome",),
        )

    return _pass_through(pfv, count + 1)
