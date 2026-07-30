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
from pathlib import Path

import uvicorn

from personal_agent.api.app import build_app
from personal_agent.api.composition import (
    AgentServiceConfig,
    CompositionError,
    agent_service,
)


DEFAULT_FINANCE_CONTROL_URL = "http://127.0.0.1:8811"
DEFAULT_FINANCE_MCP_URL = "http://127.0.0.1:8811/mcp"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Agent Client API against the loopback Finance MCP."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8810)
    parser.add_argument("--log-level", default="info")
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

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        # Design 3 puts this service behind Nginx on a Unix socket; the socket
        # itself is `DEV-032`. Until then a public bind is a misconfiguration,
        # not an option.
        raise SystemExit(
            "personal-agent-api binds loopback only; public exposure is "
            "Nginx's job (DEV-033)"
        )

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
        asyncio.run(_serve(config, args))
    except CompositionError as exc:
        raise SystemExit(str(exc)) from exc


async def _serve(config: AgentServiceConfig, args) -> None:
    async with agent_service(config) as composed:
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(composed.deps),
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                access_log=False,
            )
        )
        await server.serve()
