"""Evaluation cases with enforced provenance and contract conformance.

Two failure modes this module exists to prevent:

- claiming real-world accuracy from synthetic data. Every case carries a source
  label, and `user_provided_redacted` may only be applied to expressions Henson
  has actually reviewed. Nothing may be relabelled to make a number look better;
- inheriting superseded semantics. The spike's baseline was written before the
  contract froze, so it expects a default personal scope, a clarification for
  foreign currency, and two separate writes for a two-entry message. All three
  are now wrong. Linting every expected call against the generated manifest is
  what stops those expectations from quietly surviving.

Linting is offline and deterministic. It says nothing about model accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_agent_core.errors import ErrorCode
from personal_agent_core.manifest import canonical_json, load_manifest
from personal_agent_core.timeutil import ledger_date, parse_ledger_date, parse_rfc3339


_REPOSITORY_DATASET: Final[Path] = (
    Path(__file__).parents[2] / "evals" / "finance_public_synthetic_v1.jsonl"
)
_PACKAGED_DATASET: Final[Path] = (
    Path(__file__).with_name("data") / "finance_public_synthetic_v1.jsonl"
)
DEFAULT_DATASET: Final[Path] = (
    _REPOSITORY_DATASET if _REPOSITORY_DATASET.exists() else _PACKAGED_DATASET
)

#: Names that were replaced before the contract froze. They must never reappear
#: in a dataset, a fixture or a production catalog.
OBSOLETE_TOOL_NAMES: Final[tuple[str, ...]] = (
    "finance.query_transactions",
    "finance.analyze_period",
    "meta.get_capabilities",
)

SourceType = Literal["synthetic", "prd_example", "user_provided_redacted"]
Action = Literal["call_tool", "ask_clarification", "reject"]
PriorEventType = Literal["user_message", "operation_result"]
PriorOperationState = Literal[
    "waiting_for_clarification",
    "succeeded",
    "needs_manual_review",
]
PRIOR_OPERATION_STATES: Final[frozenset[PriorOperationState]] = frozenset(
    {"waiting_for_clarification", "succeeded", "needs_manual_review"}
)

#: The public corpus contains no reviewed personal expressions. Keep the
#: registry empty so a false reviewed-source claim fails closed. Tests may
#: inject a synthetic witness registry to exercise the same verifier.
REVIEWED_CASE_DIGESTS: Final[dict[str, str]] = {}


#: Which capability a case measures. Scores must never be pooled across these:
#: the acceptance for `DEV-037` is that Finance, authorisation and MCP are
#: reported separately, because a high Finance number can otherwise hide a
#: refusal boundary that does not hold. `authorization` is every case whose
#: correct answer is a refusal at a capability or permission boundary rather
#: than an accounting judgement.
EvalDomain = Literal["finance", "authorization", "mcp"]

_AUTHORIZATION_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.TOOL_NOT_ALLOWLISTED,
        ErrorCode.SCOPE_DENIED,
        ErrorCode.HOST_CONTEXT_MISMATCH,
        ErrorCode.UNSUPPORTED_OPERATION,
    }
)


def domain_of(case: "EvalCase") -> EvalDomain:
    """Classify a case, derived from its expected behaviour rather than stored.

    Stored alongside the case it would be one more field to disagree with the
    others; derived, it cannot drift from what the case actually expects.
    """
    if case.expected.reason_code in _AUTHORIZATION_CODES:
        return "authorization"
    if case.expected.tool == "meta.capabilities":
        return "mcp"
    return "finance"


class ExpectedBehavior(BaseModel):
    """What the agent should do, expressed against the frozen contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Action
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    reason_code: ErrorCode | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "ExpectedBehavior":
        if self.action == "call_tool":
            if not self.tool:
                raise ValueError("call_tool cases must name a tool")
            if self.reason_code is not None:
                raise ValueError("a successful call has no error reason code")
        else:
            if self.tool or self.arguments:
                raise ValueError(
                    "non-call cases must not declare a tool or arguments"
                )
            if self.reason_code is None:
                raise ValueError(
                    "ask_clarification and reject cases need a stable reason code"
                )
        return self


class EvalPriorTurn(BaseModel):
    """One production-shaped event preceding the evaluated user message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: PriorEventType
    content: dict[str, Any]
    tool: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "EvalPriorTurn":
        if self.event_type == "user_message":
            text = self.content.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("user_message prior turns require content.text")
            if self.tool is not None:
                raise ValueError("user_message prior turns cannot name a tool")
        else:
            state = self.content.get("state")
            if not isinstance(state, str) or not state.strip():
                raise ValueError("operation_result prior turns require content.state")
            if state not in PRIOR_OPERATION_STATES:
                raise ValueError(f"unsupported prior operation state {state!r}")
        return self


class EvalCase(BaseModel):
    """One evaluated expression."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source_type: SourceType
    reference_time: str
    input: str
    prior_turns: tuple[EvalPriorTurn, ...] = ()
    expected: ExpectedBehavior
    tags: tuple[str, ...] = ()

    @property
    def is_synthetic(self) -> bool:
        """Derived, never stored, so a label cannot contradict its source."""
        return self.source_type != "user_provided_redacted"


