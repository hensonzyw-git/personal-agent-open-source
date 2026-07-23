from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings
from importlib.metadata import version
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters


FIXTURE_ARGS = ["-m", "personal_agent_spike.fixture_server"]


async def probe_adk_mcp() -> dict[str, Any]:
    connection = StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=FIXTURE_ARGS,
        ),
        timeout=5.0,
    )
    toolset = McpToolset(connection_params=connection)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tools = await toolset.get_tools()
        return {
            "sdk_version": version("google-adk"),
            "mcp_tool_discovery": True,
            "tool_names": sorted(tool.name for tool in tools),
            "model_call_attempted": False,
        }
    finally:
        await toolset.close()


def probe_claude_sdk_config() -> dict[str, Any]:
    options = ClaudeAgentOptions(
        mcp_servers={
            "personal-agent-fixture": {
                "type": "stdio",
                "command": sys.executable,
                "args": FIXTURE_ARGS,
            }
        },
        allowed_tools=[
            "mcp__personal-agent-fixture__finance.log_expense",
            "mcp__personal-agent-fixture__meta.get_capabilities",
        ],
        max_turns=1,
    )
    ClaudeSDKClient(options=options)
    return {
        "sdk_version": version("claude-agent-sdk"),
        "mcp_config_constructed": True,
        "configured_servers": sorted(options.mcp_servers),
        "allowed_tools": options.allowed_tools,
        "client_constructed": True,
        "client_connected": False,
        "model_call_attempted": False,
        "boundary": (
            "The SDK does not expose MCP discovery before starting its bundled "
            "agent process; connect/query is deferred until a model credential is configured."
        ),
    }


async def run_probe() -> dict[str, Any]:
    return {
        "credentials_present": {
            "zai": bool(os.getenv("ZAI_API_KEY")),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        },
        "shared_dependencies": {
            "mcp": version("mcp"),
            "litellm": version("litellm"),
        },
        "google_adk": await probe_adk_mcp(),
        "claude_agent_sdk": probe_claude_sdk_config(),
        "scope": (
            "Offline framework readiness only. No model quality, latency, token, "
            "cost, streaming, or GLM compatibility claim is made."
        ),
    }


def main() -> None:
    print(json.dumps(asyncio.run(run_probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
