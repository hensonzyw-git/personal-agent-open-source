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

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable, Final

from personal_agent_core.fault_breakpoint import FaultBreakpoint
from personal_agent_core.timeutil import utc_now
from personal_data_mcp.crypto.keys import load_data_keyring
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    PRODUCTION_KIND,
    SYNTHETIC_TEST_KIND,
    LedgerSourceError,
    load_base_source,
    production_write_allowed,
    require_synthetic_test_base,
    require_write_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.ledger_config import LedgerConfig, load_ledger_config
from personal_data_mcp.finance.query_cursor_secret import load_query_cursor_secret
from personal_data_mcp.finance.reconciler import reconcile_write
from personal_data_mcp.server.app import build_registry
from personal_data_mcp.server.control import RecordReader
from personal_data_mcp.server.finance_write import (
    FinanceWriteDependencies,
    build_expense_handler,
    build_category_update_handler,
    build_family_fund_handler,
    build_income_handler,
    fresh_validation,
)
from personal_data_mcp.server.expense_query import (
    ExpenseQueryDependencies,
    build_handler as build_expense_query_handler,
)
from personal_data_mcp.server.handlers import ToolRegistry
from personal_data_mcp.server.record_reader import build_record_reader
from personal_data_mcp.storage.execution_store import scan_unfinished


logger = logging.getLogger(__name__)

RECOVERY_INTERVAL_SECONDS: Final[float] = 60.0
RECOVERY_SCAN_LIMIT: Final[int] = 100


@dataclass(frozen=True)
class FinanceComposition:
    """The live dependencies, the registry, and the control-plane record read."""

    dependencies: FinanceWriteDependencies
    registry: ToolRegistry
    #: `DEV-028`: reads a written record's current values for a review card. It
    #: is a control-plane read, never an MCP tool, so the model cannot reach it.
    record_reader: RecordReader


async def recover_unfinished(
    dependencies: FinanceWriteDependencies,
    *,
    owner: str,
    limit: int = RECOVERY_SCAN_LIMIT,
) -> list[tuple[str, str]]:
    """Drive one snapshot of unfinished Finance executions toward truth.

    The database read ends before schema or Feishu network activity begins. Each
    execution then acquires its own durable lease inside ``reconcile_write``, so
    a concurrent or restarted worker either owns the recovery or leaves it for
    the next bounded scan. One bad execution cannot suppress recovery of the
    rest, and log messages never include the idempotency key.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    with dependencies.sessions() as session:
        keys = [
            row.idempotency_key
            for row in scan_unfinished(session, limit=limit)
        ]
    if not keys:
        return []

    results: list[tuple[str, str]] = []
    for key in keys:
        try:
            # Schema evidence is per execution, not per scan. A recovery can
            # spend seconds at each provider boundary; reusing one snapshot for
            # every row would let a Feishu schema change after the first row go
            # unnoticed by every later write in this slice.
            current_validation = await fresh_validation(dependencies)
            result = await reconcile_write(
                key,
                sessions=dependencies.sessions,
                adapter=dependencies.adapter,
                source=dependencies.source,
                config=dependencies.config,
                validation=current_validation,
                keyring=dependencies.keyring,
                owner=owner,
                now=dependencies.now,
            )
        except Exception as exc:  # noqa: BLE001 - retry next bounded scan
            # Exception messages and tracebacks can carry an idempotency key or
            # provider detail. The durable row and DEV-034 report hold the
            # diagnosis; the ordinary journal gets only the exception class.
            logger.warning(
                "Finance recovery left one execution unfinished (%s); "
                "retrying next interval",
                type(exc).__name__,
            )
            continue
        results.append((key, result.final_state))
    return results


async def _recover_periodically(
    dependencies: FinanceWriteDependencies,
    *,
    owner: str,
    interval_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Run Finance recovery for the service lifetime without overlapping scans."""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            await recover_unfinished(dependencies, owner=owner)
        except Exception as exc:  # noqa: BLE001 - the next interval must survive
            logger.warning(
                "Finance recovery scan could not run (%s); retrying next interval",
                type(exc).__name__,
            )