def semantic_case_digest(case: EvalCase) -> str:
    """Bind an eval result or provenance claim to the case's full semantics."""
    payload = {
        "id": case.id,
        "source_type": case.source_type,
        "reference_time": case.reference_time,
        "input": case.input,
        "prior_turns": [
            turn.model_dump(mode="json") for turn in case.prior_turns
        ],
        "expected": case.expected.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def load_cases(path: Path = DEFAULT_DATASET) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            cases.append(EvalCase.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"invalid eval case at line {number}: {exc}") from exc
    return cases


def _schema_for(tool_name: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
    for entry in manifest["tools"]:
        if entry["name"] == tool_name:
            return entry["model_input_schema"]
    return None


def verify_provenance(
    cases: list[EvalCase], *, complete: bool = True,
    reviewed_case_digests: Mapping[str, str] | None = None
) -> list[str]:
    """Reject unregistered reviewed claims and changed pinned semantics.

    The production public registry is empty. A test-only registry lets
    adversarial tests exercise the full positive and mutation paths without
    distributing private user-case digests.
    """
    registry = (
        REVIEWED_CASE_DIGESTS
        if reviewed_case_digests is None
        else reviewed_case_digests
    )
    problems: list[str] = []
    claimed = {c.id for c in cases if c.source_type == "user_provided_redacted"}

    for extra in sorted(claimed - set(registry)):
        problems.append(
            f"{extra}: claims user_provided_redacted but is not one of the "
            "approved reviewed cases"
        )
    if complete:
        for missing in sorted(set(registry) - claimed):
            problems.append(
                f"{missing}: is a reviewed case but no longer carries the "
                "user_provided_redacted label"
            )
    for case in cases:
        pinned = registry.get(case.id)
        if pinned is None:
            continue
        actual = semantic_case_digest(case)
        if actual != pinned:
            problems.append(
                f"{case.id}: the reviewed case semantics were edited "
                f"(pinned {pinned}, found {actual}); update the reviewed-case registry "
                "only after the input and expected output are reviewed"
            )
    return problems


def case_distribution(cases: list[EvalCase]) -> dict[tuple[SourceType, EvalDomain], int]:
    """Case counts keyed by exact source type and domain.

    This is inventory, not a score. Calling it a score was the DEV-037 review
    defect: no model or deterministic boundary had run, yet case counts were
    presented as though they measured correctness.
    """
    counts: dict[tuple[SourceType, EvalDomain], int] = {}
    for case in cases:
        key = (case.source_type, domain_of(case))
        counts[key] = counts.get(key, 0) + 1
    return counts


class EvalOutcome(BaseModel):
    """One evaluator's result, bound to the exact case semantics it ran."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    case_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator: str = Field(min_length=1)
    passed: bool
    safety_pass: bool
    observed_action: str
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreCell:
    """An actual score for one provenance/domain cell."""

    total: int
    passed: int
    safety_passed: int

    def as_dict(self) -> dict[str, int | float]:
        return {
            "total": self.total,
            "passed": self.passed,
            "safety_passed": self.safety_passed,
            "pass_rate": self.passed / self.total,
            "safety_rate": self.safety_passed / self.total,
        }


def load_outcomes(path: Path) -> list[EvalOutcome]:
    """Load evaluator results without accepting partial or malformed lines."""
    outcomes: list[EvalOutcome] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            outcomes.append(EvalOutcome.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"invalid eval outcome at line {number}: {exc}") from exc
    return outcomes


def score_outcomes(
    cases: list[EvalCase],
    outcomes: list[EvalOutcome],
    *,
    require_complete: bool = True,
    required_domains: tuple[EvalDomain, ...] = ("finance", "authorization", "mcp"),
) -> dict[tuple[SourceType, EvalDomain], ScoreCell]:
    """Compute real pass rates; refuse stale, duplicate or incomplete evidence."""
    by_id = {case.id: case for case in cases}
    if len(by_id) != len(cases):
        raise ValueError("dataset contains duplicate case ids")
    evaluators = {outcome.evaluator for outcome in outcomes}
    if len(evaluators) > 1:
        raise ValueError(
            "results mix evaluators and cannot be pooled: "
            + ", ".join(sorted(evaluators))
        )

    seen: set[str] = set()
    buckets: dict[tuple[SourceType, EvalDomain], list[EvalOutcome]] = {}
    for outcome in outcomes:
        if outcome.case_id in seen:
            raise ValueError(f"duplicate outcome for {outcome.case_id}")
        seen.add(outcome.case_id)
        case = by_id.get(outcome.case_id)
        if case is None:
            raise ValueError(f"outcome names unknown case {outcome.case_id}")
        expected_digest = semantic_case_digest(case)
        if outcome.case_digest != expected_digest:
            raise ValueError(
                f"{outcome.case_id}: stale result digest {outcome.case_digest}; "
                f"current case is {expected_digest}"
            )
        buckets.setdefault((case.source_type, domain_of(case)), []).append(outcome)

    if require_complete:
        missing = sorted(set(by_id) - seen)
        if missing:
            raise ValueError(f"results are incomplete; missing: {', '.join(missing)}")
    populated_domains = {domain for (_, domain), values in buckets.items() if values}
    missing_domains = sorted(set(required_domains) - populated_domains)
    if missing_domains:
        raise ValueError(
            "results cannot claim split domain scores; no outcomes for: "
            + ", ".join(missing_domains)
        )

    return {
        key: ScoreCell(
            total=len(values),
            passed=sum(outcome.passed for outcome in values),
            safety_passed=sum(outcome.safety_pass for outcome in values),
        )
        for key, values in sorted(buckets.items())
    }


def lint_cases(
    cases: list[EvalCase], *, complete_provenance: bool = False
) -> list[str]:
    """Return every problem found. An empty list is the only passing result.

    Subset callers leave ``complete_provenance`` false so the reviewed cases
    outside their slice are not reported as missing. Whole-dataset entrypoints
    turn it on and get both label and semantic completeness in one pass.
    """
    import jsonschema

    manifest = load_manifest()
    enabled = set(manifest["enabled_tools"])
    problems: list[str] = list(
        verify_provenance(cases, complete=complete_provenance)
    )

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            problems.append(f"{case.id}: duplicate case id")
        seen.add(case.id)

        try:
            reference = parse_rfc3339(case.reference_time)
        except Exception:
            problems.append(f"{case.id}: reference_time is not RFC 3339")
            continue

        for obsolete in OBSOLETE_TOOL_NAMES:
            if obsolete in json.dumps(case.model_dump(mode="json")):
                problems.append(f"{case.id}: references obsolete tool {obsolete}")

        if case.expected.action != "call_tool":
            continue

        tool_name = case.expected.tool or ""
        if tool_name not in enabled:
            problems.append(
                f"{case.id}: expects {tool_name!r}, which is not an enabled tool"
            )
            continue

        schema = _schema_for(tool_name, manifest)
        if schema is None:  # pragma: no cover - guarded by the enabled check
            problems.append(f"{case.id}: no schema for {tool_name!r}")
            continue

        try:
            jsonschema.validate(
                case.expected.arguments,
                schema,
                format_checker=jsonschema.FormatChecker(),
            )
        except jsonschema.ValidationError as exc:
            problems.append(
                f"{case.id}: arguments violate {tool_name} schema: {exc.message}"
            )

        occurred_on = case.expected.arguments.get("occurred_on")
        if isinstance(occurred_on, str):
            try:
                day = parse_ledger_date(occurred_on)
            except ValueError:
                problems.append(f"{case.id}: occurred_on is not YYYY-MM-DD")
            else:
                if day > ledger_date(reference):
                    problems.append(
                        f"{case.id}: occurred_on is in the future; "
                        "occurred_on is the payment date, never a future event date"
                    )

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate eval cases against the frozen tool manifest."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()

    cases = load_cases(args.dataset)
    # The whole file is here, so the completeness half of the provenance check
    # is meaningful: a reviewed expression that lost its label is caught.
    problems = lint_cases(
        cases,
        complete_provenance=args.dataset.resolve() == DEFAULT_DATASET.resolve(),
    )
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.source_type] = counts.get(case.source_type, 0) + 1

    print(f"dataset: {args.dataset}")
    print(f"cases: {len(cases)}")
    for source_type in sorted(counts):
        print(f"  {source_type}: {counts[source_type]}")

    # Inventory only. Actual correctness rates come from `score_main`, which
    # requires one digest-bound outcome for every case and refuses empty domains.
    split = case_distribution(cases)
    print("\ncase distribution by source and domain (not scores):")
    for source_type in ("user_provided_redacted", "synthetic", "prd_example"):
        for domain in ("finance", "authorization", "mcp"):
            print(
                f"  {source_type:24} {domain:14} "
                f"{split.get((source_type, domain), 0)}"
            )

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(1)
    print("\nall cases conform to the frozen contract")


def score_main() -> None:
    """Score a complete evaluator result set, split by source and domain."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute digest-bound eval pass rates by provenance and domain. "
            "Refuses stale, incomplete, or empty-domain result sets."
        )
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cases = load_cases(args.dataset)
    problems = lint_cases(
        cases,
        complete_provenance=args.dataset.resolve() == DEFAULT_DATASET.resolve(),
    )
    if problems:
        raise SystemExit("dataset lint failed before scoring: " + "; ".join(problems))
    cells = score_outcomes(
        cases,
        load_outcomes(args.results),
        required_domains=tuple(sorted({domain_of(case) for case in cases})),
    )

    payload = {
        source: {
            domain: cell.as_dict()
            for (cell_source, domain), cell in cells.items()
            if cell_source == source
        }
        for source in ("user_provided_redacted", "synthetic", "prd_example")
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"dataset: {args.dataset}")
        print(f"results: {args.results}")
        print("scores by source and domain:")
        for (source, domain), cell in cells.items():
            print(
                f"  {source:24} {domain:14} "
                f"strict={cell.passed}/{cell.total} ({cell.passed / cell.total:.1%}) "
                f"safety={cell.safety_passed}/{cell.total} "
                f"({cell.safety_passed / cell.total:.1%})"
            )
