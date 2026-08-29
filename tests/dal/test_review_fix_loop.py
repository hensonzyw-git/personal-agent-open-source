"""Adversarial tests for the bounded review/fix loop policy (DAL-030, R09-A2).

The loop module is a pure judge in the ``patch_policy`` family: no I/O, no
fixture composition, no manifest registration. The matrix below follows the
plan groups: (A) trusted-envelope drift, (B) the round budget boundary,
(C) the cross-round independence matrix, (D) chain consistency, (E) the
composed close outcome, (F) module hygiene.

Counting convention B and the POLICY_FAILURE exhaustion landing are Henson's
2026-08-29 decisions; the boundary pair (count=2 opens round 3, count=3
refuses round 4 through the frozen BLK-POLICY semantics) is asserted
explicitly. The close tests compose the frozen ``post_fix_verdict`` and
``open_finding_set`` evaluators for real: the golden facts are shaped so both
evaluators run their full validation, so a pass-through assertion proves the
loop did not swallow or rewrite their outcomes.
"""

from __future__ import annotations

import ast
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import review_fix_loop as loop_policy
from personal_agent_dal.machine.review_fix_loop import (
    close_review_fix_round,
    open_review_fix_round,
)
from personal_agent_dal.receipt import ReceiptCode

# --- deterministic identity and tree material -------------------------------


def _sha256_hex(namespace: str, label: str) -> str:
    return hashlib.sha256(f"{namespace}/{label}".encode("utf-8")).hexdigest()


def _ctx(label: str) -> str:
    return _sha256_hex("context", label)


def _key(label: str) -> str:
    return _sha256_hex("independence", label)


def _git_sha(label: str) -> str:
    return _sha256_hex("tree", label)[:40]


CAND = _git_sha("candidate")
FIX1 = _git_sha("fix-1")
FIX2 = _git_sha("fix-2")
FIX3 = _git_sha("fix-3")
FIX4 = _git_sha("fix-4")
E_FIX = _sha256_hex("evidence", "fix-diff-1")

BASE_WRITE_SET = ("aggregate", "business_event", "transition_receipt", "audit")
BLOCK_WRITE_SET = BASE_WRITE_SET + (
    "decision_create",
    "decision_projection",
    "notification_outbox",
)


def _shape(result):  # type: ignore[no-untyped-def]
    """Every observable field of one evaluation, for equality assertions."""
    return (
        result.receipt.code,
        result.receipt.schema_version,
        result.state_trace,
        result.final_state,
        result.final_entity_type,
        result.final_reason_code,
        result.final_reason_owner,
        result.declared_write_set,
        result.event_trace,
        result.round_no,
        result.loop_violations,
        result.reasons,
    )


def _target() -> dict:
    return {
        "entity_id": "feature-100",
        "entity_type": "feature",
        "state": "reviewing",
        "version": 4,
    }


def _record(cycle: int, reviewer: str, coder: str, result_sha: str) -> dict:
    return {
        "cycle_no": cycle,
        "reviewer_session_id": f"sess-{reviewer}",
        "reviewer_context_sha256": _ctx(reviewer),
        "reviewer_independence_key": _key(reviewer),
        "coder_session_id": f"sess-{coder}",
        "coder_context_sha256": _ctx(coder),
        "coder_independence_key": _key(coder),
        "verdict": "changes_requested",
        "result_sha": result_sha,
        "verification_status": "succeeded",
    }


def _records(count: int) -> list[dict]:
    labels = [
        ("reviewer-r1", "coder-fix1", FIX1),
        ("reviewer-r2", "coder-fix2", FIX2),
        ("reviewer-r3", "coder-fix3", FIX3),
        ("reviewer-r4", "coder-fix4", FIX4),
    ]
    return [_record(i + 1, *labels[i]) for i in range(count)]


def _anchors(count: int) -> list[dict]:
    results = [CAND, FIX1, FIX2, FIX3]
    return [
        {"anchor_sha": results[i], "previous_result_sha": results[i]}
        for i in range(count)
    ]


