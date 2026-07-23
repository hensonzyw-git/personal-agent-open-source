from __future__ import annotations

import json
from pathlib import Path

from personal_agent_spike.contracts import load_eval_cases
from personal_agent_spike.policy import decide_tool_call


DEFAULT_DATASET = Path(__file__).parents[2] / "evals" / "finance_expense_v0.1.jsonl"


def evaluate_expected_calls(dataset: Path = DEFAULT_DATASET) -> dict:
    cases = load_eval_cases(dataset)
    evaluated = 0
    policy_mismatches: list[dict[str, str]] = []
    for case in cases:
        expected_calls = (
            [{"tool": case.expected.tool, "arguments": case.expected.arguments}]
            if case.expected.action == "call_tool"
            else [
                {"tool": call.tool, "arguments": call.arguments}
                for call in case.expected.calls
            ]
            if case.expected.action == "call_tools"
            else []
        )
        for index, expected_call in enumerate(expected_calls):
            evaluated += 1
            decision = decide_tool_call(
                expected_call["tool"] or "",
                expected_call["arguments"],
                {"finance.expense.write"},
            )
            if decision.outcome != "allow":
                policy_mismatches.append(
                    {
                        "id": case.id,
                        "call_index": str(index),
                        "outcome": decision.outcome,
                        "reason_code": decision.reason_code,
                    }
                )
    return {
        "dataset_cases": len(cases),
        "expected_tool_calls_evaluated": evaluated,
        "policy_mismatches": policy_mismatches,
        "note": "This validates the harness and expected calls, not model accuracy.",
    }


def main() -> None:
    print(json.dumps(evaluate_expected_calls(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
