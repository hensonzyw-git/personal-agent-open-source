"""The advertised expense-query handler revalidates mutable ledger schema."""

from __future__ import annotations

import asyncio

from personal_data_mcp.server import expense_query
from personal_data_mcp.server.expense_query import ExpenseQueryDependencies
from personal_data_mcp.server.handlers import ToolInvocation


def test_each_query_uses_a_fresh_schema_validation(monkeypatch) -> None:
    validations = iter([object(), object()])
    observed: list[object] = []

    async def validate_schema():
        return next(validations)

    async def query(_arguments, **kwargs):
        observed.append(kwargs["validation"])
        return {"status": "ok"}

    monkeypatch.setattr(expense_query, "query_expenses", query)
    dependencies = ExpenseQueryDependencies(
        adapter=object(),  # type: ignore[arg-type]
        source=object(),  # type: ignore[arg-type]
        config=object(),  # type: ignore[arg-type]
        validate_schema=validate_schema,
        cursor_secret=b"q" * 32,
    )
    handler = expense_query.build_handler(dependencies)
    invocation = ToolInvocation(
        tool="finance.query_expenses",
        arguments={"view": "total"},
        verified_call=object(),  # type: ignore[arg-type]
    )

    asyncio.run(handler(invocation))
    asyncio.run(handler(invocation))

    assert len(observed) == 2
    assert observed[0] is not observed[1]
