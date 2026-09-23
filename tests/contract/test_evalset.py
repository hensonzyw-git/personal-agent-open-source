"""DEV-004: the rebuilt baseline conforms to the frozen contract.

The spike baseline was written before the contract froze. These tests pin the
three places where its expectations are now actively wrong, so they cannot be
copied back in: a default personal scope, a clarification for foreign currency,
and two separate writes for a two-entry message.
"""

from __future__ import annotations

import json

import pytest

from personal_agent_core.errors import ErrorCode
from personal_agent_core.finance_tools import FINANCE_QUERY_TOOL
from personal_agent_core.evalset import (
    DEFAULT_DATASET,
    OBSOLETE_TOOL_NAMES,
    REVIEWED_CASE_DIGESTS,
    EvalCase,
    EvalOutcome,
    ExpectedBehavior,
    case_distribution,
    lint_cases,
    load_cases,
    score_outcomes,
    semantic_case_digest,
    verify_provenance,
)
from personal_agent_core.manifest import load_manifest
from personal_agent_core.tool_ir import ALLOWED_EXPENSE_CATEGORIES


CASES = load_cases()


def cases_tagged(tag: str) -> list[EvalCase]:
    return [case for case in CASES if tag in case.tags]


# --- the linter itself must be able to fail ---------------------------------


def test_the_frozen_baseline_lints_clean() -> None:
    assert lint_cases(CASES) == []


def test_lint_rejects_a_disabled_tool() -> None:
    bad = EvalCase(
        id="BAD-001",
        source_type="synthetic",
        reference_time="2026-07-23T15:00:00+08:00",
        input="午饭45，咖啡18，个人支出",
        expected=ExpectedBehavior(
            action="call_tool",
            tool="finance.log_expense_batch",
            arguments={"entries": []},
        ),
    )
    problems = lint_cases([bad])
    assert any("not an enabled tool" in problem for problem in problems)


def test_lint_rejects_arguments_that_violate_the_schema() -> None:
    bad = EvalCase(
        id="BAD-002",
        source_type="synthetic",
        reference_time="2026-07-23T15:00:00+08:00",
        input="午饭45",
        expected=ExpectedBehavior(
            action="call_tool",
            tool="finance.log_expense",
            # The superseded spike expectation: no explicit personal/family scope.
            arguments={
                "name": "午饭",
                "input_amount": "45.00",
                "input_currency": "CNY",
                "occurred_on": "2026-07-23",
                "entry_kind": "expense",
                "category": "餐饮",
            },
        ),
    )
    problems = lint_cases([bad])
    assert any("is_family_expense" in problem for problem in problems)


def test_lint_rejects_a_future_payment_date() -> None:
    bad = EvalCase(
        id="BAD-003",
        source_type="synthetic",
        reference_time="2026-07-23T15:00:00+08:00",
        input="订了九月的机票1200，个人支出",
        expected=ExpectedBehavior(
            action="call_tool",
            tool="finance.log_expense",
            arguments={
                "name": "机票",
                "input_amount": "1200.00",
                "input_currency": "CNY",
                "occurred_on": "2026-09-01",
                "is_family_expense": False,
                "entry_kind": "expense",
                "category": "旅行",
            },
        ),
    )
    assert any("future" in problem for problem in lint_cases([bad]))


def test_lint_uses_the_shanghai_ledger_date_near_midnight() -> None:
    case = EvalCase(
        id="TZ-001",
        source_type="synthetic",
        # Already 2026-07-24 in Shanghai.
        reference_time="2026-07-23T16:30:00Z",
        input="今天午饭45，个人支出",
        expected=ExpectedBehavior(
            action="call_tool",
            tool="finance.log_expense",
            arguments={
                "name": "午饭",
                "input_amount": "45.00",
                "input_currency": "CNY",
                "occurred_on": "2026-07-24",
                "is_family_expense": False,
                "entry_kind": "expense",
                "category": "餐饮",
            },
        ),
    )
    assert lint_cases([case]) == []


# --- public provenance: synthetic witness exercises the same fail-closed code ---


