"""T1 framework gate: real locked ADK, synthetic counterparties, no network.

This deliberately tests ADK mechanics before production runtime implementation.
The harness is not a dispatcher, budget store, or model accuracy evaluation.
"""

import asyncio
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool
from google.genai import types
from pydantic import PrivateAttr


def call(name="read", call_id="c1", **args):
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))


def response(*parts):
    return LlmResponse(content=types.Content(role="model", parts=list(parts)))


class ScriptedModel(BaseLlm):
    _harness: object = PrivateAttr()

    def __init__(self, harness):
        super().__init__(model="synthetic-locked-adk-gate")
        self._harness = harness

    async def generate_content_async(self, llm_request, stream=False):
        h = self._harness
        assert stream is False
        h.requests.append(SimpleNamespace(
            contents=[c.model_copy(deep=True) for c in llm_request.contents],
            config=llm_request.config.model_copy(deep=True),
        ))
        h.trace.append("model")
        try:
            item = h.script[len(h.requests) - 1]
            if isinstance(item, Exception):
                raise item
            if item == "hang":
                h.entered.set()
                await asyncio.Event().wait()
            yield item
        finally:
            h.trace.append("model_closed")


class ProbeTool(BaseTool):
    def __init__(self, name, harness):
        super().__init__(name=name, description="Synthetic framework probe")
        self.harness = harness

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={"type": "object", "properties": {}},
        )

    async def run_async(self, *, args, tool_context):
        h = self.harness
        async with h.lock:
            h.verify(self.name, args, tool_context.function_call_id)
            h.trace.append(f"start:{self.name}")
            await asyncio.sleep(0)
            if h.tool_failure:
                raise RuntimeError("synthetic tool failure")
            if self.name != "read":
                h.stopped = True
                tool_context.actions.skip_summarization = True
            h.trace.append(f"end:{self.name}")
            return {"kind": self.name, "evidence": "synthetic evidence"}


class Harness:
    def __init__(self, script, *, reject=False, tool_failure=False, model=None):
        self.script = script
        self.reject = reject
        self.tool_failure = tool_failure
        self.trace = []
        self.requests = []
        self.accepted = {}
        self.stopped = False
        self.lock = asyncio.Lock()
        self.entered = asyncio.Event()
        self.cleaned = False
        self.model = model

    async def before_model(self, callback_context, llm_request):
        assert not self.stopped
        self.trace.append("before_model")
        # Host is sole input authority; deliberately exclude ADK's user/history.
        llm_request.contents = [types.Content(
            role="user", parts=[types.Part(text=f"host projection {len(self.requests)}")]
        )]

    async def after_model(self, callback_context, llm_response):
        self.trace.append("after_model")
        if self.reject:
            raise ValueError("synthetic whole-batch refusal")
        calls = [p.function_call for p in llm_response.content.parts if p.function_call]
        self.accepted = {c.id: (c.name, c.args) for c in calls}

    def verify(self, name, args, call_id):
        assert not self.stopped
        assert self.accepted[call_id] == (name, args)

    async def before_tool(self, tool, args, tool_context):
        self.verify(tool.name, args, tool_context.function_call_id)
        self.trace.append(f"before_tool:{tool.name}")

    async def run(self, *, limit=4):
        sessions = InMemorySessionService()
        agent = LlmAgent(
            name="locked_gate", model=self.model or ScriptedModel(self), include_contents="none",
            tools=[ProbeTool(name, self) for name in ("read", "finish", "card", "clarification", "write_handoff")],
            before_model_callback=self.before_model,
            after_model_callback=self.after_model,
            before_tool_callback=self.before_tool,
        )
        runner = Runner(app_name="locked_gate", agent=agent, session_service=sessions)
        await sessions.create_session(app_name="locked_gate", user_id="synthetic", session_id="probe")
        try:
            return [event async for event in runner.run_async(
                user_id="synthetic", session_id="probe",
                new_message=types.Content(role="user", parts=[types.Part(text="excluded raw history")]),
                run_config=RunConfig(max_llm_calls=limit),
            )]
        finally:
            await runner.close()
            await sessions.delete_session(app_name="locked_gate", user_id="synthetic", session_id="probe")
            assert await sessions.get_session(app_name="locked_gate", user_id="synthetic", session_id="probe") is None
            self.cleaned = True


def test_exact_locked_versions():
    assert version("google-adk") == "2.5.0"
    assert version("litellm") == "1.91.4"


@pytest.mark.parametrize("parts", [
    (), (call(), call("unknown", "c2")),
    (call(), call("write_handoff", "c2")),
    (call("finish"), call("read", "c2")),
    (call("write_handoff"), call("write_handoff", "c2")),
    (call(), call(call_id="c1")),
    (call(amount="malformed"),),
])
def test_after_model_refusal_precedes_every_tool(parts):
    h = Harness([response(*parts)], reject=True)
    with pytest.raises(ValueError, match="whole-batch"):
        asyncio.run(h.run())
    assert h.trace == ["before_model", "model", "after_model", "model_closed"]
    assert h.cleaned


