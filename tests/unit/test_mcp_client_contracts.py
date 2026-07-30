"""Focused regressions for MCP result and pagination contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from personal_agent.mcp_client.core import (
    McpClientCore,
    McpTransportError,
    StdioTransport,
    _validate_protocol_version,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.mcp_protocol import FINANCE_PROTOCOL_VERSIONS


def client_with(session) -> McpClientCore:
    client = McpClientCore(
        "fixture", StdioTransport(command="unused", args=[])
    )
    client._session = session
    return client


def test_business_error_code_survives_the_mcp_boundary() -> None:
    class Session:
        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(
                is_error=True,
                structured_content={
                    "error": {
                        "code": "SOURCE_COMMIT_UNKNOWN",
                        "message": "untrusted",
                        "retryable": False,
                    }
                },
                content=[],
            )

    with pytest.raises(AppError) as excinfo:
        asyncio.run(client_with(Session()).call_tool("finance.log_expense", {}))
    assert excinfo.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN


def test_an_unknown_or_malformed_error_fails_closed() -> None:
    class Session:
        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(
                is_error=True,
                structured_content={"error": {"code": "PROVIDER_SECRET_ERROR"}},
                content=[],
            )

    with pytest.raises(AppError) as excinfo:
        asyncio.run(client_with(Session()).call_tool("finance.log_expense", {}))
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert "PROVIDER_SECRET_ERROR" not in str(excinfo.value.to_envelope())


def test_resources_list_is_followed_to_exhaustion() -> None:
    class Session:
        def __init__(self) -> None:
            self.cursors = []

        async def list_resources(self, *, params=None):
            cursor = params.cursor if params is not None else None
            self.cursors.append(cursor)
            if cursor is None:
                return SimpleNamespace(resources=["one"], next_cursor="page-2")
            return SimpleNamespace(resources=["two"], next_cursor=None)

    session = Session()
    resources = asyncio.run(client_with(session).list_resources())
    assert resources == ["one", "two"]
    assert session.cursors == [None, "page-2"]


def test_only_the_frozen_modern_and_explicit_legacy_versions_are_accepted() -> None:
    with pytest.raises(McpTransportError):
        _validate_protocol_version("fixture", "2025-06-18")
    _validate_protocol_version("third-party", "2025-11-25")
    with pytest.raises(McpTransportError):
        _validate_protocol_version(
            "finance", "2025-11-25", FINANCE_PROTOCOL_VERSIONS
        )
    with pytest.raises(McpTransportError):
        _validate_protocol_version("fixture", "2099-01-01")


def test_client_snapshots_a_mutable_protocol_policy() -> None:
    allowed = {"2026-07-28"}
    client = McpClientCore(
        "finance",
        StdioTransport(command="unused", args=[]),
        allowed_protocol_versions=allowed,
    )

    allowed.add("2025-11-25")

    assert client.allowed_protocol_versions == FINANCE_PROTOCOL_VERSIONS


def test_stdio_carries_the_host_context_in_meta() -> None:
    """stdio has no header layer, so `_meta` is the only channel it has."""
    captured: dict[str, object] = {}

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                is_error=False, structured_content={"ok": True}, content=[]
            )

    client = client_with(Session())
    asyncio.run(
        client.call_tool(
            "finance.log_expense",
            {},
            host_context={"Authorization": "Bearer t", "X-Request-ID": "req-1"},
        )
    )
    assert captured["meta"] == {
        "Authorization": "Bearer t",
        "X-Request-ID": "req-1",
    }


def test_a_call_without_a_host_context_sends_no_meta() -> None:
    captured: dict[str, object] = {}

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                is_error=False, structured_content={"ok": True}, content=[]
            )

    asyncio.run(client_with(Session()).call_tool("finance.query_expenses", {}))
    assert captured["meta"] is None


def test_the_caller_cannot_choose_the_channel() -> None:
    # There is one parameter, so a context can neither be sent down both
    # channels nor be dropped by picking the one this transport lacks.
    import inspect

    parameters = inspect.signature(McpClientCore.call_tool).parameters
    assert "host_context" in parameters
    assert "headers" not in parameters
    assert "meta" not in parameters