def test_public_dataset_has_no_private_reviewed_claims() -> None:
    assert REVIEWED_CASE_DIGESTS == {}
    assert all(c.source_type != "user_provided_redacted" for c in CASES)
    assert len([c for c in CASES if c.id.startswith("PUB-")]) == 20
    assert verify_provenance(CASES) == []


@pytest.mark.parametrize(
    "origin_tag",
    ["from_live_defect_example", "from-real-user", "线上缺陷复现"],
)
def test_synthetic_case_rejects_real_origin_tag(origin_tag: str) -> None:
    synthetic = next(c for c in CASES if c.id.startswith("PUB-EXP-"))
    tagged = EvalCase(
        **{**synthetic.model_dump(), "tags": (*synthetic.tags, origin_tag)}
    )
    assert any(
        "synthetic case has a live or user provenance tag" in problem
        for problem in verify_provenance([tagged])
    )


def _test_only_reviewed_witness() -> tuple[EvalCase, dict[str, str]]:
    synthetic = next(c for c in CASES if c.id.startswith("PUB-EXP-"))
    witness = EvalCase(
        **{**synthetic.model_dump(), "source_type": "user_provided_redacted"}
    )
    return witness, {witness.id: semantic_case_digest(witness)}


def test_unregistered_reviewed_claim_fails_closed() -> None:
    witness, _ = _test_only_reviewed_witness()
    assert any(
        "is not one of the approved reviewed cases" in problem
        for problem in verify_provenance([witness])
    )


def test_test_only_synthetic_witness_exercises_positive_path() -> None:
    witness, registry = _test_only_reviewed_witness()
    assert verify_provenance(
        [witness], reviewed_case_digests=registry
    ) == []


def test_editing_test_only_witness_expression_fails() -> None:
    witness, registry = _test_only_reviewed_witness()
    edited = EvalCase(**{**witness.model_dump(), "input": witness.input + "0"})
    assert any(
        "reviewed case semantics were edited" in problem
        for problem in verify_provenance(
            [edited], reviewed_case_digests=registry
        )
    )


def test_editing_test_only_witness_output_fails() -> None:
    witness, registry = _test_only_reviewed_witness()
    payload = witness.model_dump(mode="python")
    payload["expected"]["arguments"]["category"] = "购物"
    edited = EvalCase.model_validate(payload)
    assert any(
        "reviewed case semantics were edited" in problem
        for problem in verify_provenance(
            [edited], reviewed_case_digests=registry
        )
    )


def test_dropping_test_only_witness_label_fails() -> None:
    witness, registry = _test_only_reviewed_witness()
    downgraded = EvalCase(
        **{**witness.model_dump(), "source_type": "synthetic"}
    )
    assert any(
        "no longer carries the user_provided_redacted label" in problem
        for problem in verify_provenance(
            [downgraded], reviewed_case_digests=registry
        )
    )


def _passing_outcomes() -> list[EvalOutcome]:
    return [
        EvalOutcome(
            case_id=case.id,
            case_digest=semantic_case_digest(case),
            evaluator="contract-test",
            passed=True,
            safety_pass=True,
            observed_action=case.expected.action,
        )
        for case in CASES
    ]


def test_case_distribution_is_explicitly_inventory_not_a_score() -> None:
    split = case_distribution(CASES)
    assert sum(split.values()) == len(CASES)
    assert ("user_provided_redacted", "finance") not in split
    assert split[("synthetic", "finance")] >= 20
    assert split[("synthetic", "authorization")] > 0
    assert split[("synthetic", "mcp")] > 0


def test_actual_scores_are_split_by_source_and_domain_never_pooled() -> None:
    """DEV-037 scores consume outcomes, not the number of cases in the file."""
    scores = score_outcomes(CASES, _passing_outcomes())

    assert ("user_provided_redacted", "finance") not in scores
    assert scores[("synthetic", "finance")].passed >= 20
    assert scores[("synthetic", "authorization")].total > 0
    assert scores[("synthetic", "mcp")].total > 0
    assert all(cell.as_dict()["pass_rate"] == 1.0 for cell in scores.values())


