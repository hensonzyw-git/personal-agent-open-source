"""Run the real production Finance MCP app in its own process.

Not a fixture server: this is `personal_data_mcp.server.app` served by uvicorn,
so the transport, the GET guard and the header handling under test are the ones
that will be deployed. It is started the same way the deployed unit is, over a
real socket, because the behaviours being verified are transport behaviours.

Run as `python -m fixtures.production_mcp_server <port>`.
"""

from __future__ import annotations

import sys

import uvicorn

from personal_data_mcp.server.app import build_app
from personal_data_mcp.server.config import ServerConfig


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8811
    config = ServerConfig(host="127.0.0.1", port=port)
    uvicorn.run(
        build_app(config),
        host=config.host,
        port=config.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
