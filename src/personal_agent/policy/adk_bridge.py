"""Optional Google ADK wrappers for the governed tool bridge."""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from google.adk.tools import BaseTool
from google.genai import types

if TYPE_CHECKING:
    from personal_agent.policy.bridge import (
        BridgeCallContext,
        DeviceAuthorization,
        GovernedToolBridge,
        VisibleTool,
    )


class GovernedAdkTool(BaseTool):
    """An ADK wrapper whose only execution path is the governed bridge."""

    def __init__(
        self,
        *,
        visible: "VisibleTool",
        bridge: "GovernedToolBridge",
        device_provider: Callable[[], "DeviceAuthorization"],
        context_provider: Callable[
            [str, dict[str, Any], "DeviceAuthorization", Any],
            "BridgeCallContext",
        ],
    ) -> None:
        super().__init__(
            name=visible.alias,
            description=visible.description,
            custom_metadata={
                "risk_level": visible.risk_level,
                "required_scopes": list(visible.required_scopes),
            },
        )
        self._visible = visible
        self._bridge = bridge
        self._device_provider = device_provider
        self._context_provider = context_provider

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self._visible.input_schema,
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        device = self._device_provider()
        call_context = self._context_provider(
            self.name, args, device, tool_context
        )
        result = await self._bridge.execute(
            self.name,
            args,
            device,
            call_context=call_context,
        )
        return result.model_result


def build_adk_tools(
    *,
    visible_tools: list["VisibleTool"],
    bridge: "GovernedToolBridge",
    device_provider: Callable[[], "DeviceAuthorization"],
    context_provider: Callable[
        [str, dict[str, Any], "DeviceAuthorization", Any],
        "BridgeCallContext",
    ],
) -> list[BaseTool]:
    return [
        GovernedAdkTool(
            visible=visible,
            bridge=bridge,
            device_provider=device_provider,
            context_provider=context_provider,
        )
        for visible in visible_tools
    ]
