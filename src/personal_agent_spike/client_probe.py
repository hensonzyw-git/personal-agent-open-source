from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.client import negotiate_auto
from mcp.client.stdio import stdio_client


async def run_probe() -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "personal_agent_spike.fixture_server"],
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await negotiate_auto(session)
            tools_result = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools_result.tools)
            call_result = await session.call_tool(
                "finance.log_expense",
                arguments={
                    "name": "午饭",
                    "amount": "45",
                    "category": "餐饮",
                    "occurred_on": "2026-07-23",
                    "is_family_expense": False,
                },
            )
            if call_result.is_error:
                raise RuntimeError("fixture MCP tool returned an error")
            return {
                "protocol_version": session.protocol_version,
                "tool_names": tool_names,
                "structured_result": call_result.structured_content,
            }


def main() -> None:
    print(json.dumps(asyncio.run(run_probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
