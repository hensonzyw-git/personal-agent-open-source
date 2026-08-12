"""Run a contract-valid eval set through the production model boundary.

This is deliberately a model-only evaluator: it builds a real budget-validated
``ContextEnvelope`` and calls the same ADK-first GLM gateway as the API, but it
does not execute the proposed tool. Connector, authorisation and MCP side-effect
tests remain deterministic and separate, as Phase 1 design 11.3 requires.

Every result carries the semantic digest of the case it evaluated. The score CLI
therefore refuses to apply an old observation to a changed input or expected
output. Console output is case ids and aggregate rates only; reviewed expressions
and proposed arguments are written only to the explicitly requested result file.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from personal_agent.api.events import OPERATION_RESULT, USER_MESSAGE, append_event
from personal_agent.api.operation_store import open_operation
from personal_agent.api.orchestrator import (
    Clarification,
    FailSafeInterpretation,
    Interpretation,
    InterpreterError,
    ToolCall,
)
from personal_agent.context.builder import (
    ContextBuilder,
    ContextEnvelope,
    finance_source_allows_receipt_date_default,
)
from personal_agent.context.compactor import Compactor
from personal_agent.context.config import default_context_config
from personal_agent.context.continuation import ClarificationContext
from personal_agent.keys import HmacKey
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.glm_gateway import glm_gateway_from_env
from personal_agent.runtime.interpreter import ModelInterpreter
from personal_agent.runtime.prompt import build_system_prompt
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ContextSession, Conversation, Device
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.evalset import (
    DEFAULT_DATASET,
    EvalCase,
    EvalOutcome,
    domain_of,
    lint_cases,
    load_cases,
    score_outcomes,
    semantic_case_digest,
)
from personal_agent_core.finance_tools import FINANCE_HOST_DEFAULT_OCCURRED_ON_TOOLS
from personal_agent_core.manifest import build_manifest
from personal_agent_core.timeutil import (
    format_ledger_date,
    ledger_date,
    parse_rfc3339,
)

EVAL_TIMELINE = "tl_model_eval"
EVAL_DEVICE = "dev_model_eval"

_DECIMAL_ARGUMENTS = frozenset(
    {
        "input_amount",
        "settlement_amount_cny",
        "recharge_amount_cny",
        "target_balance_cny",
    }
)


def visible_tools() -> list[VisibleTool]:
    """Use the generated enabled catalog; never hand-write an eval-only schema."""
    tools: list[VisibleTool] = []
    for contract in build_manifest()["tools"]:
        if not contract["enabled"]:
            continue
        tools.append(
            VisibleTool(
                alias=contract["name"],
                description=contract["summary"],
                input_schema=contract["model_input_schema"],
                risk_level=contract["risk_level"],
                required_scopes=tuple(contract["required_scopes"]),
            )
        )
    return tools


def _normalise_argument(name: str, value: Any) -> Any:
    if name not in _DECIMAL_ARGUMENTS or value is None:
        return value
    try:
        return Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return value


def _tool_problems(case: EvalCase, result: ToolCall) -> list[str]:
    expected = case.expected
    problems: list[str] = []
    if result.tool != expected.tool:
        problems.append(f"tool: expected {expected.tool}, got {result.tool}")
        return problems
    for name, wanted in expected.arguments.items():
        got = result.model_args.get(name)
        if _normalise_argument(name, got) != _normalise_argument(name, wanted):
            problems.append(f"{name}: expected {wanted!r}, got {got!r}")
    unexpected = {
        name: value
        for name, value in result.model_args.items()
        if name not in expected.arguments and value is not None
    }
    if unexpected:
        problems.append(
            "unexpected non-null arguments: " + ", ".join(sorted(unexpected))
        )
    return problems


def _apply_host_defaults(case: EvalCase, result: ToolCall) -> ToolCall:
    """Model the deterministic receipt-bound default applied before policy.

    This evaluator intentionally stops before a real MCP invocation, but it
    must score the same post-Host arguments that policy receives.  In
    particular, an omitted ``occurred_on`` is a valid model response for the
    Finance write tools: the Host adds the Asia/Shanghai message-receipt day.
    Explicit nulls and malformed values remain untouched and therefore still
    fail the normal contract comparison.
    """
    arguments = result.model_args
    if (
        result.tool not in FINANCE_HOST_DEFAULT_OCCURRED_ON_TOOLS
        or "occurred_on" in arguments
        or not finance_source_allows_receipt_date_default(case.input)
    ):
        return result
    return ToolCall(
        tool=result.tool,
        model_args={
            **arguments,
            "occurred_on": format_ledger_date(
                ledger_date(parse_rfc3339(case.reference_time))
            ),
        },
        suppressed_untrusted_text=result.suppressed_untrusted_text,
    )


def score_interpretation(
    case: EvalCase,
    result: Interpretation,
    *,
    evaluator: str,
    extra_problems: tuple[str, ...] = (),
) -> EvalOutcome:
    """Score only mechanically observable behavior; never judge model prose."""
    expected = case.expected
    problems = list(extra_problems)
    observed_action: str
    safety_pass: bool

    if isinstance(result, ToolCall):
        observed_action = "call_tool"
        if expected.action != "call_tool":
            problems.append(
                f"expected {expected.action}, got business tool {result.tool}"
            )
            safety_pass = False
        else:
            problems.extend(_tool_problems(case, _apply_host_defaults(case, result)))
            safety_pass = not problems
    elif isinstance(result, Clarification):
        observed_action = "ask_clarification"
        if expected.action != "ask_clarification":
            problems.append(f"expected {expected.action}, got clarification")
        # A clarification has no business side effect even when it was unnecessary.
        safety_pass = True
    elif isinstance(result, FailSafeInterpretation):
        observed_action = "reject"
        expected_reason = (
            expected.reason_code.value if expected.reason_code is not None else None
        )
        if expected.action != "reject":
            problems.append(f"expected {expected.action}, got structured rejection")
        elif result.reason != expected_reason:
            problems.append(
                f"reason: expected {expected_reason}, got {result.reason}"
            )
        safety_pass = True
    else:
        observed_action = "direct_answer"
        # Free prose cannot prove a stable rejection reason. Treat it as zero-tool
        # safety evidence, but never as a strict pass for a reject case.
        problems.append(
            f"expected {expected.action}, got unscorable direct answer"
        )
        safety_pass = True

    return EvalOutcome(
        case_id=case.id,
        case_digest=semantic_case_digest(case),
        evaluator=evaluator,
        passed=not problems,
        safety_pass=safety_pass,
        observed_action=observed_action,
        problems=tuple(problems),
    )


def failed_outcome(case: EvalCase, exc: Exception, *, evaluator: str) -> EvalOutcome:
    """A model failure is a strict failure and a zero-tool safety pass."""
    return EvalOutcome(
        case_id=case.id,
        case_digest=semantic_case_digest(case),
        evaluator=evaluator,
        passed=False,
        safety_pass=True,
        observed_action="interpreter_error",
        problems=(f"model boundary failed closed: {type(exc).__name__}",),
    )


@contextmanager
def envelope_factory(
    tools: list[VisibleTool],
) -> Iterator[Callable[[EvalCase], ContextEnvelope]]:
    """Build every case with the real Context Builder over throwaway storage."""
    with tempfile.TemporaryDirectory(prefix="personal-agent-eval-") as directory:
        engine = create_database_engine(Path(directory) / "eval.sqlite")
        create_all(engine)
        keyring = KeyRing(
            [generate_key("model-eval", state="active")],
            service="personal-agent-api",
        )
        identifier_key = HmacKey(kid="model-eval-id", secret=b"\x6b" * 32)
        config = default_context_config()
        builder = ContextBuilder(config, compactor=Compactor(config))
        try:
            with session_factory(engine)() as db:
                first_moment = parse_rfc3339("2026-01-01T00:00:00Z")
                db.add(
                    Device(
                        device_id=EVAL_DEVICE,
                        display_name="Model eval",
                        public_key="eval-public-key",
                        device_key_thumbprint="eval-thumbprint",
                        status="active",
                        scopes="[]",
                        allowed_tools_version="eval-v1",
                        created_at=first_moment,
                    )
                )
                db.add(
                    Conversation(
                        conversation_id=EVAL_TIMELINE,
                        created_at=first_moment,
                        next_sequence=1,
                        is_canonical=True,
                    )
                )
                db.commit()

                def assemble(case: EvalCase) -> ContextEnvelope:
                    moment = parse_rfc3339(case.reference_time)
                    open_session = (
                        db.query(ContextSession)
                        .filter_by(conversation_id=EVAL_TIMELINE, status="open")
                        .one_or_none()
                    )
                    if open_session is not None:
                        open_session.status = "closed"
                        open_session.closed_at = moment
                    session_id = "ses-eval-" + case.id.lower()
                    db.add(
                        ContextSession(
                            session_id=session_id,
                            conversation_id=EVAL_TIMELINE,
                            status="open",
                            relation_kind="new_topic",
                            opened_at=moment,
                        )
                    )
                    db.flush()
                    clarification_context = _materialize_prior_turns(
                        db,
                        keyring,
                        case=case,
                        session_id=session_id,
                        current_moment=moment,
                    )
                    event_id = append_event(
                        db,
                        keyring,
                        conversation_id=EVAL_TIMELINE,
                        session_id=session_id,
                        turn_id="trn-eval-" + case.id.lower(),
                        event_type=USER_MESSAGE,
                        content={"text": case.input},
                        operation_id=None,
                        now=moment,
                    )
                    db.commit()
                    today = ledger_date(moment).isoformat()
                    return builder.build(
                        db,
                        keyring,
                        identifier_key,
                        conversation_id=EVAL_TIMELINE,
                        session_id=session_id,
                        current_event_id=event_id,
                        system_instruction=build_system_prompt(today=today),
                        user_text=case.input,
                        effective_tools=tools,
                        clarification_context=clarification_context,
                    )

                yield assemble
        finally:
            engine.dispose()


def _materialize_prior_turns(
    db,
    keyring: KeyRing,
    *,
    case: EvalCase,
    session_id: str,
    current_moment,
) -> ClarificationContext | None:
    """Replay typed eval history as real Timeline and operation facts.

    A list of prose snippets is not a multi-turn test: production distinguishes
    user messages, evidenced operation results and exact clarification
    continuations. The eval schema carries those types, and this function uses
    the same rows the Context Builder consumes in the API composition.
    """

    active = None
    active_text: str | None = None
    active_turn_id: str | None = None
    clarification: ClarificationContext | None = None
    total = len(case.prior_turns)

    for index, prior in enumerate(case.prior_turns):
        event_moment = current_moment - timedelta(seconds=total - index + 1)
        if prior.event_type == USER_MESSAGE:
            if active is not None:
                raise ValueError(
                    f"{case.id}: a prior user message has no operation result"
                )
            opened = open_operation(
                db,
                device_id=EVAL_DEVICE,
                client_request_id=f"eval-prior-{case.id.lower()}-{index}",
                request_fingerprint=f"eval-prior-fingerprint-{case.id.lower()}-{index}",
                now=event_moment,
            )
            active = opened.operation
            active_text = str(prior.content["text"])
            active_turn_id = f"trn-eval-{case.id.lower()}-prior-{index}"
            append_event(
                db,
                keyring,
                conversation_id=EVAL_TIMELINE,
                session_id=session_id,
                turn_id=active_turn_id,
                event_type=USER_MESSAGE,
                content=prior.content,
                operation_id=active.operation_id,
                now=event_moment,
            )
            continue

        if active is None or active_text is None or active_turn_id is None:
            raise ValueError(
                f"{case.id}: operation_result prior turn has no user message"
            )
        content = dict(prior.content)
        state = str(content["state"])
        active.tool = prior.tool
        active.state = state
        active.state_version = 2
        active.updated_at = event_moment
        if state == "waiting_for_clarification":
            question = content.get("clarification")
            if not isinstance(question, str) or not question.strip():
                raise ValueError(
                    f"{case.id}: waiting clarification needs a question"
                )
            active.safe_result = question
            clarification = ClarificationContext(
                original_user_text=active_text,
                question=question,
                source_operation_ids=(active.operation_id,),
            )
            if index != total - 1:
                raise ValueError(
                    f"{case.id}: waiting clarification must be the final prior result"
                )
        elif state == "succeeded":
            safe_result = content.get("record_id", content.get("answer"))
            if safe_result is not None:
                active.safe_result = str(safe_result)
        elif state == "needs_manual_review":
            failure_reason = content.get("failure_reason")
            if failure_reason is not None:
                active.failure_reason = str(failure_reason)
        append_event(
            db,
            keyring,
            conversation_id=EVAL_TIMELINE,
            session_id=session_id,
            turn_id=active_turn_id,
            event_type=OPERATION_RESULT,
            content=content,
            operation_id=active.operation_id,
            now=event_moment,
        )
        if state == "waiting_for_clarification":
            # Production cancels the parked source before interpreting its
            # answer. The exact transcript remains mandatory through the
            # separately bound ClarificationContext above.
            active.state = "cancelled_pre_submit"
            active.state_version = 3
        active = None
        active_text = None
        active_turn_id = None

    if active is not None:
        raise ValueError(f"{case.id}: final prior user message has no result")
    return clarification


def _is_repository_baseline(path: Path) -> bool:
    return path.resolve() == DEFAULT_DATASET.resolve()


def _write_outcomes(path: Path, outcomes: list[EvalOutcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False)
        for outcome in outcomes
    ) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, required=False)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    cases = load_cases(args.dataset)
    problems = lint_cases(
        cases,
        complete_provenance=_is_repository_baseline(args.dataset),
    )
    if problems:
        parser.error("dataset lint failed: " + "; ".join(problems))
    if args.list:
        for case in cases:
            print(f"{case.id:12} {case.source_type:24} {domain_of(case)}")
        return 0
    if args.out is None:
        parser.error("--out is required for a model run so evidence is not discarded")

    if args.case_ids:
        wanted = set(args.case_ids)
        unknown = wanted - {case.id for case in cases}
        if unknown:
            parser.error("unknown case(s): " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case.id in wanted]

    evaluator = "google-adk:" + os.environ.get("GLM_MODEL", "glm-5.2")
    interpreter = ModelInterpreter(glm_gateway_from_env())
    outcomes: list[EvalOutcome] = []
    tools = visible_tools()
    with envelope_factory(tools) as assemble:
        for case in cases:
            started = time.monotonic()
            try:
                result = interpreter.interpret(envelope=assemble(case))
                outcome = score_interpretation(case, result, evaluator=evaluator)
            except InterpreterError as exc:
                outcome = failed_outcome(case, exc, evaluator=evaluator)
            outcomes.append(outcome)
            mark = "PASS" if outcome.passed else "FAIL"
            print(
                f"[{mark}] {case.id:12} {domain_of(case):14} "
                f"{time.monotonic() - started:.2f}s"
            )

    _write_outcomes(args.out, outcomes)
    cells = score_outcomes(
        cases,
        outcomes,
        require_complete=True,
        required_domains=tuple(sorted({domain_of(case) for case in cases})),
    )
    print("\nscores by source and domain:")
    for (source, domain), cell in cells.items():
        print(
            f"  {source:24} {domain:14} "
            f"strict={cell.passed}/{cell.total} ({cell.passed / cell.total:.1%}) "
            f"safety={cell.safety_passed}/{cell.total} "
            f"({cell.safety_passed / cell.total:.1%})"
        )
    print(f"results: {args.out}")
    return 0 if all(outcome.passed for outcome in outcomes) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
