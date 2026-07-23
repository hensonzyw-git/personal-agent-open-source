"""A Finance MCP fixture served over stdio or loopback Streamable HTTP.

The tools come from the generated manifest rather than being written out again,
so the fixture cannot drift from the contract. It performs no I/O beyond
returning deterministic receipts: no Feishu, no credentials, no network.

Run as `python -m fixtures.mcp_servers.finance_fixture_server [stdio|http] [port]`.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, Tool

from fixtures.finance_fixture import fixture_catalog, fixture_receipt


SERVER_NAME = "personal-data-mcp-fixture"


def build_server(*, host: str = "127.0.0.1", port: int = 0) -> FastMCP:
    server = FastMCP(
        SERVER_NAME,
        host=host,
        port=port,
        json_response=True,
        stateless_http=True,
    )
    catalog = fixture_catalog()

    @server._mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=entry["name"],
                description=entry["description"],
                inputSchema=entry["inputSchema"],
            )
            for entry in catalog
        ]

    @server._mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        known = {entry["name"] for entry in catalog}
        if name not in known:
            raise ValueError(f"unknown tool {name}")
        import json

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    fixture_receipt(name, arguments), ensure_ascii=False
                ),
            )
        ]

    return server


def main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    server = build_server(port=port)
    if transport == "http":
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
