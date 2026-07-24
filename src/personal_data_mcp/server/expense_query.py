"""Bind the verified MCP invocation to the DEV-022 Finance query core.

The dependencies are injected by service composition instead of loaded inside a
tool handler.  In particular, a handler must never open ``.env.finance.local``
or guess a protected config path: the service owns credential/config loading and
only supplies a source that has already been bound to the protected ledger.
"""

from __future__ import annotations

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
    validation: SchemaValidation
    cursor_secret: bytes


def build_handler(dependencies: ExpenseQueryDependencies) -> ToolHandler:
    """Return the only handler allowed to expose ``finance.query_expenses``."""

    async def handler(invocation: ToolInvocation) -> dict:
        return await query_expenses(
            invocation.arguments,
            adapter=dependencies.adapter,
            source=dependencies.source,
            config=dependencies.config,
            validation=dependencies.validation,
            cursor_secret=dependencies.cursor_secret,
        )

    return handler
