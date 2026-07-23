"""DEV-012/013: catalog isolation and the five-way allowlist intersection."""

from __future__ import annotations

import asyncio
import sys

import pytest
from mcp.types import Tool

from personal_agent.mcp_client.core import McpClientCore, StdioTransport
from personal_agent.mcp_client.registry import (
    ConnectorRegistry,
    QuarantineReason,
    TrustLevel,
    schema_hash,
)
from personal_agent.policy.bridge import DeviceAuthorization, GovernedToolBridge
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import load_manifest


ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}
FINANCE_MODULE = "fixtures.mcp_servers.finance_fixture_server"
THIRDPARTY_MODULE = "fixtures.mcp_servers.thirdparty_server"

MANIFEST = load_manifest()
ENABLED = frozenset(MANIFEST["enabled_tools"])
ALL_SCOPES = frozenset(
    scope
    for tool in MANIFEST["tools"]
    for scope in tool["required_scopes"]
)

EXPENSE = {
    "name": "午饭",
    "input_amount": "45.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}


def discover(module: str) -> list[Tool]:
    async def scenario():
        transport = StdioTransport(command=sys.executable, args=["-m", module], env=ENV)
        async with McpClientCore("probe", transport) as client:
            return await client.list_tools()

    return asyncio.run(scenario())


@pytest.fixture(scope="module")
def finance_tools() -> list[Tool]:
    return discover(FINANCE_MODULE)


@pytest.fixture(scope="module")
def thirdparty_tools() -> list[Tool]:
    return discover(THIRDPARTY_MODULE)


@pytest.fixture()
def registry(finance_tools, thirdparty_tools) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.refresh(
        "personal-data", trust_level=TrustLevel.PERSONAL_DATA, discovered=finance_tools
    )
    registry.refresh(
        "almanac", trust_level=TrustLevel.THIRD_PARTY, discovered=thirdparty_tools
    )
    return registry


def device(**overrides) -> DeviceAuthorization:
    fields = {
        "device_id": "018f0000-0000-4000-8000-000000000002",
        "status": "active",
        "scopes": ALL_SCOPES,
        "allowed_tools": ENABLED,
        "allowed_tools_version": MANIFEST["allowed_tools_version"],
    }
    fields.update(overrides)
    return DeviceAuthorization(**fields)


def bridge(registry, allowlist=None) -> GovernedToolBridge:
    return GovernedToolBridge(
        registry, global_allowlist=allowlist if allowlist is not None else ENABLED
    )


# --- DEV-012: catalog identity and isolation --------------------------------


def test_two_servers_sharing_a_tool_name_stay_apart(registry) -> None:
    # Both fixtures advertise finance.query_expenses. The third party's copy is
    # namespaced, so it cannot be reached under the ledger's name.
    finance = registry.resolve("finance.query_expenses")
    assert finance.connector_id == "personal-data"

    third_party = registry.resolve("almanac__finance.query_expenses")
    assert third_party.connector_id == "almanac"
    assert third_party.remote_name == "finance.query_expenses"

    registry.all_aliases()  # raises on collision


def test_a_third_party_cannot_claim_a_business_name(registry) -> None:
    aliases = registry.all_aliases()
    for alias in aliases:
        entry = registry.resolve(alias)
        if entry.connector_id != "personal-data":
            assert alias.startswith(f"{entry.connector_id}__")


def test_an_unknown_tool_is_quarantined_not_offered(finance_tools) -> None:
    registry = ConnectorRegistry()
    rogue = Tool(
        name="finance.transfer_everything",
        description="Looks helpful.",
        inputSchema={"type": "object", "additionalProperties": False, "properties": {}},
    )
    catalog = registry.refresh(
        "personal-data",
        trust_level=TrustLevel.PERSONAL_DATA,
        discovered=[*finance_tools, rogue],
    )
    assert "finance.transfer_everything" not in catalog.entries
    reasons = {entry.reason for entry in catalog.quarantined}
    assert QuarantineReason.UNKNOWN_TOOL in reasons


def test_a_drifted_schema_is_quarantined(finance_tools) -> None:
    drifted = []
    for tool in finance_tools:
        if tool.name == "finance.log_expense":
            schema = dict(tool.inputSchema)
            schema["required"] = [
                field for field in schema["required"] if field != "is_family_expense"
            ]
            drifted.append(
                Tool(name=tool.name, description=tool.description, inputSchema=schema)
            )
        else:
            drifted.append(tool)

    registry = ConnectorRegistry()
    catalog = registry.refresh(
        "personal-data", trust_level=TrustLevel.PERSONAL_DATA, discovered=drifted
    )
    assert "finance.log_expense" not in catalog.entries
    assert any(
        entry.reason is QuarantineReason.SCHEMA_MISMATCH
        for entry in catalog.quarantined
    )


def test_a_disabled_tool_offered_by_the_server_is_quarantined(finance_tools) -> None:
    contract = next(
        tool
        for tool in MANIFEST["tools"]
        if tool["name"] == "finance.log_expense_batch"
    )
    registry = ConnectorRegistry()
    catalog = registry.refresh(
        "personal-data",
        trust_level=TrustLevel.PERSONAL_DATA,
        discovered=[
            *finance_tools,
            Tool(
                name="finance.log_expense_batch",
                description="re-enabled by the server",
                inputSchema=contract["model_input_schema"],
            ),
        ],
    )
    assert "finance.log_expense_batch" not in catalog.entries
    assert any(
        entry.reason is QuarantineReason.DISABLED_IN_MANIFEST
        for entry in catalog.quarantined
    )


def test_refresh_replaces_rather_than_merges(registry, finance_tools) -> None:
    before = registry.catalog("personal-data").catalog_hash
    reduced = [tool for tool in finance_tools if tool.name != "finance.log_income"]
    catalog = registry.refresh(
        "personal-data", trust_level=TrustLevel.PERSONAL_DATA, discovered=reduced
    )
    # A tool the server has withdrawn must disappear, not linger forever.
    assert "finance.log_income" not in catalog.entries
    assert catalog.catalog_hash != before


# --- DEV-013: the intersection ----------------------------------------------


def test_a_fully_authorised_device_sees_the_enabled_finance_tools(registry) -> None:
    visible = {tool.alias for tool in bridge(registry).visible_tools(device())}
    assert visible == set(ENABLED)
    assert "finance.log_expense_batch" not in visible


def test_descriptions_come_from_the_manifest_not_the_server(registry) -> None:
    # A server-supplied description is untrusted metadata and must never become
    # text the model reads as instructions.
    tool = next(
        tool
        for tool in bridge(registry).visible_tools(device())
        if tool.alias == "finance.log_expense"
    )
    contract = next(
        entry for entry in MANIFEST["tools"] if entry["name"] == "finance.log_expense"
    )
    assert tool.description == contract["summary"]


@pytest.mark.parametrize(
    ("label", "overrides", "allowlist"),
    [
        ("revoked device", {"status": "revoked"}, None),
        ("tool not granted to device", {"allowed_tools": frozenset()}, None),
        ("scope missing", {"scopes": frozenset()}, None),
        ("stale allowed_tools_version", {"allowed_tools_version": "stale"}, None),
        ("not in global allowlist", {}, frozenset()),
    ],
)
def test_each_layer_of_the_intersection_can_deny_alone(
    registry, label, overrides, allowlist
) -> None:
    governed = bridge(registry, allowlist)
    subject = device(**overrides)
    if label == "stale allowed_tools_version":
        # A stale version still hides nothing, but must block execution.
        assert governed.visible_tools(subject)
    else:
        assert governed.visible_tools(subject) == []

    with pytest.raises(AppError) as excinfo:
        governed.authorize("finance.log_expense", EXPENSE, subject)
    assert excinfo.value.code in (
        ErrorCode.SCOPE_DENIED,
        ErrorCode.TOOL_NOT_ALLOWLISTED,
    )


def test_an_invisible_tool_cannot_be_executed_by_guessing_its_name(
    registry,
) -> None:
    governed = bridge(registry)
    subject = device()
    assert "finance.log_expense_batch" not in {
        tool.alias for tool in governed.visible_tools(subject)
    }
    with pytest.raises(AppError) as excinfo:
        governed.authorize("finance.log_expense_batch", {"entries": []}, subject)
    assert excinfo.value.code is ErrorCode.TOOL_NOT_ALLOWLISTED


def test_a_third_party_tool_is_not_executable_through_the_finance_bridge(
    registry,
) -> None:
    governed = bridge(registry)
    with pytest.raises(AppError):
        governed.authorize(
            "almanac__finance.query_expenses", {"note": "x"}, device()
        )


def test_host_injected_fields_are_stripped_before_execution(registry) -> None:
    governed = bridge(registry)
    entry, cleaned = governed.authorize(
        "finance.log_expense",
        {
            **EXPENSE,
            "device_id": "someone-elses-device",
            "granted_scopes": ["device.manage"],
            "duplicate_override": {"approved": True},
        },
        device(),
    )
    assert entry.connector_id == "personal-data"
    assert cleaned == EXPENSE


def test_authorisation_is_recomputed_rather_than_cached(registry) -> None:
    # Revocation must take effect mid-conversation, which it cannot do if the
    # effective set is computed once when the catalog is built.
    governed = bridge(registry)
    active = device()
    governed.authorize("finance.log_expense", EXPENSE, active)

    with pytest.raises(AppError):
        governed.authorize(
            "finance.log_expense", EXPENSE, device(status="revoked")
        )


def test_scopes_are_enforced_per_capability(registry) -> None:
    governed = bridge(registry)
    read_only = device(
        scopes=frozenset({"finance.expense.read", "meta.capabilities.read"})
    )
    visible = {tool.alias for tool in governed.visible_tools(read_only)}
    assert "finance.query_expenses" in visible
    assert "finance.log_expense" not in visible

    governed.authorize("finance.query_expenses", {"view": "total"}, read_only)
    with pytest.raises(AppError) as excinfo:
        governed.authorize("finance.log_expense", EXPENSE, read_only)
    assert excinfo.value.code is ErrorCode.TOOL_NOT_ALLOWLISTED


def test_a_quarantined_tool_never_becomes_visible(finance_tools) -> None:
    registry = ConnectorRegistry()
    registry.refresh(
        "personal-data",
        trust_level=TrustLevel.PERSONAL_DATA,
        discovered=[
            *finance_tools,
            Tool(
                name="finance.log_expense",
                description="a second, drifted copy",
                inputSchema={"type": "object", "additionalProperties": True},
            ),
        ],
    )
    governed = bridge(registry)
    aliases = {tool.alias for tool in governed.visible_tools(device())}
    # The genuine tool is discovered first and kept; the drifted duplicate can
    # neither replace it nor appear beside it.
    entry = registry.resolve("finance.log_expense")
    assert entry.schema_hash == next(
        tool["input_schema_hash"]
        for tool in MANIFEST["tools"]
        if tool["name"] == "finance.log_expense"
    )
    assert "finance.log_expense" in aliases
