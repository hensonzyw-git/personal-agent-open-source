"""Frozen Finance tool classes used at the model/side-effect boundary.

The orchestrator must distinguish a read from a write before dispatch.  Tool
names alone are model output, so accepting every ``finance.*`` alias would let
a bookkeeping request complete through a read-only query with zero ledger
writes.  Keep this partition explicit and pin it to the generated manifest in
the contract suite.
"""

from __future__ import annotations


FINANCE_QUERY_TOOL = "finance.query_expenses"

FINANCE_READ_TOOLS: frozenset[str] = frozenset({FINANCE_QUERY_TOOL})

FINANCE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "finance.log_expense",
        "finance.log_expense_batch",
        "finance.log_income",
        "finance.update_family_fund",
    }
)

FINANCE_TOOLS: frozenset[str] = FINANCE_READ_TOOLS | FINANCE_WRITE_TOOLS