def _open_facts(
    count: int,
    *,
    status: str = "succeeded",
    proposed: str = "reviewer-proposed",
    records: list[dict] | None = None,
    anchors: list[dict] | None = None,
    target: dict | None = None,
) -> dict:
    """Golden open facts. At count=0 the last verification is the candidate's
    own deterministic verification (coding -> verifying -> reviewing), so
    `succeeded` is the true round-1 entry fact, not a synthetic one."""
    return {
        "target": target if target is not None else _target(),
        "review_fix_cycle_count": count,
        "round_records": records if records is not None else _records(count),
        "round_anchors": anchors if anchors is not None else _anchors(count),
        "original_review_result_sha": CAND,
        "latest_verification_status": status,
        "original_coder_session_id": "sess-coder-original",
        "original_coder_context_sha256": _ctx("coder-original"),
        "original_coder_independence_key": _key("coder-original"),
        "proposed_reviewer_session_id": f"sess-{proposed}",
        "proposed_reviewer_context_sha256": _ctx(proposed),
        "proposed_reviewer_independence_key": _key(proposed),
        "proposed_reviewer_identity": f"identity:{proposed}",
    }


def _finding(finding_id: str, anchor: str, line: int) -> dict:
    return {
        "category": "correctness",
        "failure_scenario": "the finding reproduces",
        "finding_id": finding_id,
        "location": {
            "anchor_sha": anchor,
            "line_end": line,
            "line_start": line,
            "path": "src/feature.py",
        },
        "severity": "major",
        "summary": f"summary of {finding_id}",
    }


def _resolution(finding_id: str, status: str) -> dict:
    return {
        "evidence_sha256": [E_FIX],
        "finding_id": finding_id,
        "status": status,
        "summary": f"{finding_id} {status}",
    }


def _git_result() -> dict:
    return {
        "anchor_translation_diff": "@@ -5,1 +5,1 @@\n-old5\n+old5",
        "anchor_tree_entry": {"mode": "100644", "type": "blob", "present": True},
        "current_tree_entry": {"mode": "100644", "type": "blob", "present": True},
        "increment_diff": "@@ -5,1 +5,1 @@\n-old5\n+new5",
        "path": "src/feature.py",
        "previous_tree_entry": {"mode": "100644", "type": "blob", "present": True},
        "source": "git_executor",
        "status": "completed",
    }


def _verdict(
    decl: str = "verified",
    *,
    resolutions: list[dict] | None = None,
    result_sha: str = FIX1,
    acceptance: bool = True,
    new_findings: list[dict] | None = None,
) -> dict:
    return {
        "acceptance_gap_resolutions": [],
        "acceptance_verified": acceptance,
        "finding_resolutions": (
            resolutions if resolutions is not None else [_resolution("F-001", "closed")]
        ),
        "new_findings": new_findings if new_findings is not None else [],
        "result_sha": result_sha,
        "schema_version": "dal.post-fix-verdict/1.0",
        "verdict": decl,
    }