def load_protected_config(
    path: Path, *, allow_production: bool = False
) -> LedgerConfig:
    """Read the frozen annual config, or refuse to serve Finance tools.

    `allow_production` is the G5 switch: when False (the default, and the only
    value before G5) a config whose kind is not `synthetic_test` is refused
    outright; when True, a `production` config is accepted. The synthetic-only
    gate stays the default so that nothing on the write path opens for the real
    ledger without the explicit authorisation.
    """
    config = load_ledger_config(json.loads(path.read_text(encoding="utf-8")))
    if config.ledger_kind == SYNTHETIC_TEST_KIND:
        return config
    if allow_production and config.ledger_kind == PRODUCTION_KIND:
        return config
    raise LedgerSourceError(
        f"refusing to serve Finance write tools with a "
        f"{config.ledger_kind!r} ledger config: a production write is a G5 "
        "decision and requires PERSONAL_AGENT_ALLOW_PRODUCTION_WRITE=1"
    )


@asynccontextmanager
async def finance_tools(
    *,
    config_path: Path,
    sessions,
    now: Callable[[], datetime] = utc_now,
    recovery_interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
    fault_breakpoint: FaultBreakpoint | None = None,
) -> AsyncIterator[FinanceComposition]:
    """Open the Finance write surface for the lifetime of the service."""
    if recovery_interval_seconds <= 0:
        raise ValueError("recovery_interval_seconds must be positive")
    allow_production = production_write_allowed()
    config = load_protected_config(config_path, allow_production=allow_production)
    query_cursor_secret = load_query_cursor_secret(required=False)
    credentials = load_credentials()
    # The binding is chosen by the same switch that admitted the config: a
    # synthetic config always binds through `require_synthetic_test_base`; a
    # production config -- reachable only with the explicit G5 switch on -- binds
    # through the write-path authorisation. Keeping them as two named functions
    # means the synthetic path never silently acquires a production binding.
    source_loader = (
        require_write_base
        if allow_production and config.ledger_kind == PRODUCTION_KIND
        else require_synthetic_test_base
    )
    source = source_loader(
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
            fault_breakpoint=fault_breakpoint,
        )
        # Fail at boot rather than at the first write. This validation is
        # deliberately discarded: the handlers revalidate inside each call.
        await fresh_validation(dependencies)
        query_handler = (
            build_expense_query_handler(
                ExpenseQueryDependencies(
                    adapter=adapter,
                    source=source,
                    config=config,
                    validate_schema=lambda: fresh_validation(dependencies),
                    cursor_secret=query_cursor_secret,
                )
            )
            if query_cursor_secret is not None
            else None
        )
        registry = build_registry(
            expense_query_handler=query_handler,
            expense_write_handler=build_expense_handler(dependencies),
            income_write_handler=build_income_handler(dependencies),
            family_fund_handler=build_family_fund_handler(dependencies),
            category_update_handler=build_category_update_handler(dependencies),
        )
        recovery_owner = f"finance-recovery-{uuid.uuid4()}"
        recovery_stop = asyncio.Event()
        recovery_task: asyncio.Task[None] | None = None
        try:
            # Design 7.6.2: recover before accepting new writes, then every
            # minute for executions created or stranded during this process.
            await recover_unfinished(
                dependencies,
                owner=recovery_owner,
            )
            recovery_task = asyncio.create_task(
                _recover_periodically(
                    dependencies,
                    owner=recovery_owner,
                    interval_seconds=recovery_interval_seconds,
                    stop=recovery_stop,
                ),
                name="finance-recovery",
            )
            yield FinanceComposition(
                dependencies=dependencies,
                registry=registry,
                record_reader=build_record_reader(dependencies),
            )
        finally:
            recovery_stop.set()
            if recovery_task is not None:
                # Do not close the shared Feishu client underneath an in-flight
                # recovery call. Setting the event stops the next interval;
                # awaiting the task lets the bounded current scan finish.
                await recovery_task
    finally:
        await fx.aclose()
        await adapter.aclose()


def _utc_clock(now: Callable[[], datetime]) -> Callable[[], datetime]:
    """The FX connector wants aware UTC instants, like everything else here."""

    def clock() -> datetime:
        return now()

    return clock
