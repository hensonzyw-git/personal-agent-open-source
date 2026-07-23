"""Console entrypoint for personal-data-mcp."""

from __future__ import annotations

import argparse
from pathlib import Path

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
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help=(
            "Finance SQLite path. When given, the internal control API is served "
            "on /internal; without it only the MCP endpoint is exposed."
        ),
    )
    args = parser.parse_args()

    # Constructing the config is the bind guard: a non-loopback host raises
    # here, before a socket exists.
    config = ServerConfig(host=args.host, port=args.port)

    session_factory = None
    if args.database is not None:
        from personal_data_mcp.storage.engine import (
            check_integrity,
            create_database_engine,
        )
        from personal_data_mcp.storage.engine import (
            session_factory as make_session_factory,
        )

        engine = create_database_engine(args.database)
        if args.database.exists():
            check_integrity(engine)
        session_factory = make_session_factory(engine)

    uvicorn.run(
        build_app(config, session_factory=session_factory),
        host=config.host,
        port=config.port,
        log_level=args.log_level,
        access_log=False,
    )
