"""One narrow, structured model call for the auxiliary `CAP-001` providers.

The Session boundary classifier and the Compactor both need a model to return a
*shape*, not a conversation. They use this seam rather than a second model path:
the transport is the same pinned Google ADK / LiteLlm / Zhipu endpoint the Chat
gateway uses, and the answer is required to arrive as a function call so the
schema is enforced by the provider rather than by parsing free text.

Everything here fails closed. Prose instead of a call, several calls, a
different function name, arguments that are not an object, a `thought` part, an
unsupported part payload, a partial or errored response, a timeout: all raise
`StructuredCallError`, and each caller turns that into its own safe outcome --
`continue_session` for the classifier, `provider_failed` for the Compactor.

What this module does *not* do is repair a payload. A returned object is handed
to the caller's own validators untouched, because a provider that repaired its
own output would be deciding the thing it was asked to propose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from personal_agent.runtime.glm_gateway import (
    ZHIPU_API_BASE,
    Generate,
    generate_with_adk,
    require_env,
    validated_api_base,
)


#: Both auxiliary calls are bounded well inside the 25-second turn budget: they
#: run *around* a user's turn, not inside it, and a slow one must not turn into
#: a slow reply.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0

_SUPPORTED_PART_FIELDS: Final[frozenset[str]] = frozenset(
    {"function_call", "text", "thought"}
)


class StructuredCallError(RuntimeError):
    """The structured turn could not be obtained or understood."""


@dataclass(frozen=True)
class StructuredRequest:
    """One call: an instruction, one untrusted data block, one output shape."""

    system: str
    #: Already framed as untrusted data by the caller. This module never adds
    #: content of its own and never promotes any of it to the instruction.
    user_content: str
    function_name: str
    parameters_schema: dict[str, Any]
    max_tokens: int = 1024
    temperature: float = 0.0


class StructuredModelClient:
    """Synchronous, single-function structured call over the pinned endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        api_base: str = ZHIPU_API_BASE,
        generate: Generate | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout <= 0 or timeout > 25.0:
            raise StructuredCallError(
                "structured call timeout must be within the 25-second budget"
            )
        self._model = model
        self._api_key = api_key
        self._api_base = validated_api_base(api_base)
        self._timeout = timeout
        self._generate = generate or generate_with_adk

    def call(self, request: StructuredRequest) -> dict[str, Any]:
        declaration = {
            "type": "function",
            "function": {
                "name": request.function_name,
                "description": (
                    "以这个函数返回本次判断的结构化结果；不要用普通文字回答。"
                ),
                "parameters": request.parameters_schema,
            },
        }
        try:
            response = self._generate(
                model=self._model,
                api_key=self._api_key,
                api_base=self._api_base,
                system=request.system,
                messages=[{"role": "user", "content": request.user_content}],
                declarations=[declaration],
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                timeout=self._timeout,
            )
        except StructuredCallError:
            raise
        except Exception as exc:  # noqa: BLE001 - every provider failure closes
            raise StructuredCallError(
                f"structured model call failed: {type(exc).__name__}"
            ) from exc
        return _parse(response, expected=request.function_name)


def structured_client_from_env(
    *, generate: Generate | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> StructuredModelClient:
    """Build the production client from an already-loaded environment."""
    import os

    return StructuredModelClient(
        model=f"openai/{os.environ.get('GLM_MODEL', 'glm-5.2')}",
        api_key=require_env("ZAI_API_KEY"),
        api_base=os.environ.get("GLM_OPENAI_BASE_URL", ZHIPU_API_BASE),
        generate=generate,
        timeout=timeout,
    )


def _parse(response: Any, *, expected: str) -> dict[str, Any]:
    if getattr(response, "error_code", None):
        raise StructuredCallError("structured response reported an error")
    if getattr(response, "partial", False):
        raise StructuredCallError("structured response was partial")
    if getattr(response, "interrupted", False):
        raise StructuredCallError("structured response was interrupted")
    parts = getattr(getattr(response, "content", None), "parts", None)
    if not isinstance(parts, list) or not parts:
        raise StructuredCallError("structured response had no content")

    calls: list[Any] = []
    for part in parts:
        if getattr(part, "thought", False):
            raise StructuredCallError(
                "structured response contained thought content"
            )
        unsupported = _unsupported_fields(part)
        if unsupported:
            raise StructuredCallError(
                "structured response contained unsupported content: "
                + ", ".join(sorted(unsupported))
            )
        call = getattr(part, "function_call", None)
        if call is not None:
            calls.append(call)
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            # Prose beside a structured answer is not a second opinion to pick
            # from; it means the contract was not followed.
            raise StructuredCallError("structured response contained prose")

    if len(calls) != 1:
        raise StructuredCallError(
            f"structured response carried {len(calls)} function calls"
        )
    call = calls[0]
    if getattr(call, "name", None) != expected:
        raise StructuredCallError("structured response called another function")
    arguments = getattr(call, "args", None)
    if not isinstance(arguments, dict):
        raise StructuredCallError("structured arguments were not an object")
    return dict(arguments)


def _unsupported_fields(part: Any) -> set[str]:
    try:
        fields = vars(part)
    except TypeError as exc:
        raise StructuredCallError(
            "structured response part was not inspectable"
        ) from exc
    return {
        name
        for name, value in fields.items()
        if name not in _SUPPORTED_PART_FIELDS and value is not None
    }


#: The clock a caller may inject; kept here so both providers share one shape.
Clock = Callable[[], Any]
