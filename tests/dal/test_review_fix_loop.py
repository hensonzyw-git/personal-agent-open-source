"""Adversarial tests for the bounded review/fix loop policy (DAL-030, R09-A2).

The loop module is a pure judge in the ``patch_policy`` family: no I/O, no
fixture composition, no manifest registration. The matrix follows the plan
groups — (A) trusted-envelope drift, (B) the round budget boundary, (C) the
cross-round independence matrix, (D) chain consistency, (E) the composed
close outcome, (F) module hygiene — restructured for the round-1 review's
call-timing split: ``open_review_fix_round`` is the call-before gate (budget,
verification precondition), ``admit_round_reviewer`` is the call-after gate
(independence matrix against the attested binding), ``close_review_fix_round``
composes the frozen evaluators.

Counting convention B and the POLICY_FAILURE exhaustion landing are Henson's
2026-08-29 decisions; the boundary pair (count=2 opens round 3, count=3
refuses round 4 through the frozen BLK-POLICY semantics) is asserted
explicitly — including that the refusal does not require any
reviewer-binding field, so it lands before a fourth provider call could be
spent (round-1 review B1). The close tests compose the frozen
``post_fix_verdict`` and ``open_finding_set`` evaluators for real: the golden
facts are shaped so both evaluators run their full validation, so a
pass-through assertion proves the loop did not swallow or rewrite their
outcomes.
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
    admit_round_reviewer,
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
E_FIX2 = _sha256_hex("evidence", "fix-diff-2")
E_RECEIPT = _sha256_hex("evidence", "test-receipt-1")

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


def _history(count: int) -> dict:
    """The history facts shared by all three gates (call-before legal)."""
    return {
        "review_fix_cycle_count": count,
        "round_records": _records(count),
        "round_anchors": _anchors(count),
        "original_review_result_sha": CAND,
        "original_coder_session_id": "sess-coder-original",
        "original_coder_context_sha256": _ctx("coder-original"),
        "original_coder_independence_key": _key("coder-original"),
    }


def _open_facts(
    count: int,
    *,
    status: str = "succeeded",
    target: dict | None = None,
) -> dict:
    """Golden open facts — call-before only: no reviewer-binding field exists
    at this stage (round-1 review B1). At count=0 the last verification is
    the candidate's own deterministic verification (coding -> verifying ->
    reviewing), so `succeeded` is the true round-1 entry fact."""
    return {
        "target": target if target is not None else _target(),
        **_history(count),
        "latest_verification_status": status,
    }


def _admit_facts(count: int, *, proposed: str = "reviewer-proposed") -> dict:
    """Golden admit facts — call-after: the controller has attested the
    reviewer's binding and the gate re-derives the budget and the
    verification precondition from its own facts (round-2 review B1)."""
    return {
        "target": _target(),
        **_history(count),
        "latest_verification_status": "succeeded",
        "proposed_reviewer_session_id": f"sess-{proposed}",
        "proposed_reviewer_context_sha256": _ctx(proposed),
        "proposed_reviewer_independence_key": _key(proposed),
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
    #: Distinct evidence digest per resolution — the verdict drift check
    #: rejects two resolutions citing the same first digest.
    return {
        "evidence_sha256": [
            E_FIX if finding_id == "F-001" else E_FIX2,
        ],
        "finding_id": finding_id,
        "status": status,
        "summary": f"{finding_id} {status}",
    }


def _gap_resolution(acceptance_id: str, status: str) -> dict:
    """A well-formed acceptance-gap resolution citing a test-receipt digest."""
    return {
        "acceptance_id": acceptance_id,
        "evidence_sha256": [E_RECEIPT],
        "status": status,
        "summary": f"{acceptance_id} {status}",
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


def _chains(count: int) -> tuple[list[dict], list[dict]]:
    """`(pfv_chain, openset_chain)` for a re-review closing at `count`.

    The prior verdict chain holds V_1 .. V_{count-1}, so at count=1 (the
    first re-review) it is empty and the open set is the original review's
    findings (frozen rule: "round 1, empty chain"). In the golden history
    the original finding stays `remaining` through every completed cycle
    (changes_requested → fix → verify), so each chain entry resolves it
    remaining and introduces no regressions — carried findings travel
    through resolutions by their original id, and chain `new_findings` are
    new regressions with disjoint ids (round-2 review S1: the golden
    fixtures must follow the contract's carry-forward model, not reuse an
    original id as a chain new-finding id).
    """
    entries = max(count - 1, 0)
    results = [FIX1, FIX2, FIX3][:entries]
    #: Both sub-evaluators' chain entries now carry ``finding_resolutions``
    #: (refreeze §2c D1): the open-set derivation removes V_j's closed
    #: findings per round. In the golden history the original finding stays
    #: ``remaining`` through every completed cycle, so each entry resolves it
    #: remaining.
    chain = [
        {
            "finding_resolutions": [_resolution("F-001", "remaining")],
            "new_findings": [],
            "result_sha": results[index],
            "sequence": index + 1,
        }
        for index in range(entries)
    ]
    return chain, [dict(entry) for entry in chain]


def _pfv_facts(
    count: int, anchors: dict, chain: list[dict], finding_ids: list[str]
) -> dict:
    return {
        "manifest_roles": {
            E_FIX: "fix_diff",
            E_FIX2: "fix_diff",
            E_RECEIPT: "test_receipts",
        },
        "test_receipts": {E_RECEIPT: {"verification_id": "VR-1"}},
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


def _openset_facts(
    count: int,
    anchors: dict,
    chain: list[dict],
    recomputed: str,
    finding_ids: list[str],
) -> dict:
    return {
        "manifest_roles": {
            E_FIX: "fix_diff",
            E_FIX2: "fix_diff",
            E_RECEIPT: "test_receipts",
        },
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
    verdict_sha = verdict_sha if verdict_sha is not None else [FIX1, FIX2, FIX3, FIX4][
        count - 1
    ]
    current = CAND if count == 1 else FIX1
    expected = {"anchor_sha": current, "previous_result_sha": current}
    new_findings = new_findings if new_findings is not None else []
    pfv_chain, openset_chain = _chains(count)
    return {
        "target": _target(),
        **_history(count),
        "post_fix_verdict_facts": _pfv_facts(
            count,
            pfv_anchors if pfv_anchors is not None else expected,
            pfv_chain,
            finding_ids,
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
                new_findings=new_findings,
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
    del dropped["latest_verification_status"]
    with pytest.raises(DalError) as raised:
        open_review_fix_round(dropped)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_open_facts_carry_no_reviewer_binding_field() -> None:
    """B1: the call-before facts must not require any reviewer identity —
    the exhaustion refusal lands without one, so no fourth call is spent."""
    facts = _open_facts(3)
    assert not any(
        field.startswith("proposed_reviewer") for field in facts
    )
    result = open_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"


def test_admit_facts_shape_is_closed() -> None:
    facts = _admit_facts(1)
    with pytest.raises(DalError) as raised:
        admit_round_reviewer({**deepcopy(facts), "junk": True})
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT
    dropped = deepcopy(facts)
    del dropped["proposed_reviewer_independence_key"]
    with pytest.raises(DalError) as raised:
        admit_round_reviewer(dropped)
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
        ("original_coder_session_id", ""),
        ("original_coder_context_sha256", _ctx("coder-original")[:63]),
        ("original_coder_independence_key", "zzz-not-hex"),
        ("original_coder_independence_key", _key("coder-original")[:40]),
    ],
)
def test_open_rejects_malformed_identity_fields(field: str, value: str) -> None:
    facts = _open_facts(0)
    facts[field] = value
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "field",
    [
        "proposed_reviewer_session_id",
        "proposed_reviewer_context_sha256",
        "proposed_reviewer_independence_key",
    ],
)
def test_admit_rejects_malformed_reviewer_binding(field: str) -> None:
    facts = _admit_facts(0)
    facts[field] = "" if field.endswith("session_id") else "not-hex"
    with pytest.raises(DalError) as raised:
        admit_round_reviewer(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "bool_cycle_no",
        "wrong_cycle_no",
        "blocked_verification",
        "bad_result_sha",
        "short_record_key",
        "record_verdict_verified",
        "record_verification_failed",
    ],
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
    elif mutation == "short_record_key":
        del record["verdict"]
    elif mutation == "record_verdict_verified":
        record["verdict"] = "verified"
    elif mutation == "record_verification_failed":
        record["verification_status"] = "failed"
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "count, status",
    [(True, "succeeded"), (-1, "succeeded"), (0, "unknown"), (0, "blocked")],
)
def test_open_rejects_bad_count_and_status(count: int, status: str) -> None:
    facts = _open_facts(0, status=status)
    facts["review_fix_cycle_count"] = count
    with pytest.raises(DalError) as raised:
        open_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# --- B. round budget boundary ------------------------------------------------


def test_count_zero_opens_the_original_review_round() -> None:
    result = open_review_fix_round(_open_facts(0))
    assert _shape(result) == _shape(open_review_fix_round(_open_facts(0)))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "reviewing"
    assert result.state_trace == ("reviewing",)
    assert result.declared_write_set == ()
    assert result.round_no == 1
    assert result.loop_violations == ()
    assert result.final_reason_code is None


def test_count_two_opens_round_three() -> None:
    result = open_review_fix_round(_open_facts(2))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "reviewing"
    assert result.declared_write_set == ()
    assert result.round_no == 3
    assert result.loop_violations == ()


def test_count_three_refuses_round_four_with_policy_failure() -> None:
    result = open_review_fix_round(_open_facts(3))
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
    result = open_review_fix_round(_open_facts(4))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "policy-engine"
    assert result.declared_write_set == BLOCK_WRITE_SET


def test_exhaustion_precedes_verification_drift() -> None:
    """B3: the budget is judged before the verification precondition — even
    a drifted status value must not reorder the landing. The count=3 block
    fires regardless of what the status field says."""
    result = open_review_fix_round(_open_facts(3, status="failed"))
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.declared_write_set == BLOCK_WRITE_SET


def test_budget_block_carries_history_violation_labels() -> None:
    """S4: the declared order is the real order — accounting, budget,
    verification, history. A budget refusal on a forged history still lands
    (the count is readable before the history is certified) and carries the
    history labels with it instead of raising."""
    facts = _open_facts(3)
    facts["round_records"][1]["reviewer_session_id"] = "sess-coder-original"
    result = open_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert any("round_records[1] reviewer" in label for label in result.loop_violations)


def test_verification_failed_block_on_forged_history_still_lands() -> None:
    facts = _open_facts(1, status="failed")
    facts["round_records"][0]["coder_session_id"] = facts["round_records"][0][
        "reviewer_session_id"
    ]
    result = open_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert any("round_records[0] coder" in label for label in result.loop_violations)


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


# --- C. cross-round independence matrix (admit gate) -------------------------


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
    """The attested reviewer must differ on every binding field from every
    historical reviewer and coder, including the original candidate's coder."""
    facts = _admit_facts(1)
    source = {
        "session": "session_id",
        "context": "context_sha256",
        "independence key": "independence_key",
    }[field]
    if historical == "original coder":
        facts[f"proposed_reviewer_{source}"] = facts[f"original_coder_{source}"]
    else:
        record = facts["round_records"][0]
        prefix = "reviewer" if historical.endswith("reviewer") else "coder"
        facts[f"proposed_reviewer_{source}"] = record[f"{prefix}_{source}"]
    result = admit_round_reviewer(facts)
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
    facts = _admit_facts(1)
    record = facts["round_records"][0]
    facts["proposed_reviewer_session_id"] = record["reviewer_session_id"]
    facts["proposed_reviewer_context_sha256"] = record["reviewer_context_sha256"]
    facts["proposed_reviewer_independence_key"] = record["reviewer_independence_key"]
    result = admit_round_reviewer(facts)
    assert result.receipt.code is ReceiptCode.POLICY_DENIED
    assert result.loop_violations == (
        "proposed reviewer reuses the cycle 1 reviewer session",
        "proposed reviewer reuses the cycle 1 reviewer context",
        "proposed reviewer reuses the cycle 1 reviewer independence key",
    )


