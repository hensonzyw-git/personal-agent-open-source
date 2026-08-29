"""`DAL-030` (R09-A2): bounded review/fix loop — the round-level boundary.

The frozen Wave-3 evaluators each judge ONE review round: ``review_independence``
compares one coder↔reviewer pair, ``review_disposition`` recomputes one
disposition, ``post_fix_verdict`` and ``open_finding_set`` validate one
post-fix verdict. None of them sees the loop. This module owns what only the
loop can decide, as three uniform-signature pure gates whose call timing
follows the frozen binding contract (DAL021-024 §independence:
``session_id``/``independence_key`` do not exist before the provider call —
they are the controller's call-**after** attestation in
``dal.reviewer-session-binding/1.0``):

- ``open_review_fix_round`` — **call-before** round-entry gate. From facts
  that exist before any provider call (the completed-cycle history and the
  latest deterministic verification outcome) it judges, in order: the round
  budget, then the verification precondition, then the history's own drift
  invariants... the budget first, so the round-4 refusal lands
  ``needs_human`` before a fourth reviewer call can be spent.
- ``admit_round_reviewer`` — **call-after** admission gate. Once the call has
  returned and the controller has attested the reviewer's binding (session
  id, context binding digest, independence key), it re-runs the drift
  invariants and judges the cross-round independence matrix against the
  attested binding; only success allows the review receipt to be accepted.
  Any reuse is a clean ``POLICY_DENIED`` zero-write refusal.
- ``close_review_fix_round`` — round-outcome gate composing
  ``validate_post_fix_verdict`` + ``derive_open_finding_set`` (below).

The independence policy is exactly the frozen contract's, no more: every
*reviewer* (the original R_1 and each post-fix re-review) must differ on all
three binding fields from the original coder, every earlier reviewer and
every earlier coder — ``coder 不自证`` and ``首 reviewer 不复核自己的结论``
are the count-0 special cases. *Coders* carry no novelty requirement across
cycles: the same coder may produce successive fixes, and only the same-cycle
pair ``coder(k) ≠ reviewer(k)`` is enforced (round-1 review S1). Imposing a
coder-novelty rule here would brand a legal history as forged drift.

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
round numbers, prior identities or verification outcomes. Closed facts shapes
raise `DalError(INVALID_ARGUMENT)` on any drift, including **forged history**
(a prior record whose reviewer reuses an earlier identity, or a cycle whose
coder reviewed itself), because a malformed past is a controller bug, not a
policy outcome. Identity formats follow the frozen binding contract:
``independence_key`` MUST be 64-char lowercase hex (the frozen digest
representation of the HMAC-SHA256 derivation, DAL021-024 "digest 表示闭合");
``session_id`` is the provider's own session identifier, so only non-emptiness
is checkable here.

Untrusted evidence. The closer's ``git_result`` and ``reviewer_result`` are
provider/Git output. Their *envelope shapes* are validated by the frozen
sub-evaluators' own command checks; their *content members* (resolutions,
new findings) are pre-flighted here, because the frozen evaluators dereference
them directly (``item["status"]``, ``item["evidence_sha256"][0]``) — a
``None`` member or an empty evidence list would crash the composition, and a
member carrying extra fields could reach a legal verdict despite the
contract's closed per-item field sets. The pre-flight lands the frozen
``PROVIDER_CONTRACT_FAILURE`` block without dispatching; direct callers of
the frozen evaluators keep their own behaviour (hardening them in place is a
refreeze question, out of R09-A2 scope). A pure judge also cannot
cryptographically bind the opener's admitted reviewer to the closer's facts
across calls — the controller owns that CAS binding; the closer re-validates
every invariant the facts themselves can carry, including the full history
drift set (round-1 review B6). No I/O; no persistence.

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

#: The opener's closed facts — **call-before only**. Nothing here depends on
#: a provider call's outcome: the budget, the verification precondition and
#: the history invariants are all decidable before any reviewer is invoked,
#: which is what makes the round-4 refusal land before a fourth call can be
#: spent (round-1 review B1). No proposed-reviewer field exists at this stage:
#: per the frozen binding contract the reviewer's ``session_id`` /
#: ``independence_key`` do not exist until the controller attests them
#: call-after.
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
    }
)

#: The admission gate's closed facts — **call-after**. The controller has
#: invoked the reviewer and attested its binding; the gate judges the
#: cross-round independence matrix against that attested binding before the
#: review receipt may be accepted. The history facts mirror the opener's.
ADMIT_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "target",
        "review_fix_cycle_count",
        "round_records",
        "round_anchors",
        "original_review_result_sha",
        "original_coder_session_id",
        "original_coder_context_sha256",
        "original_coder_independence_key",
        "proposed_reviewer_session_id",
        "proposed_reviewer_context_sha256",
        "proposed_reviewer_independence_key",
    }
)

#: The closer's closed facts: the round target, the original coder's binding
#: identity (the closer re-validates the same history-drift contract as the
#: opener), the loop accounting, the two frozen evaluators' own fact objects
#: (validated by them, never here) and the round's evidence (the Git result
#: and the reviewer result, whose content the sub-evaluators judge
#: fail-closed). The closer judges only what the loop can see; the
#: sub-evaluators judge their own contracts.
CLOSE_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "target",
        "original_coder_session_id",
        "original_coder_context_sha256",
        "original_coder_independence_key",
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

#: The closed per-item field sets of the untrusted verdict members, mirroring
#: the frozen contract (DAL021-024 §verdict: resolutions carry exactly
#: ``finding_id,status,summary,evidence_sha256[]``; gap resolutions carry
#: ``acceptance_id,status,summary,evidence_sha256[]``; new findings carry the
#: ``dal.review-findings/1.0`` field set). The pre-flight enforces them
#: because the frozen evaluators dereference members directly without a
#: closed-shape check.
_RESOLUTION_FIELD_SETS: Final[frozenset[str]] = frozenset(
    {"finding_id", "status", "summary", "evidence_sha256"}
)
_GAP_FIELD_SETS: Final[frozenset[str]] = frozenset(
    {"acceptance_id", "status", "summary", "evidence_sha256"}
)
_NEW_FINDING_FIELD_SET: Final[frozenset[str]] = frozenset(
    {"category", "failure_scenario", "finding_id", "location", "severity", "summary"}
)

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
#: The frozen contract-block landing the composed evaluators emit for content
#: violations; the closer's crash pre-flight lands the same way (B5).
PROVIDER_REASON: Final[str] = "PROVIDER_CONTRACT_FAILURE"
PROVIDER_OWNER: Final[str] = "feature"
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
    """One completed-cycle record: closed shape and per-entry invariants.

    Identity formats follow the frozen binding contract: ``session_id`` is
    the provider's own session identifier (only non-emptiness is checkable);
    ``context_sha256`` and ``independence_key`` are digest outputs and MUST be
    64-char lowercase hex (round-1 review B2).
    """
    if not isinstance(record, dict) or frozenset(record) != RECORD_FIELDS:
        raise _invalid(f"round_records[{index}] shape is not closed")
    if record["cycle_no"] != index + 1 or isinstance(record["cycle_no"], bool):
        raise _invalid(f"round_records[{index}] cycle_no must be exactly {index + 1}")
    for field in ("reviewer_session_id", "coder_session_id"):
        if not _is_non_empty_str(record[field]):
            raise _invalid(f"round_records[{index}] {field} must be a non-empty string")
    for field in (
        "reviewer_context_sha256",
        "coder_context_sha256",
        "reviewer_independence_key",
        "coder_independence_key",
    ):
        if not _is_sha256_hex(record[field]):
            raise _invalid(
                f"round_records[{index}] {field} must be 64-char lowercase hex"
            )
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


def _original_coder(facts: dict[str, Any]) -> _IDENTITY:
    return (
        facts["original_coder_session_id"],
        facts["original_coder_context_sha256"],
        facts["original_coder_independence_key"],
    )


def _validate_identity_fields(
    facts: dict[str, Any], prefix: str, *, context: bool = True
) -> None:
    """Closed identity-field format checks for one `<prefix>_*` triple.

    ``session_id`` is the provider's own session identifier, so only
    non-emptiness is checkable (the frozen binding contract pins it to the
    provider's structured session output, which this judge cannot parse);
    digests must be 64-char lowercase hex (round-1 review B2).
    """
    if not _is_non_empty_str(facts.get(f"{prefix}_session_id")):
        raise _invalid(f"{prefix}_session_id must be a non-empty string")
    if context and not _is_sha256_hex(facts.get(f"{prefix}_context_sha256")):
        raise _invalid(f"{prefix}_context_sha256 must be 64-char lowercase hex")
    if not _is_sha256_hex(facts.get(f"{prefix}_independence_key")):
        raise _invalid(f"{prefix}_independence_key must be 64-char lowercase hex")


def _history_identities(
    records: list[dict[str, Any]], facts: dict[str, Any]
) -> list[tuple[str, _IDENTITY]]:
    """The labelled identity history a reviewer must be independent of.

    The frozen contract imposes novelty on **reviewers** only: the original
    coder plus every recorded reviewer and coder. Coders may repeat across
    cycles (the same fixer may produce successive fixes) — only the
    same-cycle pair ``coder(k) ≠ reviewer(k)`` is separately enforced
    (round-1 review S1).
    """
    identities: list[tuple[str, _IDENTITY]] = [("original coder", _original_coder(facts))]
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

    Every recorded **reviewer** must differ (on all three binding fields)
    from the original coder and from every earlier reviewer and coder; within
    one cycle the coder must differ from that cycle's reviewer (coder
    不自证). Coders carry no cross-cycle novelty (S1). A history that already
    contains a reviewer reuse equality could not have been produced by the
    gates, so its presence is INVALID_ARGUMENT.
    """
    seen: list[tuple[str, _IDENTITY]] = [("original coder", _original_coder(facts))]
    for index, record in enumerate(records):
        reviewer: _IDENTITY = (
            record["reviewer_session_id"],
            record["reviewer_context_sha256"],
            record["reviewer_independence_key"],
        )
        violations = _identity_violations(reviewer, seen)
        if violations:
            raise _invalid(f"round_records[{index}] reviewer " + "; ".join(violations))
        coder: _IDENTITY = (
            record["coder_session_id"],
            record["coder_context_sha256"],
            record["coder_independence_key"],
        )
        violations = _identity_violations(
            coder, [(f"cycle {index + 1} reviewer", reviewer)]
        )
        if violations:
            raise _invalid(f"round_records[{index}] coder " + "; ".join(violations))
        seen.append((f"cycle {index + 1} reviewer", reviewer))
        seen.append((f"cycle {index + 1} coder", coder))


def _proposed_reuse(
    records: list[dict[str, Any]], facts: dict[str, Any]
) -> tuple[str, ...]:
    """The cross-round independence matrix for the attested reviewer binding.

    The attested reviewer must differ (on all three binding fields) from the
    original coder and from every recorded reviewer and coder.
    """
    proposed: _IDENTITY = (
        facts["proposed_reviewer_session_id"],
        facts["proposed_reviewer_context_sha256"],
        facts["proposed_reviewer_independence_key"],
    )
    return tuple(
        f"proposed reviewer {violation}"
        for violation in _identity_violations(
            proposed, _history_identities(records, facts)
        )
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
    """Decide whether the next review round may open — **before any call**.

    Judged in order: the round budget (`count >= 3` → the BLK-POLICY block),
    then the verification precondition (only a succeeded verification returns
    the feature to `reviewing`). The budget is judged **first** so the
    round-4 refusal lands `needs_human` before a fourth reviewer call can be
    spent (round-1 review B1). Facts drift — including a status value or
    history shape that contradicts a feature in `reviewing` — raises;
    identity and independence are *not* judged here: the reviewer's binding
    does not exist until call-after (``admit_round_reviewer``).
    """
    if not isinstance(facts, dict) or frozenset(facts) != OPEN_FACT_FIELDS:
        raise _invalid("open facts shape is not closed")
    _validate_target(facts.get("target"))
    _validate_identity_fields(facts, "original_coder")

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

    status = facts["latest_verification_status"]
    if status not in VERIFICATION_STATUSES:
        raise _invalid("latest_verification_status is outside the closed set")
    if status == "blocked":
        raise _invalid(
            "latest_verification_status is blocked, which contradicts a feature "
            "in reviewing"
        )
    if status == "failed":
        return _policy_block(
            facts["target"],
            ("the latest fix verification failed; no review round may open",),
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


def admit_round_reviewer(facts: dict[str, Any]) -> ReviewFixLoopEvaluation:
    """Judge the attested reviewer binding — **after the call returns**.

    The controller has invoked the round's reviewer and attested its binding
    (``session_id``, ``context_binding_sha256``, ``independence_key`` from
    ``dal.reviewer-session-binding/1.0``). Before any review receipt may be
    accepted, the gate re-runs the history-drift invariants and judges the
    cross-round independence matrix against the attested binding: any field
    equality with the original coder or a recorded reviewer/coder is a clean
    ``POLICY_DENIED`` zero-write refusal; all violations are collected so the
    refusal is auditable. Budget and verification are *not* re-judged — the
    opener owns them and no new provider call happens between open and admit.
    """
    if not isinstance(facts, dict) or frozenset(facts) != ADMIT_FACT_FIELDS:
        raise _invalid("admit facts shape is not closed")
    _validate_target(facts.get("target"))
    _validate_identity_fields(facts, "original_coder")
    _validate_identity_fields(facts, "proposed_reviewer")

    count, records = _validate_accounting(facts)
    _history_reuse(records, facts)

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


def _preflight_verdict_members(verdict: dict[str, Any]) -> str | None:
    """Content pre-flight of the untrusted verdict members.

    The frozen sub-evaluators dereference resolution members directly
    (``item["status"]``, ``item["evidence_sha256"][0]``), so a ``None``
    member, a non-dict entry or an empty evidence list would crash the
    composition. A member carrying extra fields could reach a legal verdict
    despite the contract's closed per-item field sets. Returns a short
    drift label or ``None`` when every member is well-formed.
    """
    for field, fields in (
        ("finding_resolutions", _RESOLUTION_FIELD_SETS),
        ("acceptance_gap_resolutions", _GAP_FIELD_SETS),
    ):
        members = verdict.get(field)
        if not isinstance(members, list):
            return f"verdict {field} is not a list"
        for index, item in enumerate(members):
            if not isinstance(item, dict):
                return f"verdict {field}[{index}] is not an object"
            if frozenset(item) != fields:
                return f"verdict {field}[{index}] shape is not closed"
            evidence = item["evidence_sha256"]
            if not isinstance(evidence, list) or not evidence:
                return f"verdict {field}[{index}] evidence_sha256 is empty"
            if not all(_is_sha256_hex(digest) for digest in evidence):
                return f"verdict {field}[{index}] evidence_sha256 is malformed"
    members = verdict.get("new_findings")
    if not isinstance(members, list):
        return "verdict new_findings is not a list"
    for index, item in enumerate(members):
        if not isinstance(item, dict) or frozenset(item) != _NEW_FINDING_FIELD_SET:
            return f"verdict new_findings[{index}] shape is not closed"
    return None


def close_review_fix_round(facts: dict[str, Any]) -> ReviewFixLoopEvaluation:
    """Judge one post-fix re-review's outcome by composing the frozen gates.

    The post-fix verdict's structural invariants and the carry-forward open
    set are decided by ``validate_post_fix_verdict`` and
    ``derive_open_finding_set``; a block from either passes through verbatim.
    The loop adds only what they cannot see: the closed accounting and the
    full history-drift re-validation (the closer is a separate judge call —
    it never assumes a prior ``open``/``admit`` ran against the same facts),
    the round budget (`count` must name a round that could have opened —
    re-reviews round 2 or 3), the binding of the verdict to the recorded fix
    result, the current-round anchor agreement between the two sub-fact
    objects, and a content pre-flight of the untrusted verdict members
    (crash-shaped provider output lands the frozen contract block instead of
    raising).
    """
    if not isinstance(facts, dict) or frozenset(facts) != CLOSE_FACT_FIELDS:
        raise _invalid("close facts shape is not closed")
    _validate_target(facts.get("target"))
    _validate_identity_fields(facts, "original_coder")

    count, records = _validate_accounting(facts)
    _history_reuse(records, facts)
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

    #: Content pre-flight of the untrusted verdict members: crash-shaped
    #: output must fail closed as the frozen contract block, never raise out
    #: of a policy boundary (round-1 review B5).
    preflight = _preflight_verdict_members(verdict)
    if preflight is not None:
        return ReviewFixLoopEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.APPLIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(REVIEWING_STATE, BLOCK_STATE),
            final_state=BLOCK_STATE,
            final_entity_type=facts["target"]["entity_type"],
            final_reason_code=PROVIDER_REASON,
            final_reason_owner=PROVIDER_OWNER,
            declared_write_set=BLOCK_WRITE_SET,
            event_trace=(BLOCK_EVENT,),
            round_no=count + 1,
            reasons=(preflight,),
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
