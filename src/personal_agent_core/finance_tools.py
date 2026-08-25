"""Frozen Finance tool classes used at the model/side-effect boundary.

The orchestrator must distinguish a read from a write before dispatch.  Tool
names alone are model output, so accepting every ``finance.*`` alias would let
a bookkeeping request complete through a read-only query with zero ledger
writes.  Keep this partition explicit and pin it to the generated manifest in
the contract suite.
"""

from __future__ import annotations


FINANCE_EXPENSE_TOOL = "finance.log_expense"
FINANCE_INCOME_TOOL = "finance.log_income"
FINANCE_QUERY_TOOL = "finance.query_expenses"

FINANCE_READ_TOOLS: frozenset[str] = frozenset({FINANCE_QUERY_TOOL})

# These tools accept a model omission only because the Agent Host resolves the
# durable message-receipt date before policy binds and signs their arguments.
# The MCP server rejects a missing field, so the default can never leak into a
# direct connector call as an accidental handler error or a different clock.
FINANCE_HOST_DEFAULT_OCCURRED_ON_TOOLS: frozenset[str] = frozenset(
    {FINANCE_EXPENSE_TOOL, FINANCE_INCOME_TOOL}
)

FINANCE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        FINANCE_EXPENSE_TOOL,
        "finance.log_expense_batch",
        FINANCE_INCOME_TOOL,
        "finance.update_family_fund",
        # A category correction is a write like any other here. It is listed
        # even though the model never calls it -- the route that reaches it is
        # a deterministic tap on the receipt card, not a model decision --
        # because this partition's job is to stop a *write* being satisfied by
        # a read. Omitting a write because "the model cannot reach it today"
        # would make that guarantee depend on a routing detail rather than on
        # what the tool does.
        "finance.update_expense_category",
    }
)

FINANCE_TOOLS: frozenset[str] = FINANCE_READ_TOOLS | FINANCE_WRITE_TOOLS
