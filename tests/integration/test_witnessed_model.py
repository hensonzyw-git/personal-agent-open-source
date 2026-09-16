"""Real SDK/ADK transport, with HTTP responses and failure injection offline."""

import asyncio

import httpx
import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from personal_agent.runtime.response_witness import AttemptBinding, ResponseViolation
from personal_agent.runtime.witnessed_model import WitnessedLiteLlm

from test_adk_locked_behavior import Harness


def wire(*, args='{"value":1}', calls=None):
    return {"id": "synthetic-id", "created": 1, "object": "chat.completion",
        "model": "synthetic", "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": calls if calls is not None else [{"id": "c1", "type": "function",
                "function": {"name": "finish", "arguments": args}}],
        }}]}


class CheckedHarness(Harness):
    def __init__(self, model, binding):
        super().__init__([], model=model)
        self.binding = binding

    async def after_model(self, callback_context, llm_response):
        self.model.verify_response(llm_response, binding=self.binding)
        await super().after_model(callback_context, llm_response)


@pytest.mark.parametrize("mutation", ["multiple", "empty", "modified", "removed", "added"])
def test_sdk_response_changes_are_rejected_before_any_tool(monkeypatch, mutation):
    from google.adk.models.lite_llm import LiteLlm
    original = LiteLlm.generate_content_async
    async def injected(self, request, stream=False):
        async for result in original(self, request, stream=stream):
            if mutation == "empty":
                return
            if mutation == "modified":
                result.content.parts[0].function_call.args = {"value": 99}
            if mutation == "removed":
                result.content.parts = [types.Part(text="pretended success")]
            if mutation == "added":
                result.content.parts.append(types.Part(function_call=types.FunctionCall(
                    id="extra", name="write_handoff", args={})))
            yield result
            if mutation == "multiple":
                yield result.model_copy(deep=True)
    monkeypatch.setattr(LiteLlm, "generate_content_async", injected)
    sent = []
    async def http(request):
        sent.append(request)
        return httpx.Response(200, json=wire())
    binding = AttemptBinding("request", 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http)), binding)
    with pytest.raises(ResponseViolation):
        asyncio.run(h.run())
    assert len(sent) == 1
    assert not any(x.startswith("start:") for x in h.trace)
    assert h.cleaned


def test_bad_second_raw_call_never_delivers_first_call():
    calls = [
        {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "write_handoff", "arguments": "{bad:1}"}},
    ]
    async def http(request):
        return httpx.Response(200, json=wire(calls=calls))
    binding = AttemptBinding("r", 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http)), binding)
    with pytest.raises(ResponseViolation):
        asyncio.run(h.run())
    assert not any(x.startswith("start:") for x in h.trace)


def test_concurrent_attempts_cannot_cross_consume_or_replay():
    async def scenario():
        async def http(request):
            await asyncio.sleep(0)
            return httpx.Response(200, json=wire())
        a, b = AttemptBinding("a", 1, 1), AttemptBinding("b", 1, 1)
        first = WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
            api_key="synthetic-key", binding=a, transport=httpx.MockTransport(http))
        second = WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
            api_key="synthetic-key", binding=b, transport=httpx.MockTransport(http))
        request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="synthetic")])])
        ag, bg = first.generate_content_async(request), second.generate_content_async(request)
        ar, br = await asyncio.gather(anext(ag), anext(bg))
        with pytest.raises(ResponseViolation):
            first.verify_response(br, binding=a)
        with pytest.raises(ResponseViolation):
            first.verify_response(ar, binding=a)
        second.verify_response(br, binding=b)
        with pytest.raises(ResponseViolation):
            second.verify_response(br, binding=b)
        await asyncio.gather(ag.aclose(), bg.aclose())
        with pytest.raises(ResponseViolation):
            second.verify_response(br, binding=b)
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500])
def test_real_sdk_errors_do_not_retry_or_leak_body(status, caplog):
    sent = []
    async def http(request):
        sent.append(request)
        return httpx.Response(status, json={"error": "synthetic-secret-never-log"})
    binding = AttemptBinding("r", 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http)), binding)
    with pytest.raises(ResponseViolation) as caught:
        asyncio.run(h.run())
    assert len(sent) == 1
    assert "synthetic-secret-never-log" not in str(caught.value) + caplog.text
    assert not any(x.startswith("start:") for x in h.trace)


@pytest.mark.parametrize("usage", [
    "SYNTHETIC_PRIVATE_USAGE", ["SYNTHETIC_PRIVATE_USAGE"], {},
    *({"prompt_tokens": value, "completion_tokens": 10, "total_tokens": 30}
      for value in ("SYNTHETIC_PRIVATE_USAGE", "20", True, 20.0, None,
                    {"private": "SYNTHETIC_PRIVATE_USAGE"})),
    {"prompt_tokens": 20, "completion_tokens": "SYNTHETIC_PRIVATE_USAGE", "total_tokens": 30},
    {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": "SYNTHETIC_PRIVATE_USAGE"},
    {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
     "prompt_tokens_details": {"cached_tokens": "SYNTHETIC_PRIVATE_USAGE"}},
    {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
     "completion_tokens_details": {"reasoning_tokens": "SYNTHETIC_PRIVATE_USAGE"}},
])
def test_malformed_usage_is_rejected_without_sdk_warning_leaks(usage, capfd, caplog, recwarn):
    sent = []
    async def http(request):
        sent.append(request)
        body = wire()
        body["usage"] = usage
        return httpx.Response(200, json=body)
    binding = AttemptBinding("usage", 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http)), binding)
    with pytest.raises(ResponseViolation) as caught:
        asyncio.run(h.run())
    captured = capfd.readouterr()
    diagnostics = captured.out + captured.err + caplog.text + str(caught.value)
    diagnostics += "".join(str(w.message) for w in recwarn)
    assert "SYNTHETIC_PRIVATE_USAGE" not in diagnostics
    assert str(caught.value) == "invalid_response_usage"
    assert len(sent) == 1 and h.cleaned
    assert not any(x.startswith("start:") for x in h.trace)


