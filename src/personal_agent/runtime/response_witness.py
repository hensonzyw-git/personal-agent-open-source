"""Strict raw-response gate before SDK normalization, scoped to one attempt.

No response bodies, credentials, or argument values are retained in a receipt.
This is a transport/provenance boundary, not tool authorization or a budget store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
from openai.types.completion_usage import CompletionUsage
from pydantic import ValidationError


class ResponseViolation(ValueError):
    """Safe enumerated boundary failure; never include raw provider messages."""


@dataclass(frozen=True)
class AttemptBinding:
    request_id: str
    step_no: int
    attempt_no: int
    nonce: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        if not self.request_id or self.step_no < 1 or self.attempt_no < 1 or not self.nonce:
            raise ResponseViolation("invalid_attempt_binding")


@dataclass(frozen=True)
class CallStamp:
    call_id: str
    name: str
    args_hash: str


@dataclass(frozen=True)
class ResponseReceipt:
    binding: AttemptBinding
    request_hash: str
    calls: tuple[CallStamp, ...]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResponseViolation("duplicate_json_key")
        result[key] = value
    return result


def _constant(value):
    raise ResponseViolation("nonfinite_json_number")


def strict_json(raw: str | bytes):
    try:
        parsed = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        # json.loads accepts finite-looking overflow, e.g. 1e999 -> inf.
        json.dumps(parsed, allow_nan=False)
        return parsed
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ResponseViolation("invalid_json") from None


def arguments_hash(args: dict) -> str:
    if not isinstance(args, dict):
        raise ResponseViolation("arguments_not_object")
    try:
        serialized = json.dumps(args, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ResponseViolation("invalid_arguments") from None
    return hashlib.sha256(serialized).hexdigest()


def _raw_calls(body: bytes) -> tuple[CallStamp, ...]:
    data = strict_json(body)
    if not isinstance(data, dict) or data.get("error") is not None:
        raise ResponseViolation("invalid_response")
    # The SDK constructs responses without validating usage first. Serializing
    # a malformed counter can therefore warn with its raw input_value before
    # our outer exception sanitizer runs. Validate the locked SDK's nested
    # usage schema strictly, without coercing/replacing the original payload.
    # Missing/null usage and provider extension fields remain supported.
    if data.get("usage") is not None:
        try:
            CompletionUsage.model_validate(data["usage"], strict=True)
        except ValidationError:
            raise ResponseViolation("invalid_response_usage") from None
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ResponseViolation("response_count")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("index") != 0:
        raise ResponseViolation("invalid_choice")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ResponseViolation("invalid_message")
    if message.get("function_call") is not None:
        raise ResponseViolation("legacy_function_call")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ResponseViolation("unsupported_content")
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise ResponseViolation("invalid_tool_calls")
    if choice.get("finish_reason") != ("tool_calls" if calls else "stop"):
        raise ResponseViolation("incomplete_response")
    if not calls and not (content and content.strip()):
        raise ResponseViolation("empty_response")
    if calls and content and content.strip():
        raise ResponseViolation("mixed_tool_prose")
    stamps = []
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ResponseViolation("unsupported_call")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            raise ResponseViolation("invalid_call_id")
        seen.add(call_id)
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
            raise ResponseViolation("invalid_function")
        raw = function.get("arguments")
        if not isinstance(raw, str):
            raise ResponseViolation("arguments_not_json_string")
        args = strict_json(raw)
        stamps.append(CallStamp(call_id, function["name"], arguments_hash(args)))
    return tuple(stamps)


def response_calls(response) -> tuple[CallStamp, ...]:
    if response.partial or response.interrupted or response.error_code:
        raise ResponseViolation("incomplete_sdk_response")
    if not response.content or not response.content.parts:
        raise ResponseViolation("empty_sdk_response")
    stamps = []
    for part in response.content.parts:
        active = {k for k, v in part.model_dump(exclude_none=True).items() if v is not None}
        if active - {"text", "thought", "thought_signature", "function_call"}:
            raise ResponseViolation("unsupported_sdk_part")
        if part.function_call:
            call = part.function_call
            if set(call.model_dump(exclude_none=True)) - {"id", "name", "args"}:
                raise ResponseViolation("unsupported_sdk_call_fields")
            stamps.append(CallStamp(call.id, call.name, arguments_hash(call.args)))
    return tuple(stamps)


class ResponseWitnessTransport(httpx.AsyncBaseTransport):
    """One HTTP attempt, one immutable receipt, one callback consumption.

    The owner must give each model attempt a fresh instance. Closing HTTP resources
    does not erase a completed receipt (the callback runs after SDK cleanup);
    cancellation/error explicitly invalidates it. This object is never shared.
    """

    def __init__(self, *, binding: AttemptBinding, endpoint: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 max_response_bytes: int = 512 * 1024):
        self.binding = binding
        self.endpoint = httpx.URL(endpoint)
        if (self.endpoint.scheme != "https" or self.endpoint.username or
                self.endpoint.password or self.endpoint.query or self.endpoint.fragment):
            raise ResponseViolation("invalid_endpoint")
        if max_response_bytes < 1:
            raise ResponseViolation("invalid_response_limit")
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self._limit = max_response_bytes
        self._started = False
        self._spent = False
        self._receipt: ResponseReceipt | None = None
        self.failure: str | None = None

    def invalidate(self, code="attempt_invalidated"):
        self._spent = True
        self._receipt = None
        self.failure = code

    async def handle_async_request(self, request):
        try:
            if self._started or self._spent:
                raise ResponseViolation("attempt_reused")
            self._started = True
            if request.method != "POST" or request.url != self.endpoint:
                raise ResponseViolation("endpoint_mismatch")
            body = request.content
            outgoing = strict_json(body)
            if not isinstance(outgoing, dict) or outgoing.get("stream", False) is not False:
                raise ResponseViolation("unsupported_request")
            # Request the bounded, uncompressed JSON wire shape we can verify.
            request.headers["accept-encoding"] = "identity"
            result = await self._transport.handle_async_request(request)
            try:
                if result.status_code != 200:
                    raise ResponseViolation(f"provider_http_{result.status_code}")
                if result.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ResponseViolation("encoded_response")
                buffer = bytearray()
                async for chunk in result.aiter_bytes():
                    if len(buffer) + len(chunk) > self._limit:
                        raise ResponseViolation("response_too_large")
                    buffer.extend(chunk)
                raw = bytes(buffer)
                stamps = _raw_calls(raw)
                self._receipt = ResponseReceipt(self.binding, hashlib.sha256(body).hexdigest(), stamps)
                headers = dict(result.headers)
                headers.pop("content-length", None)
                return httpx.Response(200, headers=headers, content=raw, request=request)
            finally:
                await result.aclose()
        except BaseException as exc:
            self.invalidate(str(exc) if isinstance(exc, ResponseViolation) else "transport_failed")
            raise

    def check(self, response, *, binding: AttemptBinding) -> tuple[CallStamp, ...]:
        try:
            receipt = self._receipt
            if self._spent or receipt is None or binding != receipt.binding:
                raise ResponseViolation("witness_binding_mismatch")
            if response_calls(response) != receipt.calls:
                raise ResponseViolation("witness_calls_mismatch")
            return receipt.calls
        except BaseException:
            self.invalidate("witness_mismatch")
            raise

    def consume(self, response, *, binding: AttemptBinding) -> tuple[CallStamp, ...]:
        calls = self.check(response, binding=binding)
        self._spent = True
        self._receipt = None
        return calls

    async def aclose(self):
        await self._transport.aclose()
