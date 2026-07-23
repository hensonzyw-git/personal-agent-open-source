"""`meta.capabilities`: the only tool this server can execute without a ledger.

Progressive disclosure, per the IR contract. It reports the tools this build can
actually run, so it stays truthful as later tasks register connectors, rather
than restating the contract set and implying capability that does not exist yet.
"""

from __future__ import annotations

from typing import Any

from personal_agent_core.manifest import sha256_of
from personal_data_mcp.server.handlers import ToolInvocation, ToolRegistry


TOOL_NAME = "meta.capabilities"


def capabilities_payload(registry: ToolRegistry) -> dict[str, Any]:
    """The reported capability set and a version derived from it.

    `allowed_tools_version` is computed over what is reported, using the same
    formula the manifest uses over the enabled set. The two values converge once
    every enabled contract has a handler, and differ meaningfully before that,
    which is the signal worth having.
    """
    tools = [
        {
            "name": entry["name"],
            "summary": entry["summary"],
            "risk_level": entry["risk_level"],
            "contract_version": entry["version"],
        }
        for entry in (
            registry.contract(name) for name in sorted(registry.names())
        )
        if entry is not None
    ]
    return {
        "status": "ok",
        "tools": tools,
        "allowed_tools_version": sha256_of(
            [
                {
                    "name": entry["name"],
                    "contract_hash": registry.contract(entry["name"])[
                        "contract_hash"
                    ],
                }
                for entry in tools
            ]
        ),
    }


def build_handler(registry: ToolRegistry):
    async def handle(invocation: ToolInvocation) -> dict[str, Any]:
        return capabilities_payload(registry)

    return handle
