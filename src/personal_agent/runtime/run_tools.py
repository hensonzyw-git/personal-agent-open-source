"""ADK wrappers cannot reach a connector except through the owning Run Host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from google.adk.tools import BaseTool
from google.genai import types


@dataclass(frozen=True)
class RunToolSpec:
    name: str
    business_name: str
    kind: Literal["read", "write", "control"]
    description: str
    schema: dict


@dataclass(frozen=True)
class AcceptedCall:
    call_id: str
    name: str
    business_name: str
    arguments_json: str
    args_hash: str

    @property
    def args(self) -> dict:
        # Fresh value: neither an ADK callback nor a connector can mutate the
        # accepted arguments another concurrent wrapper will check.
        return json.loads(self.arguments_json)


@dataclass(frozen=True)
class ToolResult:
    payload: dict[str, Any]
    stop: bool = False


class RunTool(BaseTool):
    def __init__(self, spec: RunToolSpec, controller):
        super().__init__(name=spec.name, description=spec.description)
        self._spec = spec
        self._controller = controller

    def _get_declaration(self):
        return types.FunctionDeclaration(name=self.name, description=self.description,
            parameters_json_schema=self._spec.schema)

    async def run_async(self, *, args, tool_context):
        result = await self._controller.execute_tool(self.name, args, tool_context.function_call_id)
        if result.stop:
            # Host.execute must have committed its outcome before returning.
            tool_context.actions.skip_summarization = True
        return result.payload
