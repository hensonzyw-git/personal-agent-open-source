"""A deliberately independent third-party MCP server.

DEV-014 requires the same Client Core to discover and call a server implemented
outside this project's Finance path. It therefore shares no code with the
Finance fixture: its own tool names, its own schemas, its own handlers, and one
tool name chosen to collide with Finance so namespace isolation is actually
exercised rather than assumed.

Read-only. It has no write tool at all, so an accidental write cannot be the
thing that passes the interoperability gate.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import Resource, TextContent, Tool
from pydantic import AnyUrl


SERVER_NAME = "third-party-almanac"

TOOLS = [
    Tool(
        name="almanac.sunrise",
        description="Return a fixed sunrise time for a city.",
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["city"],
            "properties": {"city": {"type": "string", "minLength": 1}},
        },
    ),
    # Same business name as the Personal Data server's read tool. The Client
    # Core must keep the two apart by connector, not by name.
    Tool(
        name="finance.query_expenses",
        description="An unrelated tool that happens to share a name.",
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"note": {"type": "string"}},
        },
    ),
]


def build_server(*, host: str = "127.0.0.1", port: int = 0) -> FastMCP:
    server = FastMCP(
        SERVER_NAME, host=host, port=port, json_response=True, stateless_http=True
    )

    @server._mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server._mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name == "almanac.sunrise":
            payload = {"city": arguments["city"], "sunrise": "05:12"}
        elif name == "finance.query_expenses":
            payload = {"served_by": SERVER_NAME, "authoritative": False}
        else:
            raise ValueError(f"unknown tool {name}")
        return [TextContent(type="text", text=json.dumps(payload))]

    @server._mcp_server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl("almanac://readme"),
                name="readme",
                description="Static text, proving resources/list and read work.",
                mimeType="text/plain",
            )
        ]

    @server._mcp_server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        if str(uri) != "almanac://readme":
            raise ValueError(f"unknown resource {uri}")
        return "third-party almanac fixture"

    return server


def main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    server = build_server(port=port)
    server.run(transport="streamable-http" if transport == "http" else "stdio")


if __name__ == "__main__":
    main()
