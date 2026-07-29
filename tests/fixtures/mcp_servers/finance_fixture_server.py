"""A Finance MCP fixture served over stdio or loopback Streamable HTTP.

The tools come from the generated manifest rather than being written out again,
so the fixture cannot drift from the contract. It performs no I/O beyond
returning deterministic receipts: no Feishu, no credentials, no network.

`FIXTURE_TOOL_DELAY_SECONDS` makes a tool call take that long before answering.
An instant counterparty is the one thing every fake here has in common, and it
is exactly what hid a transport read timeout until the first live write: the
real Finance handler talks to Feishu several times, and a stream torn down
mid-call turns a completed write into an unknown commit.

Run as `python -m fixtures.mcp_servers.finance_fixture_server [stdio|http] [port]`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from fixtures.finance_fixture import fixture_catalog, fixture_receipt


SERVER_NAME = "personal-data-mcp-fixture"


def build_server(*, host: str = "127.0.0.1", port: int = 0) -> Server:
    catalog = fixture_catalog()
    delay = float(os.environ.get("FIXTURE_TOOL_DELAY_SECONDS", "0") or 0)

    async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=entry["name"],
                    description=entry["description"],
                    input_schema=entry["input_schema"],
                )
                for entry in catalog
            ]
        )

    async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        known = {entry["name"] for entry in catalog}
        if name not in known:
            raise ValueError(f"unknown tool {name}")
        if delay:
            await asyncio.sleep(delay)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        fixture_receipt(name, arguments), ensure_ascii=False
                    ),
                )
            ],
            structured_content=fixture_receipt(name, arguments),
        )

    return Server(
        SERVER_NAME,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    server = build_server(port=port)
    if transport == "http":
        import uvicorn

        app = server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    else:
        from mcp import stdio_server

        async def run_stdio() -> None:
            async with stdio_server() as (read, write):
                await server.run(
                    read, write, server.create_initialization_options()
                )

        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
