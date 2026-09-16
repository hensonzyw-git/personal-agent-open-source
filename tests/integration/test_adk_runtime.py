"""The actual v2 Runner composition, synthetic durable Host/HTTP boundary."""

import asyncio
import json

import httpx
import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from personal_agent.runtime.adk_runtime import AdkRuntime, ModelAttemptInput
from personal_agent.runtime.run_tools import RunToolSpec, ToolResult
from personal_agent.runtime.response_witness import AttemptBinding, ResponseViolation
from personal_agent.runtime.witnessed_model import WitnessedLiteLlm

from test_witnessed_model import wire


def fc(name, call_id, **args):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args),
    }}


class Host:
    def __init__(self, responses):
        self.responses = responses
        self.http_requests = []
        self.executed = []
        self.attempts = 0
        self.stopped = False
        self.failed = False
        self.stale = False

    async def prepare_model(self, *, tools):
        assert not self.stopped
        self.attempts += 1
        return ModelAttemptInput(binding=AttemptBinding("op", self.attempts, 1),
            request=LlmRequest(contents=[types.Content(role="user", parts=[types.Part(
                text=f"host projection {self.attempts}: {len(self.executed)} committed evidence"
            )])], config=types.GenerateContentConfig(system_instruction="Synthetic only", tools=tools)))

    def model_for(self, prepared):
        async def http(request):
            self.http_requests.append(json.loads(request.content))
            return httpx.Response(200, json=wire(calls=self.responses[len(self.http_requests)-1]))
        return WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
            api_key="synthetic-key", binding=prepared.binding, transport=httpx.MockTransport(http))

    async def accept_batch(self, binding, calls):
        assert len(self.http_requests) == self.attempts
        # The real Host will commit Task binding/reservations and return its fence.
        return "synthetic-fence"

    async def check_active(self, authority):
        if self.stale:
            raise ResponseViolation("stale_fence")

    async def execute(self, call, authority):
        await self.check_active(authority)
        self.executed.append(call.name)
        if self.failed:
            raise ResponseViolation("synthetic_read_failed")
        if call.name != "read":
            self.stopped = True
            return ToolResult({"kind": call.name}, stop=True)
        return ToolResult({"evidence": "synthetic"})

    async def failed_run(self, code):
        self.failed = True


SPECS = [RunToolSpec(name, name, kind, "synthetic", {
    "type": "object", "properties": {}, "additionalProperties": False,
}) for name, kind in (("read", "read"), ("finish", "control"), ("write", "write"))]


@pytest.mark.parametrize("calls", [
    [fc("read", "c1"), fc("unknown", "c2")],
    [fc("read", "c1"), fc("write", "c2")],
    [fc("write", "c1"), fc("write", "c2")],
    [fc("finish", "c1"), fc("read", "c2")],
    [fc("read", "c1", unexpected=True)],
    [fc("read", f"c{i}") for i in range(4)],
])
def test_actual_runtime_refuses_whole_invalid_batch(calls):
    host = Host([calls])
    with pytest.raises(ResponseViolation):
        asyncio.run(AdkRuntime(host=host, specs=SPECS).run())
    assert not host.executed and host.failed
    assert len(host.http_requests) == 1


def test_actual_runtime_two_reads_then_finish_from_host_projection():
    host = Host([[fc("read", "a"), fc("read", "b")], [fc("finish", "c")]])
    result = asyncio.run(AdkRuntime(host=host, specs=SPECS).run())
    assert result == {"kind": "finish"}
    assert host.executed == ["read", "read", "finish"]
    assert host.attempts == 2 and len(host.http_requests) == 2
    assert all(len(r["messages"]) == 2 for r in host.http_requests)  # system + unique Host input
    assert "2 committed evidence" in host.http_requests[1]["messages"][1]["content"]


def test_failed_first_read_does_not_start_second_read():
    host = Host([[fc("read", "a"), fc("read", "b")]])
    host.failed = True
    with pytest.raises(ResponseViolation):
        asyncio.run(AdkRuntime(host=host, specs=SPECS).run())
    assert host.executed == ["read"]


def test_stale_after_batch_admission_refuses_execution():
    host = Host([[fc("write", "a")]])
    host.stale = True
    with pytest.raises(ResponseViolation):
        asyncio.run(AdkRuntime(host=host, specs=SPECS).run())
    assert host.executed == []


def test_model_loop_limit_is_shared_across_steps():
    host = Host([[fc("read", "a")]] * 5)
    with pytest.raises(ResponseViolation):
        asyncio.run(AdkRuntime(host=host, specs=SPECS, max_reads=3).run())
    assert len(host.http_requests) <= 4
    assert len(host.executed) == 3


def test_runtime_cancellation_closes_nested_sdk_generator():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        class HangingHost(Host):
            def model_for(self, prepared):
                class Transport(httpx.AsyncBaseTransport):
                    async def handle_async_request(self, request):
                        entered.set()
                        await asyncio.Event().wait()
                    async def aclose(self):
                        closed.set()
                return WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
                    api_key="synthetic", binding=prepared.binding, transport=Transport())
        host = HangingHost([])
        task = asyncio.create_task(AdkRuntime(host=host, specs=SPECS).run())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and host.failed and not host.executed
    asyncio.run(scenario())


def test_host_model_attempt_mismatch_refuses_before_http():
    class WrongBindingHost(Host):
        def model_for(self, prepared):
            wrong = ModelAttemptInput(binding=AttemptBinding("other", 1, 1), request=prepared.request)
            return super().model_for(wrong)
    host = WrongBindingHost([[fc("finish", "a")]])
    with pytest.raises(ResponseViolation, match="model_attempt_binding_mismatch"):
        asyncio.run(AdkRuntime(host=host, specs=SPECS).run())
    assert host.http_requests == []
