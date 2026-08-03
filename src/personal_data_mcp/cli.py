"""Console entrypoint for personal-data-mcp."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from personal_agent_core.write_switch import (
    WriteSwitchConfigError,
    disabled_write_switch,
    load_write_switch,
)
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
        "--restore-read-only",
        action="store_true",
        help=(
            "Start an isolated restore probe with an OS-level read-only "
            "database. Validates the protected config but composes no Finance "
            "adapter, recovery worker or write tools."
        ),
    )
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

    # Resolved before anything else is built, so a host that has no kill switch
    # configured never reaches a socket. The restore probe is pinned off rather
    # than exempted: a recovery host has no switch file, and "no file" must not
    # be the same thing as "no switch".
    if args.restore_read_only:
        write_switch = disabled_write_switch(
            "restore read-only probe: this composition may never write"
        )
    else:
        try:
            write_switch = load_write_switch()
        except WriteSwitchConfigError as error:
            raise SystemExit(str(error)) from error

    session_factory = None
    engine = None
    if args.database is not None:
        from personal_data_mcp.storage.engine import (
            check_integrity,
            create_database_engine,
            create_read_only_database_engine,
        )
        from personal_data_mcp.storage.engine import (
            session_factory as make_session_factory,
        )

        engine = (
            create_read_only_database_engine(args.database)
            if args.restore_read_only
            else create_database_engine(args.database)
        )
        if args.database.exists():
            check_integrity(engine)
        session_factory = make_session_factory(engine)

    if args.restore_read_only:
        if session_factory is None or engine is None or args.ledger_config is None:
            raise SystemExit(
                "--restore-read-only requires --database and --ledger-config"
            )
        # Parse and enforce the protected-config kind without loading Finance
        # credentials or contacting Feishu. The served registry remains meta-only.
        from personal_data_mcp.server.composition import load_protected_config

        load_protected_config(args.ledger_config)
        try:
            uvicorn.run(
                build_app(
                    config,
                    session_factory=session_factory,
                    write_switch=write_switch,
                ),
                host=config.host,
                port=config.port,
                log_level=args.log_level,
                access_log=False,
            )
        finally:
            engine.dispose()
        return

    if args.ledger_config is None:
        uvicorn.run(
            build_app(
                config,
                session_factory=session_factory,
                write_switch=write_switch,
            ),
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
            write_switch=write_switch,
        )
    )


async def _serve_with_finance_tools(
    config, args, *, session_factory, write_switch
) -> None:
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
            record_reader=composed.record_reader,
            data_keyring=composed.dependencies.keyring,
            write_switch=write_switch,
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
