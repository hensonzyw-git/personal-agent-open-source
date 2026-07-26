"""Where the Finance MCP service actually gets its credentials and config.

Every handler in this service is written so that it *cannot* load anything: it
receives an adapter, a bound source, a protected config and a key ring. That
discipline is only worth something if there is exactly one place where those are
loaded, and this is it.

The refusals here are the ones that keep "synthetic test Base only until G5" a
property of the code rather than of the operator's memory:

- the protected config is read from an explicit path, and a config whose
  `ledger_kind` is not `synthetic_test` is refused outright. Production is a G5
  decision, not a flag;
- the source is bound with `require_synthetic_test_base`, which compares the
  environment's kind, Base token and complete three-table mapping against that
  same config. A partial match is a refusal;
- the live schema is validated once at startup, purely so a misconfigured
  service fails at boot rather than at the first write. It is **not** the
  validation the write path uses: each call revalidates, because a payload
  addressed by field name is only safe while the ids still carry those names.

Nothing is enabled implicitly. A service started without a ledger config serves
the credential-free MCP surface, exactly as it did before this module existed.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from personal_agent_core.timeutil import utc_now
from personal_data_mcp.crypto.keys import load_data_keyring
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    SYNTHETIC_TEST_KIND,
    LedgerSourceError,
    load_base_source,
    require_synthetic_test_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.ledger_config import LedgerConfig, load_ledger_config
from personal_data_mcp.server.finance_write import (
    FinanceWriteDependencies,
    fresh_validation,
    build_expense_handler,
    build_family_fund_handler,
    build_income_handler,
)
from personal_data_mcp.server.control import RecordReader
from personal_data_mcp.server.handlers import ToolRegistry
from personal_data_mcp.server.record_reader import build_record_reader
from personal_data_mcp.server.app import build_registry


@dataclass(frozen=True)
class FinanceComposition:
    """The live dependencies, the registry, and the control-plane record read."""

    dependencies: FinanceWriteDependencies
    registry: ToolRegistry
    #: `DEV-028`: reads a written record's current values for a review card. It
    #: is a control-plane read, never an MCP tool, so the model cannot reach it.
    record_reader: RecordReader


def load_protected_config(path: Path) -> LedgerConfig:
    """Read the frozen annual config, or refuse to serve Finance tools."""
    config = load_ledger_config(
        json.loads(path.read_text(encoding="utf-8"))
    )
    if config.ledger_kind != SYNTHETIC_TEST_KIND:
        raise LedgerSourceError(
            f"refusing to serve Finance write tools with a "
            f"{config.ledger_kind!r} ledger config: production is a G5 decision"
        )
    return config


@asynccontextmanager
async def finance_tools(
    *,
    config_path: Path,
    sessions,
    now: callable = utc_now,
) -> AsyncIterator[FinanceComposition]:
    """Open the Finance write surface for the lifetime of the service."""
    config = load_protected_config(config_path)
    credentials = load_credentials()
    source = require_synthetic_test_base(
        load_base_source(),
        approved_base_token=config.base_token,
        approved_tables={
            kind: table.table_id for kind, table in config.tables.items()
        },
        approved_ledger_kind=config.ledger_kind,
    )

    adapter = FeishuAdapter(credentials, now=time.monotonic)
    fx = FxConnector(now=_utc_clock(now))
    try:
        dependencies = FinanceWriteDependencies(
            adapter=adapter,
            source=source,
            config=config,
            sessions=sessions,
            keyring=load_data_keyring(),
            fx=fx,
            now=now,
        )
        # Fail at boot rather than at the first write. This validation is
        # deliberately discarded: the handlers revalidate inside each call.
        await fresh_validation(dependencies)
        registry = build_registry(
            expense_write_handler=build_expense_handler(dependencies),
            income_write_handler=build_income_handler(dependencies),
            family_fund_handler=build_family_fund_handler(dependencies),
        )
        yield FinanceComposition(
            dependencies=dependencies,
            registry=registry,
            record_reader=build_record_reader(dependencies),
        )
    finally:
        await fx.aclose()
        await adapter.aclose()


def _utc_clock(now) -> callable:
    """The FX connector wants aware UTC instants, like everything else here."""

    def clock() -> datetime:
        return now()

    return clock