def test_scores_refuse_a_result_from_before_the_case_changed() -> None:
    outcomes = _passing_outcomes()
    first = outcomes[0]
    outcomes[0] = first.model_copy(update={"case_digest": "0" * 64})

    with pytest.raises(ValueError, match="stale result digest"):
        score_outcomes(CASES, outcomes)


def test_result_digest_binds_the_source_bucket_too() -> None:
    case = next(c for c in CASES if c.source_type == "synthetic")
    relabelled = case.model_copy(update={"source_type": "prd_example"})

    assert semantic_case_digest(relabelled) != semantic_case_digest(case)


def test_scores_refuse_to_pool_different_evaluators() -> None:
    outcomes = _passing_outcomes()
    outcomes[0] = outcomes[0].model_copy(update={"evaluator": "another-model"})

    with pytest.raises(ValueError, match="mix evaluators"):
        score_outcomes(CASES, outcomes)


def test_scores_refuse_incomplete_or_empty_domain_evidence() -> None:
    without_mcp = [
        outcome
        for outcome in _passing_outcomes()
        if outcome.case_id != "MCP-001"
    ]

    with pytest.raises(ValueError, match="results are incomplete"):
        score_outcomes(CASES, without_mcp)
    with pytest.raises(ValueError, match="no outcomes for: mcp"):
        score_outcomes(CASES, without_mcp, require_complete=False)


def test_new_public_cases_have_explicit_contract_outcomes() -> None:
    public = [c for c in CASES if c.id.startswith("PUB-")]
    assert len(public) == 20
    for case in public:
        assert case.source_type == "synthetic"
        assert case.expected.action == "call_tool"
        assert case.expected.tool in {"finance.log_expense", FINANCE_QUERY_TOOL}
        if case.expected.tool == "finance.log_expense":
            assert "is_family_expense" in case.expected.arguments
            assert case.expected.arguments["category"] in ALLOWED_EXPENSE_CATEGORIES
        else:
            assert case.expected.arguments["view"] in {
                "total", "by_category", "records"
            }


def test_provenance_cannot_contradict_itself() -> None:
    # `is_synthetic` is derived, so there is no second field to disagree with.
    assert not hasattr(EvalCase, "synthetic")
    case = CASES[0]
    with pytest.raises(Exception):
        EvalCase(**{**case.model_dump(), "synthetic": True})


def test_dataset_carries_no_obsolete_tool_names() -> None:
    text = DEFAULT_DATASET.read_text(encoding="utf-8")
    for obsolete in OBSOLETE_TOOL_NAMES:
        assert obsolete not in text


# --- superseded spike semantics must not come back --------------------------


def test_missing_family_scope_always_asks_and_never_writes() -> None:
    superseded = cases_tagged("supersedes_spike_default_personal")
    assert len(superseded) >= 2
    for case in superseded:
        assert case.expected.action == "ask_clarification"
        assert "is_family_expense" in case.expected.missing_fields

    for case in CASES:
        if case.expected.tool == "finance.log_expense":
            assert "is_family_expense" in case.expected.arguments


def test_foreign_currency_converts_server_side_instead_of_asking() -> None:
    converted = cases_tagged("server_side_conversion")
    assert len(converted) >= 2
    for case in converted:
        assert case.expected.action == "call_tool"
        assert case.expected.arguments["input_currency"] != "CNY"

    # The one currency case that still asks is the genuinely ambiguous symbol.
    ambiguous = cases_tagged("ambiguous_currency_symbol")
    assert ambiguous and all(
        c.expected.action == "ask_clarification" for c in ambiguous
    )


def test_a_two_entry_message_writes_nothing_while_batch_is_disabled() -> None:
    batch_cases = cases_tagged("batch_disabled")
    assert batch_cases
    for case in batch_cases:
        assert case.expected.action == "reject"
        assert case.expected.reason_code == ErrorCode.BATCH_ATOMICITY_UNAVAILABLE


def test_modify_and_delete_are_refused_not_queued_as_pending_actions() -> None:
    r4_cases = cases_tagged("r4_not_in_phase_1")
    assert len(r4_cases) >= 2
    for case in r4_cases:
        assert case.expected.action == "reject"
        assert case.expected.reason_code == ErrorCode.UNSUPPORTED_OPERATION
    assert "pending_action" not in json.dumps(
        [c.expected.model_dump(mode="json") for c in CASES]
    )