def test_admit_at_count_zero_checks_only_the_original_coder() -> None:
    facts = _admit_facts(0)
    facts["proposed_reviewer_session_id"] = facts["original_coder_session_id"]
    result = admit_round_reviewer(facts)
    assert result.receipt.code is ReceiptCode.POLICY_DENIED
    assert result.loop_violations == (
        "proposed reviewer reuses the original coder session",
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


def test_coder_may_repeat_across_cycles() -> None:
    """S1: the frozen contract imposes novelty on reviewers only. A coder
    producing successive fixes is a legal history, not forged drift."""
    facts = _open_facts(2)
    second = facts["round_records"][1]
    first = facts["round_records"][0]
    for field in ("session_id", "context_sha256", "independence_key"):
        second[f"coder_{field}"] = first[f"coder_{field}"]
    result = open_review_fix_round(facts)
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.round_no == 3
    assert result.loop_violations == ()
    # The same history admits a fresh reviewer on the matrix too.
    admit = _admit_facts(2, proposed="reviewer-r3")
    admit_second = admit["round_records"][1]
    admit_first = admit["round_records"][0]
    for field in ("session_id", "context_sha256", "independence_key"):
        admit_second[f"coder_{field}"] = admit_first[f"coder_{field}"]
    assert admit_round_reviewer(admit).receipt.code is ReceiptCode.APPLIED


def test_fully_distinct_matrix_at_count_two_passes() -> None:
    result = admit_round_reviewer(_admit_facts(2, proposed="reviewer-r3"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.round_no == 3
    assert result.loop_violations == ()


def test_admit_at_exhausted_count_lands_the_policy_block() -> None:
    """B1: the admission gate re-derives the budget from its own facts — a
    facts swap or replay between open and admit cannot admit a round-4
    reviewer behind a refused opener."""
    result = admit_round_reviewer(_admit_facts(3, proposed="reviewer-r4"))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.state_trace == ("reviewing", "needs_human")
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "policy-engine"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert result.event_trace == ("feature.blocked",)
    assert result.round_no is None
    assert any("exhausted" in label for label in result.loop_violations)


def test_admit_with_failed_verification_lands_the_policy_block() -> None:
    result = admit_round_reviewer(_admit_facts(1, proposed="reviewer-r2"))
    result = admit_round_reviewer({**_admit_facts(1), "latest_verification_status": "failed"})
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.declared_write_set == BLOCK_WRITE_SET


def test_admit_with_blocked_verification_raises() -> None:
    facts = {**_admit_facts(1), "latest_verification_status": "blocked"}
    with pytest.raises(DalError) as raised:
        admit_round_reviewer(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


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
        _close_facts(
            1, decl="changes_requested", resolutions=[_resolution("F-001", "remaining")]
        )
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
        _close_facts(
            2, decl="changes_requested", resolutions=[_resolution("F-001", "remaining")]
        )
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
    git_result["anchor_translation_diff"] = (
        "@@ -5,2 +5,2 @@\n-old5\n-old7\n+old5\n+old7"
    )
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
    assert any(
        "omits open findings" in reason and "F-002" in reason
        for reason in result.reasons
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "resolution_not_object",
        "resolution_extra_field",
        "resolution_empty_evidence",
        "resolution_non_hex_evidence",
        "resolution_list_finding_id",
        "resolution_dict_finding_id",
        "resolutions_not_a_list",
        "gap_resolutions_not_a_list",
        "gap_resolution_empty_evidence",
        "gap_resolution_list_acceptance_id",
        "new_finding_not_object",
        "new_finding_extra_field",
        "new_finding_list_finding_id",
        "new_findings_not_a_list",
        "new_finding_location_not_object",
    ],
)
def test_crash_shaped_verdict_members_fail_closed(mutation: str) -> None:
    """B5: the frozen evaluators dereference verdict members directly, so
    crash-shaped output must land the frozen contract block here, never
    raise out of a policy boundary. Round-4 F1 extends the class with
    non-hashable ``finding_id`` values — both layers' set builds would leak
    ``TypeError: unhashable type`` without the guards."""
    resolutions = [_resolution("F-001", "closed")]
    gap_resolutions: list[dict] = []
    new_findings: list[dict] = []
    if mutation == "resolution_not_object":
        resolutions = [None]  # type: ignore[list-item]
    elif mutation == "resolution_extra_field":
        resolutions[0]["junk"] = 1
    elif mutation == "resolution_empty_evidence":
        resolutions[0]["evidence_sha256"] = []
    elif mutation == "resolution_non_hex_evidence":
        resolutions[0]["evidence_sha256"] = ["not-a-digest"]
    elif mutation == "resolution_list_finding_id":
        resolutions[0]["finding_id"] = ["unhashable-id"]
    elif mutation == "resolution_dict_finding_id":
        resolutions[0]["finding_id"] = {"k": "v"}
    elif mutation == "resolutions_not_a_list":
        resolutions = "abc"  # type: ignore[assignment]
    elif mutation == "gap_resolutions_not_a_list":
        gap_resolutions = "abc"  # type: ignore[assignment]
    elif mutation == "gap_resolution_empty_evidence":
        gap_resolutions = [_gap_resolution("AC-1", "closed")]
        gap_resolutions[0]["evidence_sha256"] = []
    elif mutation == "gap_resolution_list_acceptance_id":
        gap_resolutions = [_gap_resolution(["AC-1"], "closed")]  # type: ignore[arg-type]
    elif mutation == "gap_resolution_not_object":
        gap_resolutions = [None]  # type: ignore[list-item]
    elif mutation == "new_finding_not_object":
        new_findings = [None]  # type: ignore[list-item]
    elif mutation == "new_finding_extra_field":
        new_findings = [_finding("F-100", FIX1, 5)]
        new_findings[0]["junk"] = 1
    elif mutation == "new_finding_list_finding_id":
        new_findings = [_finding("F-100", FIX1, 5)]
        new_findings[0]["finding_id"] = ["unhashable-id"]
    elif mutation == "new_findings_not_a_list":
        new_findings = 123  # type: ignore[assignment]
    elif mutation == "new_finding_location_not_object":
        new_findings = [_finding("F-100", FIX1, 5)]
        new_findings[0]["location"] = "src/feature.py"

    facts = _close_facts(1, resolutions=resolutions)
    verdict = facts["reviewer_result"]["verdict"]
    verdict["acceptance_gap_resolutions"] = gap_resolutions
    verdict["new_findings"] = new_findings
    result = close_review_fix_round(facts)
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.state_trace == ("reviewing", "needs_human")
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert result.event_trace == ("feature.blocked",)
    assert len(result.reasons) == 1


def test_close_revalidates_history_drift() -> None:
    """B6: the closer is a separate judge call — it must not trust that an
    opener already validated the same facts. A cycle whose coder equals its
    own reviewer fails closed here too."""
    facts = _close_facts(1)
    record = facts["round_records"][0]
    for field in ("session_id", "context_sha256", "independence_key"):
        record[f"coder_{field}"] = record[f"reviewer_{field}"]
    with pytest.raises(DalError) as raised:
        close_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_close_rejects_malformed_original_coder_identity() -> None:
    facts = _close_facts(1)
    facts["original_coder_independence_key"] = "zzz-not-hex"
    with pytest.raises(DalError) as raised:
        close_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "mutation",
    [
        "chain_too_long",
        "chain_emptied",
        "chain_sequence_drift",
        "chain_result_sha_drift",
        "chain_entry_extra_field",
    ],
)
def test_close_binds_sub_fact_chains_to_recorded_history(mutation: str) -> None:
    """B2: the chain is controller-derived state, so a drift between either
    sub-fact's ``prior_verdict_chain`` and ``round_records`` raises
    INVALID_ARGUMENT — no forged chain can reach the evaluators."""
    facts = _close_facts(2)
    for sub_facts_name in ("post_fix_verdict_facts", "open_finding_set_facts"):
        chain = facts[sub_facts_name]["prior_verdict_chain"]
        if mutation == "chain_too_long":
            chain.append(dict(chain[0], sequence=2))
        elif mutation == "chain_emptied":
            facts[sub_facts_name]["prior_verdict_chain"] = []
        elif mutation == "chain_sequence_drift":
            chain[0]["sequence"] = 3
        elif mutation == "chain_result_sha_drift":
            chain[0]["result_sha"] = _git_sha("unbound-tree")
        elif mutation == "chain_entry_extra_field":
            chain[0]["junk"] = 1
    with pytest.raises(DalError) as raised:
        close_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# --- F. module hygiene -------------------------------------------------------


def test_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency — via
    from-imports, plain imports, dynamic imports (``__import__``,
    ``getattr(__builtins__, "__import__")``, subscripted builtins) or
    attribute calls into imported modules (round-1 review S2, round-2
    review S2)."""
    source_path = Path(loop_policy.__file__)
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    from_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    plain_imports = {
        name.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    }
    assert from_imports == {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
        "personal_agent_dal.machine.open_finding_set",
        "personal_agent_dal.machine.post_fix_verdict",
        "personal_agent_dal.receipt",
    }
    assert plain_imports == set()

    #: Every called function must be either a plain name from the closed
    #: allowlist below or an attribute whose receiver closes over locals and
    #: the from-imports (which expose no I/O surface). Anything else —
    #: ``getattr(__builtins__, ...)`` and friends — fails.
    allowed_calls = {
        "frozenset",
        "tuple",
        "list",
        "set",
        "dict",
        "isinstance",
        "len",
        "bool",
        "str",
        "zip",
        "sorted",
        "any",
        "all",
        "enumerate",
        "deepcopy",
        "dataclass",
        "OperationReceipt",
        "ReviewFixLoopEvaluation",
        "DalError",
        "DalErrorCode",
        "_invalid",
        "_policy_block",
        "_contract_block",
        "_pass_through",
        "_budget_and_verification",
        "_assert_untainted_history",
        "_validate_target",
        "_validate_identity_fields",
        "_validate_accounting",
        "_validate_record",
        "_validate_anchors",
        "_history_reuse",
        "_history_identities",
        "_identity_violations",
        "_original_coder",
        "_proposed_reuse",
        "_current_anchor",
        "_bind_chain_to_history",
        "_chain_member_drift",
        "_carry_forward_violations",
        "_verified_semantics",
        "_evidence_and_gap_violations",
        "_new_finding_anchor_violations",
        "_verdict_member_drift",
        "_resolution_drift",
        "_finding_drift",
        "_id_field",
        "_is_sha256_hex",
        "_is_git_sha_hex",
        "_is_non_empty_str",
        "_is_non_negative_int",
        "_sub_command",
        "validate_post_fix_verdict",
        "derive_open_finding_set",
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names <= allowed_calls

    local_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    } | {
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for arg in node.args.args + node.args.kwonlyargs
        if isinstance(arg, ast.arg)
    }
    from_symbol_names = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    module_constants = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    #: A call's receiver tree must resolve entirely to names in the closed
    #: sets: a bare name (locals, from-imports, module constants, the builtin
    #: types whose methods expose no I/O), an attribute chain rooted there
    #: (``dict.fromkeys``), or a string literal (``", ".join``). Receivers
    #: like ``__builtins__`` or ``sys`` cannot appear.
    allowed_builtin_receivers = {"dict", "str", "list", "set", "tuple"}

    def _receiver_ok(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return (
                node.id in local_names | from_symbol_names | module_constants
                or node.id in allowed_builtin_receivers
            )
        if isinstance(node, ast.Attribute):
            return _receiver_ok(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        return False

    bad_receivers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and not (
            (isinstance(node.func, ast.Name) and node.func.id in allowed_calls)
            or (isinstance(node.func, ast.Attribute) and _receiver_ok(node.func.value))
        )
    ]
    assert bad_receivers == []


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

    admit_result = admit_round_reviewer(_admit_facts(1))
    assert _shape(admit_result) == _shape(admit_round_reviewer(_admit_facts(1)))

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


# --- G. round-2 review remediation: verdict content and §6 cross-checks ------


def _attack(  # type: ignore[no-untyped-def]
    count: int = 1,
    **verdict_overrides,
):
    """A golden close whose verdict members carry the given overrides."""
    facts = _close_facts(count)
    for field, value in verdict_overrides.items():
        facts["reviewer_result"]["verdict"][field] = value
    return facts


def _attack_resolutions(*resolutions: dict) -> dict:
    return _attack(finding_resolutions=list(resolutions))


def test_verified_with_remaining_resolution_blocks() -> None:
    """B4: ``verified`` with a ``remaining`` resolution violates the §6
    if/then cross-constraint the frozen evaluators do not judge."""
    result = close_review_fix_round(
        _attack_resolutions(_resolution("F-001", "remaining"))
    )
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert any("non-closed resolutions" in reason for reason in result.reasons)


def test_verified_with_remaining_gap_resolution_blocks() -> None:
    """B4: a ``verified`` verdict whose gap resolution stays ``remaining``."""
    facts = _attack()
    facts["reviewer_result"]["verdict"]["acceptance_gap_resolutions"] = [
        {
            "acceptance_id": "A-001",
            "evidence_sha256": [E_FIX],
            "status": "remaining",
            "summary": "gap remains",
        }
    ]
    # The pfv original review must carry the same gap id for the bijection.
    facts["post_fix_verdict_facts"]["original_review"]["acceptance_gap_ids"] = ["A-001"]
    facts["open_finding_set_facts"]["original_review"]["acceptance_gap_ids"] = ["A-001"]
    result = close_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert any("non-closed gap resolution" in reason for reason in result.reasons)


def test_duplicate_resolution_id_blocks() -> None:
    result = close_review_fix_round(
        _attack_resolutions(
            _resolution("F-001", "closed"), _resolution("F-001", "closed")
        )
    )
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert any(
        ("resolves an id more than once" in reason) or ("repeats an id" in reason)
        for reason in result.reasons
    )


def test_repeated_evidence_digest_blocks() -> None:
    """B4: two resolutions citing the same digest (§6: no repeats)."""
    shared = _resolution("F-001", "closed")["evidence_sha256"][0]
    result = close_review_fix_round(
        _attack_resolutions(
            {**_resolution("F-001", "closed"), "evidence_sha256": [shared]},
            {**_resolution("F-001", "closed"), "finding_id": "F-002",
             "evidence_sha256": [shared]},
        )
    )
    assert result.final_state == "needs_human"
    assert any("repeats an evidence digest" in reason for reason in result.reasons)


def test_resolution_citing_unknown_role_blocks() -> None:
    digest = _sha256_hex("evidence", "not-in-manifest")
    resolution = {**_resolution("F-001", "closed"), "evidence_sha256": [digest]}
    result = close_review_fix_round(_attack_resolutions(resolution))
    assert result.final_state == "needs_human"
    assert any("unknown or disallowed role" in reason for reason in result.reasons)


def test_gap_resolutions_must_bijection_the_original_gaps() -> None:
    """B4: a gap resolution attached when the original review listed no gaps."""
    facts = _attack()
    facts["reviewer_result"]["verdict"]["acceptance_gap_resolutions"] = [
        {
            "acceptance_id": "A-999",
            "evidence_sha256": [E_FIX],
            "status": "closed",
            "summary": "invented gap",
        }
    ]
    result = close_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert any("bijection" in reason for reason in result.reasons)


def test_closed_gap_citing_non_test_receipt_role_blocks() -> None:
    """B4: a closed gap resolution must cite ``test_receipts`` evidence."""
    facts = _attack()
    facts["post_fix_verdict_facts"]["original_review"]["acceptance_gap_ids"] = ["A-001"]
    facts["open_finding_set_facts"]["original_review"]["acceptance_gap_ids"] = ["A-001"]
    facts["reviewer_result"]["verdict"]["acceptance_gap_resolutions"] = [
        {
            "acceptance_id": "A-001",
            "evidence_sha256": [E_FIX],
            "status": "closed",
            "summary": "closed with fix-diff evidence",
        }
    ]
    result = close_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert any("non-test-receipt evidence" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "mutation",
    [
        "location_none",
        "location_extra_field",
        "location_bad_anchor",
        "location_bad_path",
        "location_bool_line",
        "finding_empty_summary",
    ],
)
def test_new_finding_content_drift_blocks(mutation: str) -> None:
    """B6/B4: a malformed ``location`` or finding member lands the frozen
    contract block — never a native TypeError from the evaluators."""
    finding = _finding("F-100", _git_sha("r1"), 5)
    if mutation == "location_none":
        finding["location"] = None  # type: ignore[assignment]
    elif mutation == "location_extra_field":
        finding["location"]["junk"] = 1
    elif mutation == "location_bad_anchor":
        finding["location"]["anchor_sha"] = "CAND"
    elif mutation == "location_bad_path":
        finding["location"]["path"] = ""
    elif mutation == "location_bool_line":
        finding["location"]["line_start"] = True
    elif mutation == "finding_empty_summary":
        finding["summary"] = ""
    result = close_review_fix_round(_attack(new_findings=[finding]))
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert len(result.reasons) == 1


def test_new_finding_anchored_to_another_tree_blocks() -> None:
    """An anchor that is neither the result tree nor any other legal anchor
    is a provider contract failure at both layers (the loop's check and, since
    the 2026-08-29 refreeze, the frozen evaluator's check judge the same
    direction)."""
    finding = _finding("F-100", _git_sha("some-other-tree"), 5)
    result = close_review_fix_round(
        _attack(
            decl="changes_requested",
            resolutions=[_resolution("F-001", "remaining")],
            new_findings=[finding],
        )
    )
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert any("not the verdict's result tree" in reason for reason in result.reasons)


def test_omitted_original_remaining_blocks_the_carry_forward() -> None:
    """B3 at the loop level: the §6 per-round carry-forward invariant. The
    original finding stays open; a verdict that resolves only a chain new
    finding while omitting it cannot be ``verified`` — the original cannot
    silently vanish behind the chain."""
    facts = _close_facts(2, resolutions=[])
    # A verified verdict that omits F-001 entirely must block: the open set
    # after V_1 still holds F-001 (remaining), and the verdict must resolve
    # it — it cannot silently vanish behind the chain.
    result = close_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert any(
        "omits open findings" in reason and "F-001" in reason
        for reason in result.reasons
    )


def test_chain_resolving_an_unknown_finding_blocks() -> None:
    """A chain entry resolving an id outside its round's open set is caught
    by the loop-level per-round derivation."""
    facts = _close_facts(2)
    facts["post_fix_verdict_facts"]["prior_verdict_chain"][0][
        "finding_resolutions"
    ] = [_resolution("F-042", "closed")]
    result = close_review_fix_round(facts)
    # The chain member is shape-valid controller state, so the per-round
    # derivation catches the unknown id as a contract violation — fail
    # closed either way.
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert any("outside the open set" in reason for reason in result.reasons)


def test_malformed_openset_chain_members_raise_before_dispatch() -> None:
    """Round-3 review B1: the openset sub-fact's chain members are trusted
    controller state, so a malformed member is controller drift and must
    raise ``INVALID_ARGUMENT`` — not pass through the pre-flight untouched
    and crash (or silently skip the member) inside the frozen evaluator."""
    cases: list[tuple[str, dict]] = []

    facts = _close_facts(2)
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "finding_resolutions"
    ].append({"finding_id": "F-001", "status": "closed"})
    cases.append(("missing fields", facts))

    facts = _close_facts(2)
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "finding_resolutions"
    ][0]["extra"] = "x"
    cases.append(("unknown field", facts))

    facts = _close_facts(2)
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "finding_resolutions"
    ].append(None)
    cases.append(("None member", facts))

    facts = _close_facts(2)
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "finding_resolutions"
    ] = "closed"
    cases.append(("not a list", facts))

    facts = _close_facts(2)
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "new_findings"
    ].append("F-100")
    cases.append(("new finding not an object", facts))

    # Round-4 review F1: non-hashable finding_id values would leak
    # ``TypeError: unhashable type`` from the derivation's set builds.
    facts = _close_facts(2)
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "finding_resolutions"
    ][0]["finding_id"] = ["unhashable-id"]
    cases.append(("chain resolution list finding_id", facts))

    facts = _close_facts(2)
    facts["post_fix_verdict_facts"]["prior_verdict_chain"][0][
        "new_findings"
    ] = [_finding("F-100", FIX1, 5)]
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "new_findings"
    ] = [_finding("F-100", FIX1, 5)]
    for key in ("post_fix_verdict_facts", "open_finding_set_facts"):
        facts[key]["prior_verdict_chain"][0]["new_findings"][0][
            "finding_id"
        ] = {"k": "v"}
    cases.append(("chain new finding dict finding_id", facts))

    for label, drifted in cases:
        with pytest.raises(DalError) as raised:
            close_review_fix_round(drifted)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_still_open_carried_finding_needs_no_new_deletion() -> None:
    """Round-3 review B2 (renamed by round-4 review F5): the
    increment-deletion check binds only findings THIS round declares
    ``closed`` (§6 L645–651). A carried finding the prior verdict
    introduced and left open, resolved ``remaining`` here, survives its
    round's diff untouched and the round still reaches ``fixing`` legally —
    the pre-fix check would have demanded its deletion because it iterated
    every chain finding. The frozen variant id
    ``prior_closed_carried_not_touched`` keeps its round-3 name (renaming
    would refreeze the manifest); its fixture comment now states the real
    shape."""
    facts = _close_facts(
        2,
        decl="changes_requested",
        resolutions=[
            _resolution("F-001", "remaining"),
            _resolution("F-100", "remaining"),
        ],
    )
    # V_1 introduced F-100 as a regression (anchor FIX1 = V_1's result_sha),
    # so both sub-evaluators' chains carry it and it joins the open set.
    facts["post_fix_verdict_facts"]["prior_verdict_chain"][0][
        "new_findings"
    ] = [_finding("F-100", FIX1, 5)]
    facts["open_finding_set_facts"]["prior_verdict_chain"][0][
        "new_findings"
    ] = [_finding("F-100", FIX1, 5)]
    # The golden increment deletes old5 (F-001's line, left remaining here)
    # and nothing else. F-100's own line is 5 in its anchor tree too, so
    # rewrite its location to line 6: old6 is never deleted, and the round
    # must still be legal.
    facts["git_result"]["increment_diff"] = "@@ -5,1 +5,1 @@\n-old5\n+new5"
    for sub_facts in (
        facts["post_fix_verdict_facts"],
        facts["open_finding_set_facts"],
    ):
        sub_facts["prior_verdict_chain"][0]["new_findings"][0]["location"][
            "line_start"
        ] = 6
        sub_facts["prior_verdict_chain"][0]["new_findings"][0]["location"][
            "line_end"
        ] = 6
    result = close_review_fix_round(facts)
    assert result.final_state == "fixing"
    assert result.event_trace == ("fix.requested",)
    assert result.round_no == 3


def test_changes_requested_with_remaining_closes_to_fixing() -> None:
    """The frozen legal fixing path: a remaining finding carries the round
    back to ``fixing`` through both composed evaluators."""
    facts = _close_facts(
        2,
        decl="changes_requested",
        resolutions=[_resolution("F-001", "remaining")],
    )
    result = close_review_fix_round(facts)
    assert result.final_state == "fixing"
    assert result.event_trace == ("fix.requested",)
    assert result.round_no == 3
    assert result.loop_violations == ()


def test_changes_requested_with_contract_anchored_new_finding_closes_to_fixing() -> None:
    """D2/D3 post-refreeze legal path (previously blocked until refreeze):
    §6 anchors a new finding to the verdict's ``result_sha``, and a non-empty
    ``new_findings`` alone forces ``changes_requested`` — with every original
    finding closed and no remaining resolution, the round now reaches
    ``fixing`` at both layers."""
    facts = _close_facts(
        2,
        decl="changes_requested",
        resolutions=[_resolution("F-001", "closed")],
        new_findings=[_finding("F-100", FIX2, 7)],
    )
    result = close_review_fix_round(facts)
    assert result.final_state == "fixing"
    assert result.event_trace == ("fix.requested",)
    assert result.round_no == 3
    assert result.reasons == ()


def test_legal_verified_after_full_resolution_closes() -> None:
    """A verified verdict that closes every open finding (original F-001
    resolved closed, no new findings) closes the feature."""
    result = close_review_fix_round(
        _close_facts(2, resolutions=[_resolution("F-001", "closed")])
    )
    assert result.final_state == "verified"
    assert result.event_trace == ("review.completed",)
    assert result.round_no == 3
    assert result.reasons == ()


def test_sub_facts_disagreeing_on_original_ids_raise() -> None:
    """Controller drift between the two sub-fact objects' original review id
    sets would let each half validate against a different baseline."""
    facts = _close_facts(1)
    facts["open_finding_set_facts"]["original_review"]["finding_ids"] = ["F-001", "F-002"]
    with pytest.raises(DalError) as raised:
        close_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "mutation",
    [
        "original_review_none",
        "original_review_not_object",
        "finding_ids_missing",
        "finding_ids_not_strings",
        "finding_ids_not_list",
        "gap_ids_missing",
        "gap_ids_none",
        "gap_ids_not_strings",
        "gap_ids_not_list",
        "gap_ids_int_hashable",
        "manifest_roles_missing",
        "manifest_roles_not_object",
        "chain_none",
        "chain_not_list",
    ],
)
def test_drifted_original_review_fails_closed(mutation: str) -> None:
    """Round-4 F6, extended by round-5 F-1a: the carry-forward derivation,
    the gap bijection and the evidence-role checks dereference the
    sub-facts' ``original_review``, ``manifest_roles`` and
    ``prior_verdict_chain`` directly, so a drifted shape must raise
    ``INVALID_ARGUMENT`` before any dereference — not leak
    TypeError/KeyError out of a policy boundary. The hashable-but-wrong
    ``[123]`` shape matters too: unguarded, it would flow into the gap
    bijection and be mis-judged as a provider contract failure instead of
    controller drift."""
    facts = _close_facts(2)
    sub = facts["open_finding_set_facts"]
    if mutation == "original_review_none":
        sub["original_review"] = None
    elif mutation == "original_review_not_object":
        sub["original_review"] = "original"
    elif mutation == "finding_ids_missing":
        del sub["original_review"]["finding_ids"]
    elif mutation == "finding_ids_not_strings":
        sub["original_review"]["finding_ids"] = [["F-001"]]
    elif mutation == "finding_ids_not_list":
        sub["original_review"]["finding_ids"] = "F-001"
    elif mutation == "gap_ids_missing":
        del sub["original_review"]["acceptance_gap_ids"]
    elif mutation == "gap_ids_none":
        sub["original_review"]["acceptance_gap_ids"] = None
    elif mutation == "gap_ids_not_strings":
        sub["original_review"]["acceptance_gap_ids"] = [["AC-1"]]
    elif mutation == "gap_ids_not_list":
        sub["original_review"]["acceptance_gap_ids"] = "AC-1"
    elif mutation == "gap_ids_int_hashable":
        sub["original_review"]["acceptance_gap_ids"] = [123]
    elif mutation == "manifest_roles_missing":
        del sub["manifest_roles"]
    elif mutation == "manifest_roles_not_object":
        sub["manifest_roles"] = ["fix_diff"]
    elif mutation == "chain_none":
        sub["prior_verdict_chain"] = None
    elif mutation == "chain_not_list":
        sub["prior_verdict_chain"] = {"sequence": 1}
    with pytest.raises(DalError) as raised:
        close_review_fix_round(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, mutation


def test_chain_new_finding_reusing_a_seen_id_blocks() -> None:
    """§6: chain new findings must be disjoint from everything already seen;
    a chain regression reusing the original id cannot ride the loop."""
    facts = _close_facts(2)
    findings = [_finding("F-001", FIX1, 5)]
    facts["post_fix_verdict_facts"]["prior_verdict_chain"][0]["new_findings"] = findings
    result = close_review_fix_round(facts)
    assert result.final_state == "needs_human"
    assert any("reuses an already-seen id" in reason for reason in result.reasons)
