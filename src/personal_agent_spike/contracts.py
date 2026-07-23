from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


Action = Literal["call_tool", "ask_clarification", "pending_action", "reject"]
SourceType = Literal["synthetic", "prd_example", "user_provided_redacted"]


class ExpectedBehavior(BaseModel):
    action: Action
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "ExpectedBehavior":
        if self.action == "call_tool" and not self.tool:
            raise ValueError("call_tool cases require a tool")
        if self.action != "call_tool" and self.tool is not None:
            raise ValueError("non-call cases cannot declare a tool")
        return self


class EvalCase(BaseModel):
    id: str
    source_type: SourceType
    synthetic: bool
    reference_time: str
    input: str
    prior_turns: list[str] = Field(default_factory=list)
    expected: ExpectedBehavior
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_provenance(self) -> "EvalCase":
        if self.source_type == "user_provided_redacted" and self.synthetic:
            raise ValueError("user-provided cases cannot be marked synthetic")
        if self.source_type == "synthetic" and not self.synthetic:
            raise ValueError("synthetic cases must be marked synthetic")
        return self


def load_eval_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            cases.append(EvalCase.model_validate(json.loads(raw_line)))
        except Exception as exc:
            raise ValueError(f"invalid eval case at line {line_number}: {exc}") from exc

    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("eval case IDs must be unique")
    return cases