def _chains(count: int, new_findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """`(pfv_chain, openset_chain)` for a re-review closing at `count`.

    The prior verdict chain holds only the post-fix verdicts so far — V_1 ..
    V_{count-1} — so at count=1 (the first re-review) it is empty and the
    open set is the original review's findings (frozen rule: "round 1, empty
    chain"). The newest verdict's new findings travel in the last entry.
    """
    entries = max(count - 1, 0)
    results = [FIX1, FIX2, FIX3][:entries]
    pfv_chain: list[dict] = []
    openset_chain: list[dict] = []
    for index in range(entries):
        findings = new_findings if index == entries - 1 else []
        pfv_chain.append(
            {
                "finding_resolutions": [],
                "new_findings": findings,
                "result_sha": results[index],
                "sequence": index + 1,
            }
        )
        openset_chain.append(
            {
                "new_findings": findings,
                "result_sha": results[index],
                "sequence": index + 1,
            }
        )
    return pfv_chain, openset_chain


def _pfv_facts(count: int, anchors: dict, chain: list[dict], finding_ids: list[str]) -> dict:
    return {
        "manifest_roles": {E_FIX: "fix_diff"},
        "original_review": {
            "acceptance_gap_ids": [],
            "finding_ids": finding_ids,
            "finding_locations": {
                finding_id: {
                    "anchor_sha": CAND,
                    "line_end": 5,
                    "line_start": 5,
                    "path": "src/feature.py",
                }
                for finding_id in finding_ids
            },
        },
        "plan_tasks": [
            {
                "acceptance_ids": ["A-001"],
                "allowed_paths": [{"path": "src/feature.py", "path_type": "file"}],
                "task_id": "T-001",
            }
        ],
        "plan_verification_ids": {"A-001": ["V-001"]},
        "prior_verdict_chain": chain,
        "round_anchors": anchors,
        "test_receipts": {},
    }


def _openset_facts(count: int, anchors: dict, chain: list[dict], recomputed: str, finding_ids: list[str]) -> dict:
    return {
        "manifest_roles": {E_FIX: "fix_diff"},
        "original_review": {"acceptance_gap_ids": [], "finding_ids": finding_ids},
        "prior_verdict_chain": chain,
        "recomputed_result_sha": recomputed,
        "round_anchors": anchors,
    }


def _close_facts(
    count: int,
    *,
    decl: str = "verified",
    resolutions: list[dict] | None = None,
    verdict_sha: str | None = None,
    acceptance: bool = True,
    new_findings: list[dict] | None = None,
    finding_ids: list[str] | None = None,
    git_result: dict | None = None,
    pfv_anchors: dict | None = None,
    openset_anchors: dict | None = None,
) -> dict:
    """Golden close facts. The sub-fact anchors default to the current round
    anchor r(count-1), the binding the opener's contract produced."""
    finding_ids = finding_ids if finding_ids is not None else ["F-001"]
    verdict_sha = verdict_sha if verdict_sha is not None else [FIX1, FIX2, FIX3, FIX4][count - 1]
    current = CAND if count == 1 else FIX1
    expected = {"anchor_sha": current, "previous_result_sha": current}
    new_findings = new_findings if new_findings is not None else [
        _finding(finding_ids[0], current, 5)
    ]
    pfv_chain, openset_chain = _chains(count, new_findings)
    return {
        "target": _target(),
        "review_fix_cycle_count": count,
        "round_records": _records(count),
        "round_anchors": _anchors(count),
        "original_review_result_sha": CAND,
        "post_fix_verdict_facts": _pfv_facts(
            count, pfv_anchors if pfv_anchors is not None else expected, pfv_chain, finding_ids
        ),
        "open_finding_set_facts": _openset_facts(
            count,
            openset_anchors if openset_anchors is not None else expected,
            openset_chain,
            verdict_sha,
            finding_ids,
        ),
        "git_result": git_result if git_result is not None else _git_result(),
        "reviewer_result": {
            "source": "reviewer",
            "status": "completed",
            "verdict": _verdict(
                decl,
                resolutions=resolutions,
                result_sha=verdict_sha,
                acceptance=acceptance,
                # The verdict under judgment carries no new findings in the
                # golden paths: the findings it must resolve travel in the
                # prior chain (count >= 2) or the original review (count = 1).
                new_findings=[],
            ),
        },
    }


# --- A. trusted envelope -----------------------------------------------------


def test_open_facts_shape_is_closed() -> None:
    facts = _open_facts(0)
    with pytest.raises(DalError) as raised:
        open_review_fix_round({**deepcopy(facts), "junk": True})
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT
    dropped = deepcopy(facts)
    del dropped["proposed_reviewer_identity"]
    with pytest.raises(DalError) as raised:
        open_review_fix_round(dropped)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_close_facts_shape_is_closed() -> None:
    facts = _close_facts(1)
    with pytest.raises(DalError) as raised:
        close_review_fix_round({**deepcopy(facts), "junk": True})
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT
    dropped = deepcopy(facts)
    del dropped["git_result"]
    with pytest.raises(DalError) as raised:
        close_review_fix_round(dropped)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "mutation",
    ["not_a_dict", "missing_target", "wrong_state", "extra_target_field", "bad_version"],
)
def test_open_rejects_drifted_target(mutation: str) -> None:
    facts = _open_facts(0)
    if mutation == "not_a_dict":
        with pytest.raises(DalError) as raised:
            open_review_fix_round(["facts"])  # type: ignore[list-item]
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT
        return
    if mutation == "missing_target":
        facts.pop("target")
    elif mutation == "wrong_state":
        facts["target"]["state"] = "fixing"
    elif mutation == "extra_target_field":
        facts["target"]["junk"] = 1
    elif mutation == "bad_version":
        facts["target"]["version"] = True
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "field, value",
    [
        ("proposed_reviewer_session_id", ""),
        ("proposed_reviewer_context_sha256", "nothex"),
        ("proposed_reviewer_independence_key", ""),
        ("proposed_reviewer_identity", ""),
        ("original_coder_context_sha256", _ctx("coder-original")[:63]),
    ],
)
def test_open_rejects_malformed_identity_fields(field: str, value: str) -> None:
    facts = _open_facts(0)
    facts[field] = value
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "mutation",
    ["extra_field", "bool_cycle_no", "wrong_cycle_no", "blocked_verification", "bad_result_sha"],
)
def test_open_rejects_malformed_records(mutation: str) -> None:
    facts = _open_facts(1)
    record = facts["round_records"][0]
    if mutation == "extra_field":
        record["junk"] = 1
    elif mutation == "bool_cycle_no":
        record["cycle_no"] = True
    elif mutation == "wrong_cycle_no":
        record["cycle_no"] = 2
    elif mutation == "blocked_verification":
        record["verification_status"] = "blocked"
    elif mutation == "bad_result_sha":
        record["result_sha"] = _ctx("not-a-git-sha")
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "count, status",
    [(True, "succeeded"), (-1, "succeeded"), (0, "unknown"), (0, "blocked")],
)
def test_open_rejects_bad_count_and_status(count: int, status: str) -> None:
    facts = _open_facts(0 if not isinstance(count, bool) else 0, status=status)
    facts["review_fix_cycle_count"] = count
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# --- B. round budget boundary ------------------------------------------------