@pytest.mark.parametrize("usage", [None, {
    "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
    "prompt_tokens_details": {"cached_tokens": 5, "audio_tokens": None},
    "completion_tokens_details": {"reasoning_tokens": 3},
    "prompt_cache_hit_tokens": 5,
}])
def test_valid_optional_usage_and_provider_extensions_keep_original_response(usage):
    async def http(request):
        body = wire()
        body["usage"] = usage
        return httpx.Response(200, json=body)
    binding = AttemptBinding("usage-valid", 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http)), binding)
    asyncio.run(h.run())
    assert "start:finish" in h.trace and h.cleaned


def test_cancellation_closes_http_attempt_and_invalidates_receipt():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        class BlockingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        binding = AttemptBinding("r", 1, 1)
        model = WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
            api_key="synthetic-key", binding=binding, transport=BlockingTransport())
        h = CheckedHarness(model, binding)
        task = asyncio.create_task(h.run())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and h.cleaned
        with pytest.raises(ResponseViolation):
            model.verify_response(None, binding=binding)
        assert not any(x.startswith("start:") for x in h.trace)
    asyncio.run(scenario())


def test_provider_environment_cannot_redirect_explicit_pinned_client(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    monkeypatch.setenv("HTTPS_PROXY", "https://evil.example/proxy")
    seen = []
    async def http(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=wire())
    binding = AttemptBinding("r", 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http)), binding)
    asyncio.run(h.run())
    assert seen == ["https://open.bigmodel.cn/api/paas/v4/chat/completions"]


@pytest.mark.parametrize("authorized", [True, False])
def test_image_outbound_and_response_witnesses_are_both_required(authorized):
    import hashlib
    from personal_agent.runtime.model_input import ImageInputPart
    image = b"\x89PNG\r\n\x1a\n" + b"synthetic" * 8
    part = ImageInputPart(mime_type="image/png", data=image,
        content_sha256=hashlib.sha256(image).hexdigest(), token_upper_bound=512)
    sent = []
    async def http(request):
        sent.append(request)
        return httpx.Response(200, json=wire())
    async def scenario():
        binding = AttemptBinding("image", 1, 1)
        model = WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
            api_key="synthetic-key", binding=binding, transport=httpx.MockTransport(http),
            expected_images=(part,) if authorized else (), text_required=True)
        request = LlmRequest(contents=[types.Content(role="user", parts=[
            types.Part(text="synthetic image"), types.Part(inline_data=types.Blob(mime_type="image/png", data=image))])])
        async for response in model.generate_content_async(request):
            model.verify_response(response, binding=binding)
    if authorized:
        asyncio.run(scenario())
        assert len(sent) == 1
    else:
        with pytest.raises(ResponseViolation):
            asyncio.run(scenario())
        assert sent == []

@pytest.mark.parametrize('reported', [None, 'other-model', 42])
def test_response_model_must_match_requested_model(reported):
    async def http(request):
        data = wire(); data['model'] = reported
        return httpx.Response(200, json=data)
    binding = AttemptBinding('model-check', 1, 1)
    h = CheckedHarness(WitnessedLiteLlm(model='openai/synthetic', provider_name='zhipu',
        api_key='synthetic-key', binding=binding, transport=httpx.MockTransport(http)), binding)
    with pytest.raises(ResponseViolation, match='response_model_mismatch'):
        asyncio.run(h.run())
    assert not any(x.startswith('start:') for x in h.trace)


def test_callbacks_and_verbose_logs_cannot_observe_private_input(monkeypatch, capsys, caplog):
    import litellm
    import logging
    observed = []
    def spy(*args, **kwargs): observed.append((args, kwargs))
    for name in ('callbacks', 'input_callback', 'success_callback', 'failure_callback',
                 '_async_input_callback', '_async_success_callback', '_async_failure_callback'):
        monkeypatch.setattr(litellm, name, [spy])
    monkeypatch.setattr(litellm, 'set_verbose', True)
    caplog.set_level(logging.DEBUG)
    async def http(request):
        assert request.headers['authorization'] == 'Bearer private-api-key-marker'
        return httpx.Response(200, json=wire())
    async def run():
        model = WitnessedLiteLlm(model='openai/synthetic', provider_name='zhipu',
            api_key='private-api-key-marker', binding=AttemptBinding('private', 1, 1), transport=httpx.MockTransport(http))
        request = LlmRequest(contents=[types.Content(role='user', parts=[types.Part(text='private-prompt-marker')])])
        async for _ in model.generate_content_async(request): pass
    asyncio.run(run())
    captured = capsys.readouterr()
    assert not observed
    for secret in ('private-api-key-marker', 'private-prompt-marker'):
        assert secret not in captured.out + captured.err + caplog.text
