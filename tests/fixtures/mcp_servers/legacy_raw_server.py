"""A minimal raw JSON-RPC server speaking MCP 2025-11-25 (legacy).

Implemented with plain Starlette/Uvicorn — no MCP SDK at all — so it is a
genuinely independent counterparty (AGENTS.md §5.1: "Never let a fake be the
only counterparty"). The v2 client's `negotiate_auto` probes `server/discover`
first; this server does not implement it, so the client falls back to
`initialize` and negotiates `2025-11-25`.

Run as `python -m fixtures.mcp_servers.legacy_raw_server [port]`.
"""

from __future__ import annotations

import json
import sys

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _tool() -> dict:
    return {
        "name": "legacy.echo",
        "description": "Echo the arguments back as a structured receipt.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
    }


async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "legacy-raw-server", "version": "0.1"},
                },
            }
        )

    if method == "tools/list":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [_tool()]}}
        )

    if method == "tools/call":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"echoed": True})}
                    ],
                    "isError": False,
                    "structuredContent": {"echoed": True},
                },
            }
        )

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    )


def build_app() -> Starlette:
    return Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])])


def main() -> None:
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
