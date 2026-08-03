"""Console entrypoint for personal-agent-api.

The service is composed in one place (`api/composition.py`) and served here.
Nothing is defaulted into existence: the database, both loopback Finance URLs
and the user identity must be stated, and every key comes from the environment.
A missing key, a non-loopback URL or a Finance server that advertises no
manifest-verified tool all fail here, before a socket exists.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from personal_agent.api.app import build_app, build_restore_read_only_app
from personal_agent.api.composition import (
    AgentServiceConfig,
    CompositionError,
    agent_service,
)
from personal_agent_core.write_switch import (
    WriteSwitchConfigError,
    load_write_switch,
)


DEFAULT_FINANCE_CONTROL_URL = "http://127.0.0.1:8811"
DEFAULT_FINANCE_MCP_URL = "http://127.0.0.1:8811/mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8810

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class BindTarget:
    """Where the service listens: exactly one of TCP loopback or a Unix socket."""

    uds: str | None
    host: str | None
    port: int | None


def resolve_bind(host: str | None, port: int | None, socket: str | None) -> BindTarget:
    """Resolve the CLI bind arguments into one target, refusing unsafe mixes.

    Design 3 puts this service behind Nginx on a Unix socket; a public TCP bind
    is a misconfiguration, not an option. The two bind styles are mutually
    exclusive so a deployment cannot half-move to the socket.
    """
    if socket is not None:
        if host is not None or port is not None:
            raise SystemExit("--socket cannot be combined with --host/--port")
        if not Path(socket).is_absolute():
            raise SystemExit("--socket must be an absolute path")
        return BindTarget(uds=socket, host=None, port=None)
    effective_host = host if host is not None else DEFAULT_HOST
    if effective_host not in _LOOPBACK_HOSTS:
        raise SystemExit(
            "personal-agent-api binds loopback only; public exposure is "
            "Nginx's job (DEV-033)"
        )
    return BindTarget(
        uds=None,
        host=effective_host,
        port=port if port is not None else DEFAULT_PORT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Agent Client API against the loopback Finance MCP."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--socket",
        default=None,
        metavar="PATH",
        help=(
            "Absolute path of a Unix domain socket to listen on instead of "
            "loopback TCP (the DEV-032 deployment form, behind Nginx). "
            "Cannot be combined with --host/--port."
        ),
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--restore-read-only",
        action="store_true",
        help=(
            "Start an isolated restore probe: open the existing database "
            "read-only and compose no model, MCP client, recovery worker or "
            "write route. For scripts/restore_drill.sh only."
        ),
    )
    parser.add_argument(
        "--finance-mcp-url",
        default=DEFAULT_FINANCE_MCP_URL,
        help="Loopback Streamable HTTP endpoint of the Finance MCP service.",
    )
    parser.add_argument(
        "--finance-control-url",
        default=DEFAULT_FINANCE_CONTROL_URL,
        help="Loopback base URL of the Finance MCP internal control API.",
    )
    parser.add_argument(
        "--allow-tool",
        action="append",
        default=None,
        dest="allowed_tools",
        help=(
            "Restrict the server-side allowlist to these tools; repeatable. "
            "Omitted means every enabled contract."
        ),
    )
    args = parser.parse_args()

    bind = resolve_bind(args.host, args.port, args.socket)

    if args.restore_read_only:
        _serve_restore_read_only(args.database, args, bind)
        return

    # The kill switch is resolved before the config is built, so a host that
    # has not been told where its switch lives never opens a socket.
    try:
        write_switch = load_write_switch()
    except WriteSwitchConfigError as error:
        raise SystemExit(str(error)) from error

    user_id = os.environ.get("PERSONAL_AGENT_USER_ID", "").strip()
    if not user_id:
        # It names a person and travels into Finance's audit trail, so it is
        # configuration, not a constant this code may choose.
        raise SystemExit("PERSONAL_AGENT_USER_ID must be set")

    # `DEV-031`: the ledger URL the service names to enrolled devices. Optional;
    # absence means the apps offer no "open the ledger" jump. The value itself
    # is validated at composition.
    ledger_url = os.environ.get("PERSONAL_AGENT_LEDGER_URL", "").strip() or None

    config = AgentServiceConfig(
        database=args.database,
        finance_mcp_url=args.finance_mcp_url,
        finance_control_url=args.finance_control_url,
        user_id=user_id,
        allowed_tools=(
            frozenset(args.allowed_tools) if args.allowed_tools else None
        ),
        ledger_url=ledger_url,
    )
    try:
        asyncio.run(_serve(config, args, bind, write_switch=write_switch))
    except CompositionError as exc:
        raise SystemExit(str(exc)) from exc


async def _serve(
    config: AgentServiceConfig, args, bind: BindTarget, *, write_switch
) -> None:
    async with agent_service(config, write_switch=write_switch) as composed:
        if bind.uds is not None:
            server_config = uvicorn.Config(
                build_app(composed.deps),
                uds=bind.uds,
                log_level=args.log_level,
                access_log=False,
            )
        else:
            server_config = uvicorn.Config(
                build_app(composed.deps),
                host=bind.host,
                port=bind.port,
                log_level=args.log_level,
                access_log=False,
            )
        server = uvicorn.Server(server_config)
        await server.serve()


def _serve_restore_read_only(database: Path, args, bind: BindTarget) -> None:
    from personal_agent.storage.engine import (
        check_integrity,
        create_read_only_database_engine,
        session_factory,
    )

    engine = create_read_only_database_engine(database)
    try:
        check_integrity(engine)
        app = build_restore_read_only_app(session_factory(engine))
        kwargs = {
            "log_level": args.log_level,
            "access_log": False,
        }
        if bind.uds is not None:
            uvicorn.run(app, uds=bind.uds, **kwargs)
        else:
            uvicorn.run(app, host=bind.host, port=bind.port, **kwargs)
    finally:
        engine.dispose()
