"""V2's actual Runner composition. Durable policy/state remain on its Host.

The Host contract is deliberately mandatory. This module cannot supply a fake
default store or reach a business connector directly. API composition uses DurableRunHost with persistent Task/lease/budget authority.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from jsonschema import Draft202012Validator
from pydantic import PrivateAttr

from personal_agent.runtime.response_witness import AttemptBinding, ResponseViolation, arguments_hash
from personal_agent.runtime.run_tools import AcceptedCall, RunTool, RunToolSpec, ToolResult
from personal_agent.runtime.witnessed_model import WitnessedLiteLlm
from personal_agent.runtime.run_store import RunStateError


@dataclass(frozen=True)
class ModelAttemptInput:
    binding: AttemptBinding
    request: LlmRequest


class RunHost(Protocol):
    async def prepare_model(self, *, tools: list[types.Tool]) -> ModelAttemptInput:
        """Commit reservation, fresh fence check and budgeted request projection."""
        ...

    def model_for(self, prepared: ModelAttemptInput) -> WitnessedLiteLlm: ...

    async def accept_batch(self, binding: AttemptBinding, calls: tuple[AcceptedCall, ...]) -> object:
        """Atomically bind Task, settle reservations and issue fresh authority."""
        ...

    async def check_active(self, authority: object) -> None: ...

    async def execute(self, call: AcceptedCall, authority: object) -> ToolResult:
        """Reserve/dispatch reads or commit an exclusive control/write handoff.

        The Host freezes a write proposal, then uses the existing orchestrator
        to claim submit eligibility and execute the governed handoff.
        """
        ...

    async def failed_run(self, code: str) -> None:
        """Mark remaining admitted calls not_executed, preserve committed facts."""
        ...


def _request_digest(request):
    raw = json.dumps({"model": request.model, "contents": [c.model_dump(mode="json", exclude_none=True) for c in request.contents],
        "config": request.config.model_dump(mode="json", exclude_none=True)},
        sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class _RunModel(BaseLlm):
    _owner: object = PrivateAttr()

    def __init__(self, owner):
        super().__init__(model="host_witnessed_run")
        self._owner = owner

    async def generate_content_async(self, llm_request, stream=False):
        owner = self._owner
        if stream or _request_digest(llm_request) != owner.request_digest:
            raise ResponseViolation("request_projection_mismatch")
        async with aclosing(owner.model.generate_content_async(llm_request, stream=False)) as output:
            async for result in output:
                yield result


class AdkRuntime:
    def __init__(self, *, host: RunHost, specs: list[RunToolSpec], max_reads: int = 3):
        if not 0 <= max_reads <= 3 or len({s.name for s in specs}) != len(specs):
            raise ResponseViolation("invalid_run_catalog")
        if any(s.kind not in {"read", "write", "control"} for s in specs):
            raise ResponseViolation("invalid_tool_kind")
        self.host = host
        self.specs = {s.name: s for s in specs}
        self.validators = {s.name: Draft202012Validator(s.schema) for s in specs}
        for s in specs:
            Draft202012Validator.check_schema(s.schema)
        self.max_reads = max_reads
        self.read_slots = 0
        self.model_calls = 0
        self.model = None
        self.prepared = None
        self.request_digest = None
        self.accepted = {}
        self.claimed = set()
        self.authority = None
        self.result = None
        self.failed = False
        self.started = False
        self.lock = asyncio.Lock()

    async def before_model(self, callback_context, llm_request):
        if self.failed or self.result is not None or self.model_calls >= 4:
            raise ResponseViolation("run_stopped_or_budget")
        tools = [types.Tool(function_declarations=[
            RunTool(s, self)._get_declaration() for s in self.specs.values()
        ])]
        self.prepared = await self.host.prepare_model(tools=tools)
        if self.prepared.request.config.tools != tools:
            raise ResponseViolation("catalog_projection_mismatch")
        self.model_calls += 1
        self.model = self.host.model_for(self.prepared)
        if not isinstance(self.model, WitnessedLiteLlm):
            raise ResponseViolation("unwitnessed_model")
        if self.model.binding != self.prepared.binding:
            raise ResponseViolation("model_attempt_binding_mismatch")
        llm_request.model = self.model.model
        llm_request.contents = [c.model_copy(deep=True) for c in self.prepared.request.contents]
        llm_request.config = self.prepared.request.config.model_copy(deep=True)
        # ADK inserts this exact diagnostic label after before_model. Include it
        # before hashing, rather than ignoring arbitrary config mutations.
        llm_request.config.labels = {"adk_agent_name": "personal_agent_v2"}
        self.request_digest = _request_digest(llm_request)
        self.accepted = {}
        self.claimed = set()

    async def after_model(self, callback_context, llm_response):
        stamps = self.model.verify_response(llm_response, binding=self.prepared.binding)
        recorder=getattr(self.host,'record_model_usage',None)
        if recorder is not None:await recorder(self.prepared.binding,llm_response)
        if not stamps:
            raise ResponseViolation("finish_required")
        calls = []
        for part in llm_response.content.parts:
            if not part.function_call:
                continue  # Neither prose nor thought is execution evidence.
            fc = part.function_call
            spec = self.specs.get(fc.name)
            if spec is None or not self.validators[fc.name].is_valid(fc.args):
                raise ResponseViolation("invalid_business_call")
            calls.append(AcceptedCall(fc.id, fc.name, spec.business_name,
                json.dumps(fc.args, ensure_ascii=False, allow_nan=False), arguments_hash(fc.args)))
        kinds = [self.specs[c.name].kind for c in calls]
        if len(calls) > 1 and any(k != "read" for k in kinds):
            raise ResponseViolation("mixed_or_multiple_action_batch")
        if kinds.count("read") + self.read_slots > self.max_reads:
            raise ResponseViolation("read_budget")
        # Task binding and reservation settlement happen before any wrapper.
        self.authority = await self.host.accept_batch(self.prepared.binding, tuple(calls))
        self.read_slots += kinds.count("read")
        self.accepted = {c.call_id: c for c in calls}

    async def _check_call(self, name, args, call_id):
        if self.failed or self.result is not None or call_id in self.claimed:
            raise ResponseViolation("run_stopped_or_replayed_call")
        call = self.accepted.get(call_id)
        if call is None or name != call.name or arguments_hash(args) != call.args_hash:
            raise ResponseViolation("unaccepted_call")
        await self.host.check_active(self.authority)
        return call

    async def before_tool(self, tool, args, tool_context):
        await self._check_call(tool.name, args, tool_context.function_call_id)

    async def execute_tool(self, name, args, call_id):
        async with self.lock:
            try:
                call = await self._check_call(name, args, call_id)
                self.claimed.add(call_id)
                result = await self.host.execute(call, self.authority)
                if result.stop:
                    if len(self.accepted) != 1:
                        raise ResponseViolation("nonexclusive_stop")
                    self.result = result.payload
                elif self.specs[name].kind == "write":
                    raise ResponseViolation("write_requires_handoff")
                return result
            except BaseException:
                self.failed = True
                raise

    async def run(self):
        if self.started:
            raise ResponseViolation("run_reused")
        self.started = True
        service = InMemorySessionService()
        sid = uuid4().hex
        agent = LlmAgent(name="personal_agent_v2", model=_RunModel(self),
            include_contents="none", tools=[RunTool(s, self) for s in self.specs.values()],
            before_model_callback=self.before_model, after_model_callback=self.after_model,
            before_tool_callback=self.before_tool)
        runner = Runner(app_name="personal_agent_v2", agent=agent, session_service=service)
        await service.create_session(app_name="personal_agent_v2", user_id="host", session_id=sid)
        try:
            async with asyncio.timeout(60):
                async for _ in runner.run_async(user_id="host", session_id=sid,
                    new_message=types.Content(role="user", parts=[types.Part(text="Host projection follows")]),
                    run_config=RunConfig(max_llm_calls=4)):
                    pass  # Drain Runner; closing a generator is not a normal stop.
            if self.result is None:
                raise ResponseViolation("missing_host_outcome")
            return self.result
        except BaseException as exc:
            self.failed = True
            code = str(exc) if isinstance(exc, (ResponseViolation, RunStateError)) else "run_failed"
            unstarted = tuple(c for c in self.accepted.values() if c.call_id not in self.claimed)
            if unstarted and hasattr(self.host, 'batch_failed'):
                try:await self.host.batch_failed(unstarted)
                except RunStateError:pass  # A revoked fence cannot mutate the new owner's steps.
            retry = code == 'finish_required' and getattr(self.host, 'allow_format_retry', lambda: False)()
            if not retry:await self.host.failed_run(code)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ResponseViolation(code) from None
        finally:
            await runner.close()
            await service.delete_session(app_name="personal_agent_v2", user_id="host", session_id=sid)
