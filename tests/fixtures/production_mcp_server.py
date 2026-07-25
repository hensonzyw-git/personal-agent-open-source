"""Run the real production Finance MCP app in its own process.

Not a fixture server: this is `personal_data_mcp.server.app` served by uvicorn,
so the transport, the GET guard and the header handling under test are the ones
that will be deployed. It is started the same way the deployed unit is, over a
real socket, because the behaviours being verified are transport behaviours.

Run as `python -m fixtures.production_mcp_server <port> [<database>]`. With a
database the internal control API is served too, exactly as the deployed unit
does when started with `--database`; without one only `/mcp` exists.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

from personal_agent_core.crypto import KeyRing, generate_key
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.server.app import build_app, build_registry
from personal_data_mcp.server.config import ServerConfig
from personal_data_mcp.server.finance_write import (
    FinanceWriteDependencies,
    build_expense_handler,
)


LEDGER_FIXTURES = Path(__file__).parent / "ledger"


class SyntheticBitable:
    """A counted Feishu boundary for the cross-service write test."""

    def __init__(self, source: BaseSource) -> None:
        self.source = source
        self.rows: dict[str, dict[str, dict]] = {
            kind: {} for kind in source.tables
        }
        self.fields = copy.deepcopy(
            json.loads(
                (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        self.next_id = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "fixture", "expire": 7200},
            )
        kind = self._table_kind(path)
        if request.method == "GET" and path.endswith("/fields"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": self.fields[kind], "has_more": False},
                },
            )
        if request.method == "POST" and path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"record_id": record_id, "fields": fields}
                            for record_id, fields in self.rows[kind].items()
                        ],
                        "has_more": False,
                    },
                },
            )
        if request.method == "POST" and path.endswith("/records"):
            fields = copy.deepcopy(json.loads(request.content)["fields"])
            record_id = f"rec{self.next_id:06d}"
            self.next_id += 1
            self.rows[kind][record_id] = fields
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {"record_id": record_id, "fields": fields}
                    },
                },
            )
        if request.method == "GET" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": record_id,
                            "fields": self.rows[kind][record_id],
                        }
                    },
                },
            )
        raise AssertionError(
            f"unexpected synthetic Bitable call {request.method} {path}"
        )

    def _table_kind(self, path: str) -> str:
        for kind, table_id in self.source.tables.items():
            if table_id in path:
                return kind
        raise AssertionError(f"unknown synthetic table in {path}")


def write_fixture_registry(sessions):
    """Compose the real expense handler against a synthetic Feishu boundary."""
    config = load_ledger_config(
        json.loads(
            (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
        )
    )
    source = BaseSource(
        base_token=config.base_token,
        ledger_kind=config.ledger_kind,
        tables={kind: table.table_id for kind, table in config.tables.items()},
    )
    fake = SyntheticBitable(source)
    adapter = FeishuAdapter(
        FeishuCredentials(app_id="fixture", app_secret="fixture"),
        transport=httpx.MockTransport(fake.handler),
        now=lambda: 1000.0,
    )
    fx = FxConnector(
        now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "unused"})
        ),
    )
    dependencies = FinanceWriteDependencies(
        adapter=adapter,
        source=source,
        config=config,
        sessions=sessions,
        keyring=KeyRing(
            [generate_key("fixture-data")], service="personal_data_mcp"
        ),
        fx=fx,
        now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    return build_registry(
        expense_write_handler=build_expense_handler(dependencies)
    )


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8811
    config = ServerConfig(host="127.0.0.1", port=port)

    sessions = None
    registry = None
    if len(sys.argv) > 2:
        from personal_data_mcp.storage.engine import (
            create_all,
            create_database_engine,
            session_factory as make_session_factory,
        )

        engine = create_database_engine(Path(sys.argv[2]))
        if len(sys.argv) > 3 and sys.argv[3] == "write-fixture":
            create_all(engine)
        sessions = make_session_factory(engine)
        if len(sys.argv) > 3 and sys.argv[3] == "write-fixture":
            registry = write_fixture_registry(sessions)

    uvicorn.run(
        build_app(config, registry=registry, session_factory=sessions),
        host=config.host,
        port=config.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