@pytest.mark.parametrize("name", ["finish", "card", "clarification", "write_handoff"])
def test_native_stop_consumes_runner_without_another_model_request(name):
    h = Harness([response(call(name))])
    events = asyncio.run(h.run())
    assert len(h.requests) == 1
    assert events[-1].is_final_response()
    assert events[-1].actions.skip_summarization
    assert h.trace[-1] == "model_closed"
    assert h.stopped and h.cleaned


def test_two_reads_are_validated_first_serialized_and_projected_once():
    h = Harness([
        response(types.Part(text="untrusted companion"), types.Part(text="private thought", thought=True), call(), call(call_id="c2")),
        response(call("finish", "c3")),
    ])
    asyncio.run(h.run())
    assert len(h.requests) == 2
    assert [r.contents[0].parts[0].text for r in h.requests] == ["host projection 0", "host projection 1"]
    assert all(len(r.contents) == 1 for r in h.requests)
    assert [x for x in h.trace if x.startswith(("start:", "end:"))] == [
        "start:read", "end:read", "start:read", "end:read", "start:finish", "end:finish",
    ]
    assert h.trace.index("after_model") < h.trace.index("before_tool:read")
    assert h.cleaned


@pytest.mark.parametrize("tool_failure", [False, True])
def test_errors_escape_and_cleanup_without_success(tool_failure):
    h = Harness([response(call())] if tool_failure else [RuntimeError("synthetic provider failure")], tool_failure=tool_failure)
    with pytest.raises(RuntimeError, match="synthetic"):
        asyncio.run(h.run())
    assert not h.stopped and h.cleaned
    assert "end:read" not in h.trace


def test_adk_limit_prevents_next_model_external_attempt():
    h = Harness([response(call())])
    with pytest.raises(Exception, match="[Ll][Ll][Mm]|limit"):
        asyncio.run(h.run(limit=1))
    assert len(h.requests) == 1
    assert h.cleaned


def test_cancellation_awaits_generator_and_runner_cleanup():
    async def scenario():
        h = Harness(["hang"])
        task = asyncio.create_task(h.run())
        await asyncio.wait_for(h.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.cleaned
        assert h.trace[-1] == "model_closed"
        assert not any(x.startswith("start:") for x in h.trace)
    asyncio.run(scenario())


@pytest.mark.parametrize("raw, allowed", [
    pytest.param("{'value': 1}", False, id="python-literal"),
    pytest.param('{value: 1}', False, id="unquoted-key"),
    pytest.param('{"value": 1, "value": 2}', False, id="duplicate-key"),
    pytest.param('{"value": 1}', True, id="valid-json-control"),
])
def test_locked_litellm_must_not_repair_arguments_before_batch_validation(raw, allowed):
    """Approved §4.1/§8: no repaired or duplicate arguments may reach tools.

    Real LiteLlm + Runner; only the completion counterparty is synthetic.
    This tests the information visible at after_model, not a fake ADK parser.
    """
    import httpx
    from jsonschema import validate
    from personal_agent.runtime.response_witness import AttemptBinding, ResponseViolation
    from personal_agent.runtime.witnessed_model import WitnessedLiteLlm

    binding = AttemptBinding("synthetic-request", 1, 1)
    sent = []
    async def raw_http(request):
        sent.append(request)
        return httpx.Response(200, json={
            "id": "synthetic-response", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{
                "index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "raw-c1", "type": "function",
                    "function": {"name": "write_handoff", "arguments": raw},
                }]},
            }], "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })

    class SchemaCheckingHarness(Harness):
        async def after_model(self, callback_context, llm_response):
            self.model.verify_response(llm_response, binding=binding)
            # Schema validation supplements raw syntax/provenance validation.
            parts = llm_response.content.parts
            assert len(parts) == 1
            assert parts[0].function_call.name == "write_handoff"
            validate(parts[0].function_call.args, {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"], "additionalProperties": False,
            })
            await super().after_model(callback_context, llm_response)

    model = WitnessedLiteLlm(model="openai/synthetic", provider_name="zhipu",
        api_key="synthetic-test-key", binding=binding, transport=httpx.MockTransport(raw_http))
    h = SchemaCheckingHarness([], model=model)
    try:
        asyncio.run(h.run())
    except ResponseViolation:
        if allowed:
            raise
    assert len(sent) == 1
    assert any(x.startswith("start:") for x in h.trace) is allowed, (
        "Raw invalid/duplicate arguments became executable before the callback; "
        f"callback saw {h.accepted!r}"
    )
    assert h.cleaned
