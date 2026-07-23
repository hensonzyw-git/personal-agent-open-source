from pathlib import Path

from personal_agent_spike.contracts import load_eval_cases
from personal_agent_spike.offline_eval import evaluate_expected_calls


DATASET = Path("evals/finance_expense_v0.1.jsonl")


def test_dataset_has_30_unique_cases() -> None:
    cases = load_eval_cases(DATASET)
    assert len(cases) == 30
    assert len({case.id for case in cases}) == 30


def test_dataset_provenance_is_explicit() -> None:
    cases = load_eval_cases(DATASET)
    assert all(case.source_type in {"synthetic", "prd_example"} for case in cases)
    assert all(case.synthetic for case in cases)


def test_expected_tool_calls_pass_policy() -> None:
    result = evaluate_expected_calls(DATASET)
    assert result["policy_mismatches"] == []