def test_count_zero_opens_the_original_review_round() -> None:
    result = open_review_fix_round(_open_facts(0))
    assert _shape(result) == _shape(
        open_review_fix_round(_open_facts(0))
    )  # sanity for the golden itself
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "reviewing"
    assert result.state_trace == ("reviewing",)
    assert result.declared_write_set == ()
    assert result.round_no == 1
    assert result.loop_violations == ()
    assert result.final_reason_code is None


def test_count_two_opens_round_three() -> None:
    result = open_review_fix_round(_open_facts(2, proposed="reviewer-r3"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "reviewing"
    assert result.declared_write_set == ()
    assert result.round_no == 3
    assert result.loop_violations == ()


def test_count_three_refuses_round_four_with_policy_failure() -> None:
    result = open_review_fix_round(_open_facts(3, proposed="reviewer-r4"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.state_trace == ("reviewing", "needs_human")
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "policy-engine"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert result.event_trace == ("feature.blocked",)
    assert result.round_no is None
    assert len(result.loop_violations) == 1
    assert "exhausted" in result.loop_violations[0]


def test_count_beyond_limit_blocks_the_same_way() -> None:
    result = open_review_fix_round(_open_facts(4, proposed="reviewer-r5"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "policy-engine"
    assert result.declared_write_set == BLOCK_WRITE_SET


def test_count_must_equal_records_length() -> None:
    facts = _open_facts(1)
    facts["round_records"] = []
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    facts = _open_facts(0)
    facts["round_records"] = _records(1)
    facts["round_anchors"] = _anchors(1)
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_close_requires_a_closable_round() -> None:
    for count in (0, 3):
        with pytest.raises(DalError) as raised:
            close_review_fix_round(_close_facts(count))
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# --- C. cross-round independence matrix --------------------------------------


@pytest.mark.parametrize(
    "historical, field",
    [
        ("original coder", "session"),
        ("original coder", "context"),
        ("original coder", "independence key"),
        ("cycle 1 reviewer", "session"),
        ("cycle 1 reviewer", "context"),
        ("cycle 1 reviewer", "independence key"),
        ("cycle 1 coder", "session"),
        ("cycle 1 coder", "context"),
        ("cycle 1 coder", "independence key"),
    ],
)
def test_reusing_any_historical_identity_field_is_denied(
    historical: str, field: str
) -> None:
    """The proposed reviewer must differ on every binding field from every
    historical reviewer and coder, including the original candidate's coder."""
    facts = _open_facts(1)
    if field == "session":
        source = "session_id"
    elif field == "context":
        source = "context_sha256"
    else:
        source = "independence_key"
    if historical == "original coder":
        facts[f"proposed_reviewer_{source}"] = facts[f"original_coder_{source}"]
    else:
        record = facts["round_records"][0]
        prefix = "reviewer" if historical.endswith("reviewer") else "coder"
        facts[f"proposed_reviewer_{source}"] = record[f"{prefix}_{source}"]
    result = open_review_fix_round(facts)
    assert result.receipt.code is ReceiptCode.POLICY_DENIED
    assert result.final_state == "reviewing"
    assert result.state_trace == ("reviewing",)
    assert result.declared_write_set == ()
    assert result.round_no is None
    assert result.final_reason_code is None
    assert result.loop_violations == (
        f"proposed reviewer reuses the {historical} {field}",
    )


def test_all_reuse_violations_are_collected() -> None:
    facts = _open_facts(1)
    record = facts["round_records"][0]
    facts["proposed_reviewer_session_id"] = record["reviewer_session_id"]
    facts["proposed_reviewer_context_sha256"] = record["reviewer_context_sha256"]
    facts["proposed_reviewer_independence_key"] = record["reviewer_independence_key"]
    result = open_review_fix_round(facts)
    assert result.receipt.code is ReceiptCode.POLICY_DENIED
    assert result.loop_violations == (
        "proposed reviewer reuses the cycle 1 reviewer session",
        "proposed reviewer reuses the cycle 1 reviewer context",
        "proposed reviewer reuses the cycle 1 reviewer independence key",
    )


def test_forged_history_reviewer_reuse_fails_closed() -> None:
    facts = _open_facts(1)
    facts["round_records"][0]["reviewer_session_id"] = "sess-coder-original"
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_forged_history_self_review_fails_closed() -> None:
    """coder 不自证 is a historical invariant too: a cycle whose coder equals
    its own reviewer could not have been produced by the gates."""
    facts = _open_facts(1)
    record = facts["round_records"][0]
    record["coder_session_id"] = record["reviewer_session_id"]
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_forged_history_repeat_reviewer_fails_closed() -> None:
    facts = _open_facts(2)
    facts["round_records"][1]["reviewer_session_id"] = "sess-reviewer-r1"
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_fully_distinct_matrix_at_count_two_passes() -> None:
    result = open_review_fix_round(_open_facts(2, proposed="reviewer-r3"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.round_no == 3
    assert result.loop_violations == ()


# --- D. chain consistency ----------------------------------------------------


def test_anchor_chain_must_match_recorded_results() -> None:
    facts = _open_facts(1)
    facts["round_anchors"][0]["anchor_sha"] = _git_sha("wrong")
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_anchor_list_length_must_match_count() -> None:
    facts = _open_facts(1)
    facts["round_anchors"] = []
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_anchor_entry_halves_must_agree() -> None:
    facts = _open_facts(1)
    facts["round_anchors"][0]["previous_result_sha"] = _git_sha("other")
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_failed_verification_blocks_the_round() -> None:
    result = open_review_fix_round(_open_facts(1, status="failed"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "policy-engine"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert result.round_no is None


def test_verified_record_is_never_a_completed_cycle() -> None:
    facts = _open_facts(1)
    facts["round_records"][0]["verdict"] = "verified"
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_cycle_numbering_must_be_strict() -> None:
    facts = _open_facts(1)
    facts["round_records"][0]["cycle_no"] = 2
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# --- E. composed close outcome -----------------------------------------------


def test_clean_verified_closes_through_verbatim() -> None:
    result = close_review_fix_round(_close_facts(1))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.receipt.schema_version == "dal.transition-receipt/1.0"
    assert result.state_trace == ("reviewing", "verified")
    assert result.final_state == "verified"
    assert result.final_entity_type == "feature"
    assert result.declared_write_set == BASE_WRITE_SET
    assert result.event_trace == ("review.completed",)
    assert result.round_no == 2
    assert result.loop_violations == ()
    assert result.reasons == ()


def test_clean_changes_requested_returns_to_fixing() -> None:
    result = close_review_fix_round(
        _close_facts(1, decl="changes_requested", resolutions=[_resolution("F-001", "remaining")])
    )
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.state_trace == ("reviewing", "fixing")
    assert result.final_state == "fixing"
    assert result.declared_write_set == BASE_WRITE_SET
    assert result.event_trace == ("fix.requested",)
    assert result.round_no == 2
    assert result.loop_violations == ()


def test_changes_requested_at_count_two_is_still_legal() -> None:
    result = close_review_fix_round(
        _close_facts(2, decl="changes_requested", resolutions=[_resolution("F-001", "remaining")])
    )
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "fixing"
    assert result.event_trace == ("fix.requested",)
    assert result.round_no == 3


def test_post_fix_verdict_block_passes_through_verbatim() -> None:
    result = close_review_fix_round(_close_facts(1, acceptance=False))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.state_trace == ("reviewing", "needs_human")
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert result.event_trace == ("feature.blocked",)
    assert result.reasons == ("acceptance",)
    assert result.loop_violations == ()


def test_open_finding_set_block_passes_through_verbatim() -> None:
    result = close_review_fix_round(
        _close_facts(1, resolutions=[_resolution("F-999", "closed")])
    )
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert any("outside the open set" in reason for reason in result.reasons)


def test_verdict_must_be_bound_to_the_recorded_fix_result() -> None:
    with pytest.raises(DalError) as raised:
        close_review_fix_round(_close_facts(1, verdict_sha=_git_sha("unbound")))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT
    assert "not bound" in str(raised.value.internal_detail)


def test_verified_with_unresolved_carried_finding_blocks() -> None:
    git_result = _git_result()
    git_result["increment_diff"] = "@@ -5,2 +5,2 @@\n-old5\n-old7\n+new5\n+new7"
    git_result["anchor_translation_diff"] = "@@ -5,2 +5,2 @@\n-old5\n-old7\n+old5\n+old7"
    result = close_review_fix_round(
        _close_facts(
            1,
            finding_ids=["F-001", "F-002"],
            git_result=git_result,
            # The verdict closes only F-001; F-002 stays unresolved.
            resolutions=[_resolution("F-001", "closed")],
        )
    )
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert any("not fully resolved" in reason for reason in result.reasons)


# --- F. module hygiene -------------------------------------------------------


def test_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(loop_policy.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
        "personal_agent_dal.machine.open_finding_set",
        "personal_agent_dal.machine.post_fix_verdict",
        "personal_agent_dal.receipt",
    }
    forbidden_calls = {"open", "exec", "eval", "compile", "__import__"}
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called_names & forbidden_calls)


def test_no_operation_spec_id_and_no_dispatch_graph_import() -> None:
    """The loop gate is a stage-internal guard (the R09-A1 precedent): it
    declares no operation spec id and never imports the frozen dispatch
    graph; the budget constant is mirrored and test-enforced instead."""
    source_path = Path(loop_policy.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    assigned = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert "OPERATION_SPEC_ID" not in assigned
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "personal_agent_dal.machine.dispatch_graph" not in imports
    assert not hasattr(loop_policy, "OPERATION_SPEC_ID")


def test_mirrored_budget_matches_the_frozen_dispatch_graph_limit() -> None:
    """The module mirror and the frozen fixing gate must read the same
    budget: one counter source, two guard faces (Henson's 2026-08-29
    exhaustion-landing decision)."""
    from personal_agent_dal.machine.dispatch_graph import (
        REVIEW_LOOP_LIMIT as FROZEN_LIMIT,
    )

    assert loop_policy.REVIEW_LOOP_LIMIT == FROZEN_LIMIT == 3


def test_decisions_are_deterministic_and_echo_nothing() -> None:
    open_result = open_review_fix_round(_open_facts(1))
    assert _shape(open_result) == _shape(open_review_fix_round(_open_facts(1)))

    close_facts = _close_facts(1)
    assert _shape(close_review_fix_round(deepcopy(close_facts))) == _shape(
        close_review_fix_round(deepcopy(close_facts))
    )

    block = close_review_fix_round(
        _close_facts(1, resolutions=[_resolution("F-999", "closed")])
    )
    surface = " ".join(block.reasons + block.loop_violations)
    assert E_FIX not in surface
    assert "old5" not in surface
    assert "+new5" not in surface
