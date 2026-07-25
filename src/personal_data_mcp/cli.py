"""Console entrypoint for personal-data-mcp."""

from __future__ import annotations

import argparse
import asyncio
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
    parser.add_argument(
        "--ledger-config",
        type=Path,
        default=None,
        help=(
            "Protected annual ledger config. When given, the Finance write "
            "tools are composed and advertised; a non-synthetic_test config is "
            "refused. Requires --database, since every write needs the "
            "execution store."
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

    if args.ledger_config is None:
        uvicorn.run(
            build_app(config, session_factory=session_factory),
            host=config.host,
            port=config.port,
            log_level=args.log_level,
            access_log=False,
        )
        return

    if session_factory is None:
        raise SystemExit(
            "--ledger-config requires --database: a write cannot be governed "
            "without the execution store."
        )
    asyncio.run(
        _serve_with_finance_tools(
            config,
            args,
            session_factory=session_factory,
        )
    )


async def _serve_with_finance_tools(config, args, *, session_factory) -> None:
    """Serve with the Finance write tools composed for the process lifetime.

    The adapter and the FX connector own HTTP clients, so they are opened once
    and closed on shutdown rather than per call. Composition refuses a
    non-synthetic config and an unbound source before a socket exists.
    """
    from personal_data_mcp.server.composition import finance_tools

    async with finance_tools(
        config_path=args.ledger_config, sessions=session_factory
    ) as composed:
        app = build_app(
            config,
            composed.registry,
            session_factory=session_factory,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                log_level=args.log_level,
                access_log=False,
            )
        )
        await server.serve()
