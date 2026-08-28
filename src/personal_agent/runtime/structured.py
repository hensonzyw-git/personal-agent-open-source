"""One narrow, structured model call for the auxiliary `CAP-001` providers.

The Session boundary classifier and the Compactor both need a model to return a
*shape*, not a conversation. They use this seam rather than a second model path:
the transport is the same pinned Google ADK / LiteLlm / Zhipu endpoint the Chat
gateway uses, and the answer is required to arrive as a function call so the
schema is enforced by the provider rather than by parsing free text.

Everything here fails closed. Prose instead of a call, several calls, a
different function name, arguments that are not an object, an unsupported part
payload, a partial or errored response, a timeout: all raise
`StructuredCallError`, and each caller turns that into its own safe outcome --
`continue_session` for the classifier, `provider_failed` for the Compactor.
A `thought` part is skipped (GLM 5.3-class models always reason; the raw
response stays in the audit transcript), but a thought-only response still
fails as "no call".

What this module does *not* do is repair a payload. A returned object is handed
to the caller's own validators untouched, because a provider that repaired its
own output would be deciding the thing it was asked to propose.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from personal_agent.context.budget import HeuristicTokenEstimator
from personal_agent.diagnostics import transcript
from personal_agent.diagnostics.transcript import NullRecorder, Recorder
from personal_agent.runtime.glm_gateway import (
    ZHIPU_API_BASE,
    Generate,
    generate_with_adk,
    require_env,
    validated_api_base,
)
from personal_agent_core.manifest import canonical_json


#: Both auxiliary calls are bounded well inside the 25-second turn budget: they
#: run *around* a user's turn, not inside it, and a slow one must not turn into
#: a slow reply.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0

#: How long to keep waiting after the provider's own timeout should have fired.
#: It is a backstop for an adapter that ignored its deadline, not a second one.
DEADLINE_GRACE_SECONDS: Final[float] = 5.0

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
        input_budget_tokens: int,
        api_base: str = ZHIPU_API_BASE,
        generate: Generate | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        deadline_grace_seconds: float = DEADLINE_GRACE_SECONDS,
        recorder: Recorder | None = None,
        purpose: str = "structured",
    ) -> None:
        if deadline_grace_seconds <= 0:
            raise StructuredCallError("deadline grace must be positive")
        if timeout <= 0 or timeout > 25.0:
            raise StructuredCallError(
                "structured call timeout must be within the 25-second budget"
            )
        if (
            isinstance(input_budget_tokens, bool)
            or not isinstance(input_budget_tokens, int)
            or input_budget_tokens <= 0
        ):
            raise StructuredCallError(
                "structured input budget must be a positive token count"
            )
        self._model = model
        self._api_key = api_key
        self._api_base = validated_api_base(api_base)
        self._timeout = timeout
        self._input_budget_tokens = input_budget_tokens
        self._generate = generate or generate_with_adk
        self._recorder = recorder or NullRecorder()
        self._purpose = purpose
        # Python cannot kill a thread whose provider ignored its own timeout, so
        # this bounds the *wait*, not the call, and keeps at most one abandoned
        # worker per client. The classifier runs in the request path before the
        # message is anchored: without this, one adapter that never returns
        # holds an executor thread and the user's message with it, and repeats
        # of that exhaust the pool. The Compactor has the same guard for the
        # same reason; having it in only one of the two was the asymmetry that
        # mattered, since the guarded one runs in the background.
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._deadline_grace = deadline_grace_seconds

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
        estimated_input_tokens = HeuristicTokenEstimator().estimate(
            canonical_json(
                {
                    "system": request.system,
                    "messages": [
                        {"role": "user", "content": request.user_content}
                    ],
                    "declarations": [declaration],
                    "tool_config": {
                        "mode": "ANY",
                        "allowed_function_names": [request.function_name],
                    },
                }
            )
        )
        if estimated_input_tokens > self._input_budget_tokens:
            raise StructuredCallError(
                "structured model input exceeded the configured budget"
            )
        self._recorder.record(
            transcript.MODEL_REQUEST,
            {
                "purpose": self._purpose,
                "model": self._model,
                "api_base": self._api_base,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "timeout_seconds": self._timeout,
                "system_instruction": request.system,
                "messages": [{"role": "user", "content": request.user_content}],
                "declarations": [declaration],
                "allowed_function_names": [request.function_name],
                "estimated_input_tokens": estimated_input_tokens,
            },
        )
        started = time.monotonic()
        try:
            response = self._generate_bounded(request, declaration)
        except StructuredCallError as exc:
            self._record_failure("provider_call", exc, started)
            raise
        self._recorder.record(
            transcript.MODEL_RESPONSE,
            {
                "purpose": self._purpose,
                "model": self._model,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "raw": response,
            },
        )
        try:
            return _parse(response, expected=request.function_name)
        except StructuredCallError as exc:
            self._record_failure("response_validation", exc, started)
            raise

    def _record_failure(
        self, phase: str, error: StructuredCallError, started: float
    ) -> None:
        self._recorder.record(
            transcript.MODEL_FAILURE,
            {
                "purpose": self._purpose,
                "phase": phase,
                "model": self._model,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )

    def _generate_bounded(
        self, request: StructuredRequest, declaration: dict[str, Any]
    ) -> Any:
        """Call the provider behind a hard wall-clock limit."""
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcome.put((True, self._call_generator(request, declaration)))
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                outcome.put((False, exc))

        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                # A previous call is still out there. Starting another would
                # stack abandoned workers behind a provider that is already
                # not returning.
                raise StructuredCallError(
                    "a previous structured call has not returned"
                )
            worker = threading.Thread(
                target=invoke,
                name=f"personal-agent-structured-{id(self):x}",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        # A margin over the provider's own timeout: this is the backstop for an
        # adapter that ignored it, not a second, tighter deadline.
        worker.join(self._timeout + self._deadline_grace)
        if worker.is_alive():
            raise StructuredCallError("structured model call exceeded its deadline")
        try:
            succeeded, value = outcome.get_nowait()
        except queue.Empty as exc:  # pragma: no cover - worker/result disagree
            raise StructuredCallError(
                "structured model call produced no result"
            ) from exc
        if not succeeded:
            if isinstance(value, StructuredCallError):
                raise value
            raise StructuredCallError(
                f"structured model call failed: {type(value).__name__}"
            ) from value
        return value

    def _call_generator(
        self, request: StructuredRequest, declaration: dict[str, Any]
    ) -> Any:
        try:
            return self._generate(
                model=self._model,
                api_key=self._api_key,
                api_base=self._api_base,
                system=request.system,
                messages=[{"role": "user", "content": request.user_content}],
                declarations=[declaration],
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                timeout=self._timeout,
                allowed_function_names=[request.function_name],
            )
        except StructuredCallError:
            raise
        except Exception as exc:  # noqa: BLE001 - every provider failure closes
            raise StructuredCallError(
                f"structured model call failed: {type(exc).__name__}"
            ) from exc


#: The classifier may run on a smaller, faster model than Chat. The 2026-07-29
#: live run measured 8.3s, 12.2s and 16.7s for the flagship, with one 20s
#: timeout, on a call that sits in the request path before the message is
#: anchored -- unusable as latency, however correct the answers were. This is a
#: closed-schema judgement, not a conversation, so a deployment should name a
#: fast model here. It is **not** given a default of its own: inventing a model
#: id that may not exist would fail at runtime, so an unset value keeps today's
#: behaviour and the operator opts in.
#:
#: Read-only check on 2026-08-07: the ECS deployment sets neither this nor
#: ``GLM_MODEL``, so the classifier still falls back to the Chat model and both
#: run on the same one. The isolation exists in code and not yet in production.
#: Do not read the paragraph above as a description of what is deployed.
CLASSIFIER_MODEL_ENV: Final[str] = "GLM_CLASSIFIER_MODEL"

#: The in-path deadline. Shorter than the Compactor's on purpose: a classifier
#: that does not answer in time continues the current Session (§6.1 step 9),
#: which costs some irrelevance, while a slow one costs the user every message.
CLASSIFIER_TIMEOUT_SECONDS: Final[float] = 8.0


def structured_client_from_env(
    *,
    input_budget_tokens: int,
    generate: Generate | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    model_env: str = "GLM_MODEL",
    recorder: Recorder | None = None,
    purpose: str = "structured",
) -> StructuredModelClient:
    """Build the production client from an already-loaded environment.

    `model_env` lets one deployment run the two auxiliary calls on different
    models. It falls back to `GLM_MODEL`, so an unset override changes nothing.
    """
    import os

    model = os.environ.get(model_env) or os.environ.get("GLM_MODEL", "glm-5.3-flash")
    return StructuredModelClient(
        model=f"openai/{model}",
        api_key=require_env("ZAI_API_KEY"),
        input_budget_tokens=input_budget_tokens,
        api_base=os.environ.get("GLM_OPENAI_BASE_URL", ZHIPU_API_BASE),
        generate=generate,
        timeout=timeout,
        recorder=recorder,
        purpose=purpose,
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
            # GLM 5.3-class models always reason (same policy as the Chat
            # gateway): the thought part is private process, not the structured
            # answer. Skip it; a thought-only response still fails below.
            continue
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
