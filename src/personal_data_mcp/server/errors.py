"""Turning a failure into the one error shape the client will parse.

The Client Core reads an error result by looking for a JSON object carrying a
stable `code`, and maps anything it cannot parse to `INTERNAL_ERROR`. So the
server has exactly one job here: every failure path, including one nobody
anticipated, must leave through this function.

That is why an unexpected exception is caught and rewritten rather than allowed
to propagate. If it propagated, the SDK would build an error result out of
`str(exception)`, and the text of an arbitrary exception is precisely the thing
that carries a Feishu response body, a table identifier or a file path over the
wire. The internal text stays in the log; the wire gets a code.
"""

from __future__ import annotations

import json
from typing import Any, Final

from mcp.types import CallToolResult, TextContent

from personal_agent_core.errors import AppError, ErrorCode


#: The envelope is nested under `error` so a receipt and a failure can never be
#: confused for one another by shape alone.
ERROR_KEY: Final[str] = "error"


def error_result(
    error: AppError,
    *,
    trace_id: str | None = None,
) -> CallToolResult:
    """The single outward failure shape.

    `structuredContent` is deliberately left unset. An error is not a receipt,
    and a client that validates structured output against a tool's success
    schema should not be handed something that half satisfies it.
    """
    envelope = error.to_envelope(trace_id=trace_id).model_dump(mode="json")
    return CallToolResult(
        isError=True,
        content=[
            TextContent(
                type="text",
                text=json.dumps({ERROR_KEY: envelope}, ensure_ascii=False),
            )
        ],
    )


def internal_error_result(trace_id: str | None = None) -> CallToolResult:
    """For a failure that was never given a business meaning."""
    return error_result(AppError(ErrorCode.INTERNAL_ERROR), trace_id=trace_id)


def success_result(payload: dict[str, Any]) -> CallToolResult:
    """A receipt, carried both as structured content and as its JSON text."""
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False)
            )
        ],
        structuredContent=payload,
    )
