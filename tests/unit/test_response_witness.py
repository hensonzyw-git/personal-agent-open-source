"""Raw response/attempt failures, before implementing the transport guard."""

import asyncio
import json

import httpx
import pytest
from google.genai import types
from google.adk.models.llm_response import LlmResponse

from personal_agent.runtime.response_witness import (
    AttemptBinding, ResponseViolation, ResponseWitnessTransport,
)

ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def payload(arguments='{"value":1}', *, second=None):
    calls = [{"id": "c1", "type": "function", "function": {
        "name": "read", "arguments": arguments,
    }}]
    if second is not None:
        calls.append({"id": "c2", "type": "function", "function": {
            "name": "read", "arguments": second,
        }})
    return {"id": "synthetic-response", "object": "chat.completion", "choices": [{
        "index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": calls},
    }]}


def adk_response(value=1):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(id="c1", name="read", args={"value": value})
    )]))


async def send(body, *, binding=None, url=ENDPOINT, status=200, max_bytes=1024):
    binding = binding or AttemptBinding("request-a", 1, 1)
    seen = []
    async def handler(request):
        seen.append(request.url)
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})
    guard = ResponseWitnessTransport(
        binding=binding, endpoint=ENDPOINT,
        transport=httpx.MockTransport(handler), max_response_bytes=max_bytes,
    )
    async with httpx.AsyncClient(transport=guard) as client:
        await client.post(url, json={"model": "synthetic", "stream": False})
    return guard, seen


@pytest.mark.parametrize("arguments", [
    "{'value':1}", "{value:1}", '{"value":1,"value":2}',
    '{"value":{"nested":1,"nested":2}}', '{"value":NaN}',
    '{"value":Infinity}', '{"value":1e999}', '[]', 'null', '',
])
def test_invalid_raw_arguments_are_refused_before_sdk(arguments):
    with pytest.raises(ResponseViolation):
        asyncio.run(send(json.dumps(payload(arguments)).encode()))


def test_invalid_second_call_refuses_whole_response():
    with pytest.raises(ResponseViolation):
        asyncio.run(send(json.dumps(payload(second="{bad:1}")).encode()))


@pytest.mark.parametrize("body", [b"", b"{}", b"null", b'{"choices":[]}',
    b'{"choices":[],"choices":[]}', b'{"choices":[{},{}]}',
])
def test_empty_multiple_or_duplicate_outer_response_is_refused(body):
    with pytest.raises(ResponseViolation):
        asyncio.run(send(body))


def test_oversized_response_is_refused():
    with pytest.raises(ResponseViolation, match="response_too_large"):
        asyncio.run(send(json.dumps(payload()).encode(), max_bytes=10))


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 402, 403, 429, 500])
def test_provider_error_is_safe_and_not_retried(status):
    with pytest.raises(ResponseViolation) as caught:
        asyncio.run(send(b'{"secret":"synthetic-secret-must-not-leak"}', status=status))
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("url", [
    "https://evil.example/chat/completions", ENDPOINT + "?redirect=evil",
    ENDPOINT + "/other", ENDPOINT.replace("https:", "http:"),
])
def test_host_path_and_query_tamper_refused_before_network(url):
    reached = []
    async def scenario():
        async def handler(request):
            reached.append(True)
            return httpx.Response(200, json=payload())
        guard = ResponseWitnessTransport(binding=AttemptBinding("a", 1, 1),
            endpoint=ENDPOINT, transport=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=guard) as client:
            await client.post(url, json={})
    with pytest.raises(ResponseViolation):
        asyncio.run(scenario())
    assert not reached


def test_witness_is_attempt_bound_and_consumed_once():
    binding = AttemptBinding("a", 1, 1)
    guard, seen = asyncio.run(send(json.dumps(payload()).encode(), binding=binding))
    assert len(seen) == 1
    assert guard.consume(adk_response(), binding=binding)[0].call_id == "c1"
    with pytest.raises(ResponseViolation):
        guard.consume(adk_response(), binding=binding)


@pytest.mark.parametrize("foreign", [
    AttemptBinding("other", 1, 1), AttemptBinding("a", 2, 1),
    AttemptBinding("a", 1, 2), AttemptBinding("a", 1, 1),
])
def test_foreign_request_step_attempt_or_nonce_is_refused(foreign):
    binding = AttemptBinding("a", 1, 1)
    guard, _ = asyncio.run(send(json.dumps(payload()).encode(), binding=binding))
    with pytest.raises(ResponseViolation):
        guard.consume(adk_response(), binding=foreign)
    # A failed match poisons the entire attempt; cannot retry a better match.
    with pytest.raises(ResponseViolation):
        guard.consume(adk_response(), binding=binding)


def test_mutated_call_cannot_consume_receipt():
    binding = AttemptBinding("a", 1, 1)
    guard, _ = asyncio.run(send(json.dumps(payload()).encode(), binding=binding))
    with pytest.raises(ResponseViolation):
        guard.consume(adk_response(2), binding=binding)


def test_second_http_send_on_same_attempt_is_refused():
    async def scenario():
        seen = []
        async def handler(request):
            seen.append(True)
            return httpx.Response(200, json=payload())
        guard = ResponseWitnessTransport(binding=AttemptBinding("a", 1, 1),
            endpoint=ENDPOINT, transport=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=guard) as client:
            await client.post(ENDPOINT, json={})
            with pytest.raises(ResponseViolation):
                await client.post(ENDPOINT, json={})
        assert len(seen) == 1
    asyncio.run(scenario())
