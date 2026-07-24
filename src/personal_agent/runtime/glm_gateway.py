"""Google ADK-first GLM model gateway.

`DEV-027`. Google ADK is the formal runtime adapter; its ``LiteLlm`` model
adapter is only the transport to Zhipu's OpenAI-compatible endpoint. This module
asks ADK for one model turn and converts the result into a framework-neutral
proposal. It never runs an ADK tool and never treats model prose as evidence of
an external write.

Two private, side-effect-free function declarations make non-write outcomes
structured: one parks for clarification and one reports the frozen batch gate.
They are interpreted locally and never enter the governed business-tool bridge.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.model_gateway import (
    ClarificationContext,
    ModelGatewayError,
    ModelProposal,
    ProposedAnswer,
    ProposedClarification,
    ProposedFailure,
    ProposedToolCall,
    tool_declarations,
)
from personal_agent_core.errors import ErrorCode


_ZHIPU_API_BASE = "https://open.bigmodel.cn/api/paas/v4/"
_ASK_CLARIFICATION = "agent.ask_clarification"
_FAIL_BATCH = "agent.fail_batch_unavailable"
_SUPPORTED_PART_FIELDS = frozenset({"function_call", "text", "thought"})

# Injected only by offline tests. Production always uses `_generate_with_adk`.
Generate = Callable[..., Any]


class GlmGateway:
    """A synchronous seam around ADK's async model adapter.

    The Client API runs this seam in its operation worker thread. Keeping the
    protocol synchronous preserves the framework-neutral orchestrator while the
    HTTP event loop remains unblocked.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        api_base: str = _ZHIPU_API_BASE,
        generate: Generate | None = None,
        timeout: float = 25.0,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> None:
        if timeout <= 0 or timeout > 25.0:
            raise ModelGatewayError("GLM timeout must be within the 25-second budget")
        self._model = model
        self._api_key = api_key
        self._api_base = _validated_api_base(api_base)
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._generate = generate or _generate_with_adk

    def propose(
        self,
        *,
        system: str,
        user_text: str,
        tools: list[VisibleTool],
        clarification: ClarificationContext | None = None,
    ) -> ModelProposal:
        messages = _messages(user_text, clarification)
        declarations = tool_declarations(tools) + _internal_declarations()
        try:
            response = self._generate(
                model=self._model,
                api_key=self._api_key,
                api_base=self._api_base,
                system=system,
                messages=messages,
                declarations=declarations,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                timeout=self._timeout,
            )
        except ModelGatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - all provider failures fail closed
            raise ModelGatewayError(
                f"model call failed: {type(exc).__name__}"
            ) from exc
        return _parse_adk_proposal(response)


def glm_gateway_from_env(*, generate: Generate | None = None) -> GlmGateway:
    """Build the production gateway from an already-loaded environment.

    A model credential may only be sent to Zhipu's pinned HTTPS API path. The
    environment variable is retained for deploy-time visibility, but changing it
    to another host or path is rejected before any network call.
    """

    api_key = _require_env("ZAI_API_KEY")
    api_base = os.environ.get("GLM_OPENAI_BASE_URL", _ZHIPU_API_BASE)
    model = os.environ.get("GLM_MODEL", "glm-5.2")
    return GlmGateway(
        model=f"openai/{model}",
        api_key=api_key,
        api_base=api_base,
        generate=generate,
    )


def _messages(
    user_text: str, clarification: ClarificationContext | None
) -> list[dict[str, str]]:
    if clarification is None:
        return [{"role": "user", "content": user_text}]
    return [
        {"role": "user", "content": clarification.original_user_text},
        {"role": "model", "content": clarification.question},
        {"role": "user", "content": user_text},
    ]


def _internal_declarations() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _ASK_CLARIFICATION,
                "description": "缺少执行所需信息时，只提出一个澄清问题。",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question"],
                    "properties": {
                        "question": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": _FAIL_BATCH,
                "description": (
                    "消息包含两笔及以上记录且批量原子性能力尚未启用时，"
                    "以零写入方式拒绝。"
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
            },
        },
    ]


