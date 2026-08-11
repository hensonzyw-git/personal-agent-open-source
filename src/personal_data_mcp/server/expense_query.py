"""Bind the verified MCP invocation to the DEV-022 Finance query core.

The dependencies are injected by service composition instead of loaded inside a
tool handler.  In particular, a handler must never open ``.env.finance.local``
or guess a protected config path: the service owns credential/config loading and
only supplies a source that has already been bound to the protected ledger.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.query_expenses import query_expenses
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.server.handlers import ToolHandler, ToolInvocation


@dataclass(frozen=True)
class ExpenseQueryDependencies:
    """The already-approved runtime inputs for the read-only Finance tool."""

    adapter: FeishuAdapter
    source: BaseSource
    config: LedgerConfig
    validate_schema: Callable[[], Awaitable[SchemaValidation]]
    cursor_secret: bytes


def build_handler(dependencies: ExpenseQueryDependencies) -> ToolHandler:
    """Return the only handler allowed to expose ``finance.query_expenses``."""

    async def handler(invocation: ToolInvocation) -> dict:
        # The ledger schema is an external mutable boundary. Startup validation
        # proves only that boot was safe; every request must re-read it before
        # interpreting rows or issuing a cursor tied to that schema.
        validation = await dependencies.validate_schema()
        return await query_expenses(
            invocation.arguments,
            adapter=dependencies.adapter,
            source=dependencies.source,
            config=dependencies.config,
            validation=validation,
            cursor_secret=dependencies.cursor_secret,
        )

    return handler
