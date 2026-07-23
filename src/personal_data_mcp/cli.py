"""Console entrypoint for personal-data-mcp."""

from __future__ import annotations

import argparse

import uvicorn

from personal_data_mcp.server.app import build_app
from personal_data_mcp.server.config import ServerConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the loopback Finance MCP server."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8811)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    # Constructing the config is the bind guard: a non-loopback host raises
    # here, before a socket exists.
    config = ServerConfig(host=args.host, port=args.port)
    uvicorn.run(
        build_app(config),
        host=config.host,
        port=config.port,
        log_level=args.log_level,
        access_log=False,
    )