def _parse_adk_proposal(response: Any) -> ModelProposal:
    error_code = getattr(response, "error_code", None)
    if error_code:
        raise ModelGatewayError("ADK model response reported an error")
    if getattr(response, "partial", False):
        raise ModelGatewayError("ADK model response was partial")
    if getattr(response, "interrupted", False):
        raise ModelGatewayError("ADK model response was interrupted")
    content = getattr(response, "content", None)
    parts = getattr(content, "parts", None)
    if not isinstance(parts, list) or not parts:
        raise ModelGatewayError("ADK model response had no content")

    calls: list[Any] = []
    text_parts: list[str] = []
    for part in parts:
        if getattr(part, "thought", False):
            raise ModelGatewayError(
                "ADK model response contained unsupported thought content"
            )
        unsupported = _unsupported_part_fields(part)
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ModelGatewayError(
                f"ADK model response contained unsupported content: {names}"
            )
        call = getattr(part, "function_call", None)
        if call is not None:
            calls.append(call)
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip() and not getattr(part, "thought", False):
            text_parts.append(text)

    if calls:
        if len(calls) != 1:
            raise ModelGatewayError("model proposed multiple tool calls")
        if text_parts:
            raise ModelGatewayError("model mixed a tool call with a direct answer")
        call = calls[0]
        name = getattr(call, "name", None)
        if not isinstance(name, str) or not name:
            raise ModelGatewayError("tool call had no name")
        arguments = _parse_arguments(getattr(call, "args", None))
        if name == _ASK_CLARIFICATION:
            question = arguments.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ModelGatewayError("clarification had no question")
            return ProposedClarification(question=question.strip())
        if name == _FAIL_BATCH:
            if arguments:
                raise ModelGatewayError("batch failure tool had unexpected arguments")
            return ProposedFailure(reason=ErrorCode.BATCH_ATOMICITY_UNAVAILABLE.value)
        return ProposedToolCall(tool=name, arguments=arguments)

    answer = "".join(text_parts).strip()
    if not answer:
        raise ModelGatewayError("model response was blank")
    return ProposedAnswer(text=answer)


def _unsupported_part_fields(part: Any) -> set[str]:
    """Return populated ADK Part fields whose semantics we do not support.

    Google GenAI adds new ``Part`` payload variants over time. Inspecting the
    concrete object's fields makes that evolution fail closed: a new content
    type cannot be silently discarded while an adjacent write call proceeds.
    """

    try:
        fields = vars(part)
    except TypeError as exc:
        raise ModelGatewayError("ADK model response part was not inspectable") from exc
    return {
        name
        for name, value in fields.items()
        if name not in _SUPPORTED_PART_FIELDS and value is not None
    }


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ModelGatewayError("tool call arguments were not JSON")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelGatewayError("tool call arguments were not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelGatewayError("tool call arguments were not a JSON object")
    return parsed


def _validated_api_base(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ModelGatewayError("GLM_OPENAI_BASE_URL is invalid") from exc
    valid_path = parsed.path.rstrip("/") == "/api/paas/v4"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.bigmodel.cn"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not valid_path
        or parsed.query
        or parsed.fragment
    ):
        raise ModelGatewayError(
            "GLM_OPENAI_BASE_URL must be Zhipu's pinned HTTPS API endpoint"
        )
    return _ZHIPU_API_BASE


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ModelGatewayError(f"{name} is not set in the environment")
    return value


def _generate_with_adk(
    *,
    model: str,
    api_key: str,
    api_base: str,
    system: str,
    messages: list[dict[str, str]],
    declarations: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> Any:
    """Generate one non-streaming turn through Google ADK's model contract."""

    # Lazy imports keep the base package importable when the optional runtime is
    # not installed. Production composition must install the ``adk`` extra.
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    functions = [
        types.FunctionDeclaration(
            name=item["function"]["name"],
            description=item["function"]["description"],
            parameters_json_schema=item["function"]["parameters"],
        )
        for item in declarations
    ]
    llm = LiteLlm(
        model=model,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        num_retries=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    request = LlmRequest(
        contents=[
            types.Content(
                role=message["role"],
                parts=[types.Part(text=message["content"])],
            )
            for message in messages
        ],
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            tools=[types.Tool(function_declarations=functions)],
        ),
    )

    async def one_response() -> Any:
        responses = [
            response
            async for response in llm.generate_content_async(request, stream=False)
        ]
        if len(responses) != 1:
            raise ModelGatewayError(
                f"ADK returned {len(responses)} non-streaming responses"
            )
        return responses[0]

    return asyncio.run(one_response())
