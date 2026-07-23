"""DEV-015 Commit 1 unit coverage: bind guard, registry, error envelope."""

from __future__ import annotations

import asyncio
import json

import pytest

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.server.app import build_registry, dispatch
from personal_data_mcp.server.config import ServerConfig, ServerConfigError
from personal_data_mcp.server.errors import error_result, success_result
from personal_data_mcp.server.handlers import (
    ToolInvocation,
    ToolRegistrationError,
    ToolRegistry,
)


def run(coro):
    return asyncio.run(coro)


# --- bind guard --------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.1", "192.168.1.10", "::"])
def test_a_non_loopback_bind_is_refused(host) -> None:
    with pytest.raises(ServerConfigError):
        ServerConfig(host=host)


def test_a_hostname_is_refused_even_if_it_would_resolve_to_loopback() -> None:
    # What a name resolves to is not a property of the configuration.
    with pytest.raises(ServerConfigError):
        ServerConfig(host="localhost")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1"])
def test_loopback_addresses_are_accepted(host) -> None:
    assert ServerConfig(host=host).host == host


# --- registry ----------------------------------------------------------------


def test_registering_a_non_contract_tool_is_refused() -> None:
    registry = ToolRegistry()

    async def handler(_: ToolInvocation) -> dict:
        return {}

    with pytest.raises(ToolRegistrationError):
        registry.register("finance.not_a_tool", handler)


def test_registering_a_disabled_tool_is_refused() -> None:
    registry = ToolRegistry()

    async def handler(_: ToolInvocation) -> dict:
        return {}

    # Enabled-only: a disabled contract is not in the registry's contract map.
    with pytest.raises(ToolRegistrationError):
        registry.register("finance.log_expense_batch", handler)


def test_a_tool_without_a_handler_has_no_contract_and_no_catalog_entry() -> None:
    registry = ToolRegistry()

    async def handler(_: ToolInvocation) -> dict:
        return {}

    registry.register("finance.log_expense", handler)
    assert registry.contract("finance.log_expense") is not None
    # An enabled contract that was never registered stays invisible.
    assert registry.contract("finance.log_income") is None
    assert [c["name"] for c in registry.catalog()] == ["finance.log_expense"]


def test_double_registration_is_refused() -> None:
    registry = ToolRegistry()

    async def handler(_: ToolInvocation) -> dict:
        return {}

    registry.register("finance.log_expense", handler)
    with pytest.raises(ToolRegistrationError):
        registry.register("finance.log_expense", handler)


# --- error envelope: no provider text on the wire ----------------------------


def test_error_result_carries_only_the_stable_code_and_fixed_message() -> None:
    detail = "Feishu said: base bascxxx table tblyyy field fldzzz is missing"
    result = error_result(
        AppError(ErrorCode.SOURCE_SCHEMA_CHANGED, internal_detail=detail)
    )
    assert result.isError is True
    assert result.structuredContent is None
    body = json.loads(result.content[0].text)
    assert body["error"]["code"] == "SOURCE_SCHEMA_CHANGED"
    # The internal detail, which names Base/table/field ids, never serialises.
    serialised = json.dumps(body, ensure_ascii=False)
    assert "bascxxx" not in serialised
    assert "tblyyy" not in serialised
    assert "fldzzz" not in serialised


def test_an_unexpected_handler_exception_becomes_internal_error() -> None:
    """A raise inside a handler must not put its text on the wire."""
    registry = build_registry()

    async def explode(_: ToolInvocation) -> dict:
        raise RuntimeError("secret path /var/lib/personal-data-mcp/x.sqlite")

    # Replace the meta handler with one that raises a revealing message.
    registry._handlers["meta.capabilities"] = explode  # type: ignore[attr-defined]
    result = run(dispatch(registry, "meta.capabilities", {}))
    assert result.isError is True
    body = json.loads(result.content[0].text)
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret path" not in result.content[0].text


def test_an_unknown_tool_is_not_allowlisted() -> None:
    registry = build_registry()
    result = run(dispatch(registry, "finance.made_up", {}))
    body = json.loads(result.content[0].text)
    assert body["error"]["code"] == "TOOL_NOT_ALLOWLISTED"


def test_success_result_carries_structured_and_text() -> None:
    result = success_result({"status": "ok", "value": 1})
    assert result.isError is False
    assert result.structuredContent == {"status": "ok", "value": 1}
    assert json.loads(result.content[0].text) == {"status": "ok", "value": 1}
