"""DEV-027: ADK-first GLM proposal parsing and fail-closed boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import types

from personal_agent.api.orchestrator import InterpreterError
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.glm_gateway import (
    GlmGateway,
    _generate_with_adk,
    glm_gateway_from_env,
)
from personal_agent.runtime.interpreter import ModelInterpreter
from personal_agent.runtime.model_gateway import (
    ClarificationContext,
    ModelGatewayError,
    ProposedAnswer,
    ProposedClarification,
    ProposedFailure,
    ProposedToolCall,
)
from personal_agent_core.errors import ErrorCode


_PINNED = "https://open.bigmodel.cn/api/paas/v4/"
_EXPENSE = VisibleTool(
    alias="finance.log_expense",
    description="记一笔支出",
    input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
    risk_level="R2",
    required_scopes=("finance.write",),
)


def _response(*parts, error_code=None):
    return SimpleNamespace(
        error_code=error_code,
        content=SimpleNamespace(parts=list(parts)) if parts else None,
    )


def _text(value, *, thought=False):
    return SimpleNamespace(text=value, thought=thought, function_call=None)


def _call(name, args):
    return SimpleNamespace(
        text=None,
        thought=False,
        function_call=SimpleNamespace(name=name, args=args),
    )


def _call_with_unsupported_content(name, args):
    return types.Part(
        function_call=types.FunctionCall(name=name, args=args),
        inline_data=types.Blob(
            mime_type="application/octet-stream",
            data=b"unsupported",
        ),
    )


def _gateway(response=None, *, raises=None):
    def generate(**kwargs):
        generate.kwargs = kwargs
        if raises is not None:
            raise raises
        return response

    generate.kwargs = None
    return (
        GlmGateway(
            model="openai/glm-5.2",
            api_key="k",
            api_base=_PINNED,
            generate=generate,
        ),
        generate,
    )


def _propose(gateway, *, clarification=None):
    return gateway.propose(
        system="SYS",
        user_text="午饭 45 个人",
        tools=[_EXPENSE],
        clarification=clarification,
    )


def test_a_tool_call_response_becomes_one_proposed_tool_call() -> None:
    gateway, _ = _gateway(
        _response(
            _call(
                "finance.log_expense",
                {"name": "午饭", "input_amount": "45"},
            )
        )
    )
    proposal = _propose(gateway)
    assert proposal == ProposedToolCall(
        "finance.log_expense",
        {"name": "午饭", "input_amount": "45"},
    )


def test_a_plain_answer_becomes_a_proposed_answer() -> None:
    gateway, _ = _gateway(_response(_text("你好")))
    assert _propose(gateway) == ProposedAnswer("你好")


def test_the_request_is_bounded_and_declares_business_and_internal_tools() -> None:
    gateway, generate = _gateway(_response(_text("ok")))
    _propose(gateway)
    kwargs = generate.kwargs
    assert kwargs["model"] == "openai/glm-5.2"
    assert kwargs["api_base"] == _PINNED
    assert kwargs["timeout"] == 25.0
    assert kwargs["messages"] == [{"role": "user", "content": "午饭 45 个人"}]
    names = [item["function"]["name"] for item in kwargs["declarations"]]
    assert names == [
        "finance.log_expense",
        "agent.ask_clarification",
        "agent.fail_batch_unavailable",
    ]


def test_only_the_controlled_unresolved_turn_is_sent_for_clarification() -> None:
    gateway, generate = _gateway(_response(_text("ok")))
    context = ClarificationContext("午饭 45", "个人还是家庭支出？")
    _propose(gateway, clarification=context)
    assert generate.kwargs["messages"] == [
        {"role": "user", "content": "午饭 45"},
        {"role": "model", "content": "个人还是家庭支出？"},
        {"role": "user", "content": "午饭 45 个人"},
    ]


def test_structured_clarification_and_batch_gate_are_not_direct_answers() -> None:
    clarification, _ = _gateway(
        _response(_call("agent.ask_clarification", {"question": "个人还是家庭？"}))
    )
    assert _propose(clarification) == ProposedClarification("个人还是家庭？")

    batch, _ = _gateway(_response(_call("agent.fail_batch_unavailable", {})))
    assert _propose(batch) == ProposedFailure(
        ErrorCode.BATCH_ATOMICITY_UNAVAILABLE.value
    )


@pytest.mark.parametrize(
    "response",
    [
        _response(),
        _response(_text("   ")),
        _response(_call("finance.log_expense", [])),
        _response(
            _call("finance.log_expense", {}),
            _call("finance.log_income", {}),
        ),
        _response(_text("已记录"), _call("finance.log_expense", {})),
        _response(_text("ok"), error_code="MAX_TOKENS"),
    ],
)
def test_malformed_or_ambiguous_responses_fail_closed(response) -> None:
    gateway, _ = _gateway(response)
    with pytest.raises(ModelGatewayError):
        _propose(gateway)


def test_a_tool_call_with_an_unsupported_part_payload_fails_closed() -> None:
    gateway, _ = _gateway(
        _response(
            _call_with_unsupported_content(
                "finance.log_expense",
                {"name": "午饭", "input_amount": "45"},
            )
        )
    )
    with pytest.raises(ModelGatewayError, match="unsupported content"):
        _propose(gateway)


def test_a_partial_response_fails_closed_even_if_it_contains_a_tool_call() -> None:
    response = _response(_call("finance.log_expense", {"name": "午饭"}))
    response.partial = True
    gateway, _ = _gateway(response)
    with pytest.raises(ModelGatewayError, match="partial"):
        _propose(gateway)


def test_thought_content_is_not_silently_dropped_beside_a_tool_call() -> None:
    gateway, _ = _gateway(
        _response(
            _text("hidden reasoning", thought=True),
            _call("finance.log_expense", {"name": "午饭"}),
        )
    )
    with pytest.raises(ModelGatewayError, match="thought content"):
        _propose(gateway)


def test_transport_failure_is_a_gateway_error() -> None:
    gateway, _ = _gateway(raises=RuntimeError("connection reset"))
    with pytest.raises(ModelGatewayError):
        _propose(gateway)


def test_from_env_requires_a_key_and_rejects_a_credential_exfiltration_host(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with pytest.raises(ModelGatewayError):
        glm_gateway_from_env()

    monkeypatch.setenv("ZAI_API_KEY", "secret")
    monkeypatch.setenv("GLM_OPENAI_BASE_URL", "https://attacker.invalid/v1")
    with pytest.raises(ModelGatewayError):
        glm_gateway_from_env()


def test_model_timeout_cannot_exceed_the_design_budget() -> None:
    with pytest.raises(ModelGatewayError):
        GlmGateway(
            model="openai/glm-5.2",
            api_key="k",
            api_base=_PINNED,
            generate=lambda **kwargs: None,
            timeout=25.1,
        )


def test_the_interpreter_translates_a_gateway_error() -> None:
    gateway, _ = _gateway(raises=RuntimeError("down"))
    interp = ModelInterpreter(gateway, tools=[_EXPENSE], system="SYS")
    with pytest.raises(InterpreterError):
        interp.interpret(text="午饭 45", conversation_id="c1")


def test_production_generator_uses_the_adk_model_contract(monkeypatch) -> None:
    captured = {}
    expected = _response(_text("ok"))

    class FakeLiteLlm:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def generate_content_async(self, request, stream=False):
            captured["request"] = request
            captured["stream"] = stream
            yield expected

    monkeypatch.setattr(
        "google.adk.models.lite_llm.LiteLlm",
        FakeLiteLlm,
    )
    actual = _generate_with_adk(
        model="openai/glm-5.2",
        api_key="secret",
        api_base=_PINNED,
        system="SYS",
        messages=[{"role": "user", "content": "hi"}],
        declarations=[
            {
                "type": "function",
                "function": {
                    "name": "meta.capabilities",
                    "description": "能力",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        temperature=0.1,
        max_tokens=512,
        timeout=25.0,
    )
    assert actual is expected
    assert captured["init"]["api_base"] == _PINNED
    assert captured["init"]["num_retries"] == 0
    assert captured["stream"] is False
    request = captured["request"]
    assert request.config.system_instruction == "SYS"
    assert request.contents[0].role == "user"
    assert (
        request.config.tools[0].function_declarations[0].name
        == "meta.capabilities"
    )
