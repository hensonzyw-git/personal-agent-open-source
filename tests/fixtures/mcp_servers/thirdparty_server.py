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

from mcp.server.lowlevel import Server
from mcp.types import (
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)


SERVER_NAME = "third-party-almanac"

TOOLS = [
    Tool(
        name="almanac.sunrise",
        description="Return a fixed sunrise time for a city.",
        input_schema={
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
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"note": {"type": "string"}},
        },
    ),
]


def build_server(*, host: str = "127.0.0.1", port: int = 0) -> Server:
    async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)

    async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        if name == "almanac.sunrise":
            payload = {"city": arguments["city"], "sunrise": "05:12"}
        elif name == "finance.query_expenses":
            payload = {"served_by": SERVER_NAME, "authoritative": False}
        else:
            raise ValueError(f"unknown tool {name}")
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

    async def on_list_resources(ctx: Any, params: Any) -> ListResourcesResult:
        return ListResourcesResult(
            resources=[
                Resource(
                    uri="almanac://readme",
                    name="readme",
                    description="Static text, proving resources/list and read work.",
                    mime_type="text/plain",
                )
            ]
        )

    async def on_read_resource(ctx: Any, params: Any) -> ReadResourceResult:
        if params.uri != "almanac://readme":
            raise ValueError(f"unknown resource {params.uri}")
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri="almanac://readme",
                    mime_type="text/plain",
                    text="third-party almanac fixture",
                )
            ]
        )

    return Server(
        SERVER_NAME,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
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
        import asyncio

        from mcp import stdio_server

        async def run_stdio() -> None:
            async with stdio_server() as (read, write):
                await server.run(
                    read, write, server.create_initialization_options()
                )

        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
