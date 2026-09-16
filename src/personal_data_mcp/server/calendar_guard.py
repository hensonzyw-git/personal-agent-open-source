"""The fail-closed guard behind ``calendar.create_event``.

`calendar.create_event` is an ``executor="device"`` contract: the write
happens on the iPhone through EventKit, not through this MCP server. The
server still advertises the tool — the registry's catalog is built from
advertised tools, so not advertising it would hide it from the model — but
the real execution path is the dispatcher's device branch, which never
reaches a handler.

That makes any call arriving here a composition defect: the guard's only
correct output is refusal. It exists so the failure is a stable code rather
than a missing-handler `TOOL_NOT_ALLOWLISTED` that would look like the tool
being uninstalled, and so a future mistake in the dispatch fork cannot
silently turn a device write into a no-op MCP success.
"""

from __future__ import annotations

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.server.handlers import ToolHandler


class CreateGuard:
    """Marker dependency: the guard needs nothing from composition."""

    def __init__(self) -> None:
        self.reason = (
            "calendar.create_event is device-executed; "
            "dispatch must intercept it before any MCP handler runs"
        )


def build_handler(_guard: CreateGuard) -> ToolHandler:
    """Return the guard handler. It must never be reachable in production."""

    async def handler(_invocation) -> dict:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=_guard.reason,
        )

    return handler