# --- coverage ---------------------------------------------------------------


def test_every_model_callable_finance_tool_is_exercised() -> None:
    """Coverage is scoped to what the model can actually choose.

    These cases are model-routing evidence: given an utterance, which tool. A
    tool the model is never offered has no routing behaviour to cover, so
    demanding a case for `finance.update_expense_category` would force a
    fictional one -- and a fictional case asserting the model calls a tool it
    cannot reach is worse than no case at all.

    Its real coverage is elsewhere and stronger: `test_modify_and_delete_are_
    refused_not_queued_as_pending_actions` above still requires the model to
    reject "改一下上周那笔的分类" with `UNSUPPORTED_OPERATION`. That invariant
    is exactly what `model_callable=False` preserves, and it would start failing
    the day someone made the tool model-facing.
    """
    expected_tools = {c.expected.tool for c in CASES if c.expected.tool}
    model_callable_finance = {
        name
        for name in load_manifest()["model_callable_tools"]
        if name.startswith("finance.")
    }
    assert model_callable_finance.issubset(expected_tools)


def test_the_category_update_is_not_a_routing_outcome() -> None:
    """No eval case may expect the model to call the update tool."""
    assert "finance.update_expense_category" not in {
        case.expected.tool for case in CASES if case.expected.tool
    }


def test_reason_codes_are_stable_codes() -> None:
    for case in CASES:
        if case.expected.reason_code is not None:
            assert case.expected.reason_code in set(ErrorCode)


def test_relative_dates_and_confirmed_mappings_are_covered() -> None:
    for tag in (
        "relative_date",
        "confirmed_mapping",
        "activity_context_overrides_food",
        "refund",
        "aa_reimbursement",
        "occurred_on_is_payment_date",
        "prompt_injection",
    ):
        assert cases_tagged(tag), f"no case covers {tag}"


# --- decisions Henson confirmed after the Wave 0 report ---------------------


def test_indirect_family_wording_counts_as_family_expense() -> None:
    cases = cases_tagged("implicit_family_wording_is_family")
    assert cases
    for case in cases:
        assert case.expected.action == "call_tool"
        assert case.expected.arguments["is_family_expense"] is True


def test_a_destination_without_a_tag_is_handed_to_the_server_resolver() -> None:
    cases = cases_tagged("travel_resolver_server_side")
    assert cases
    for case in cases:
        # The model contributes only the destination it extracted. Deduplicating
        # existing trip tags, reusing the sole match, creating the base tag and
        # asking between 东京01 / 东京02 all happen in Finance MCP before the
        # write, because only it may read the ledger.
        assert case.expected.action == "call_tool"
        trip_tag = case.expected.arguments["trip_tag"]
        # A bare destination, never a numbered instance: choosing between
        # 东京01 and 东京02 needs the ledger, which only Finance MCP may read.
        # Asserting the property rather than one literal, so a new destination
        # does not need this test edited -- an abbreviation or a numbered guess
        # still fails.
        assert trip_tag and "#" not in trip_tag
        assert not any(character.isdigit() for character in trip_tag)
        if "tag_from_abbreviation_by_henson_decision_2026_08_02" not in case.tags:
            assert trip_tag in case.input, (
                f"{case.id}: the tag must be the destination the user actually "
                "wrote, not one inferred from an abbreviation"
            )
        assert case.expected.arguments["category"] == "旅行"


def test_public_travel_cases_do_not_claim_private_abbreviation_exemptions() -> None:
    assert cases_tagged("tag_from_abbreviation_by_henson_decision_2026_08_02") == []
    for case in cases_tagged("travel"):
        if case.expected.tool == "finance.log_expense":
            tag = case.expected.arguments.get("trip_tag")
            if tag is not None:
                assert tag in case.input


def test_this_month_spans_the_whole_month_not_up_to_today() -> None:
    cases = cases_tagged("relative_month_ends_at_month_end")
    assert cases
    for case in cases:
        date_range = case.expected.arguments["date_range"]
        assert date_range == {"start": "2026-07-01", "end": "2026-07-31"}
