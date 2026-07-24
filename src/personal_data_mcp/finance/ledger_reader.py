"""Reading the annual expense table, for the resolvers that need it.

Two questions are asked of the ledger before a write: which trips already exist,
and which earlier expense a refund is reducing. Both need the *whole* year, so
both paginate to exhaustion -- engineering rule 5 forbids calling a first page
"all data", and here the consequence would be concrete: a trip that exists only
on page two would be silently re-created, and a refund whose original is on page
two would look unmatched.

This is not a model-visible tool. `finance.query_expenses` is its own contract
at DEV-022 with its own evidence and cursor rules; this module is the internal
read the write path uses to keep from guessing.

Values are normalised the moment they arrive, because the two Feishu read
endpoints disagree about JSON types for the same cell -- a name comes back as
plain text from one and as rich-text segments from the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.endpoints import SEARCH_RECORDS
from personal_data_mcp.finance.expense_record import (
    as_decimal,
    as_ledger_date,
    as_text,
)
from personal_data_mcp.finance.ledger_config import LedgerConfig


#: Feishu's documented maximum. Sent explicitly rather than relying on a
#: provider default, which could change under us.
PAGE_SIZE: Final[int] = 500

#: A year of one person's expenses cannot plausibly exceed this many pages.
#: The bound exists so a provider that never clears `has_more` fails loudly
#: instead of looping forever.
MAX_PAGES: Final[int] = 200


@dataclass(frozen=True)
class LedgerExpense:
    """One existing expense row, as much of it as the resolvers need."""

    record_id: str
    name: str
    amount_cny: Decimal | None
    occurred_on: date | None
    category: str | None


async def _search_page(
    adapter: FeishuAdapter,
    *,
    base_token: str,
    table_id: str,
    field_names: list[str],
    page_token: str | None,
) -> dict[str, Any]:
    query = {"page_size": str(PAGE_SIZE)}
    if page_token:
        query["page_token"] = page_token
    # The search endpoint requires a JSON body even when nothing is filtered:
    # omitting it returns Feishu code 9499. Confirmed against the test Base.
    return await adapter.request(
        SEARCH_RECORDS,
        params={"app_token": base_token, "table_id": table_id},
        json={"field_names": field_names, "automatic_fields": False},
        query=query,
    )


async def read_year_expenses(
    adapter: FeishuAdapter, *, source: BaseSource, config: LedgerConfig
) -> list[LedgerExpense]:
    """Every expense row in the active annual table, read to exhaustion."""
    fields = config.tables["expense"].fields
    name_field = fields["name"].expected_name
    amount_field = fields["amount"].expected_name
    date_field = fields["occurred_on"].expected_name
    category_field = fields["category"].expected_name

    rows: list[LedgerExpense] = []
    page_token: str | None = None
    seen: set[str] = set()

    for _ in range(MAX_PAGES):
        data = await _search_page(
            adapter,
            base_token=source.base_token,
            table_id=source.tables["expense"],
            field_names=[name_field, amount_field, date_field, category_field],
            page_token=page_token,
        )
        items = data.get("items") or []
        if not isinstance(items, list):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="search_records returned malformed items",
            )
        for item in items:
            if not isinstance(item, dict):
                continue
            cells = item.get("fields") or {}
            rows.append(
                LedgerExpense(
                    record_id=str(item.get("record_id", "")),
                    name=as_text(cells.get(name_field)) or "",
                    amount_cny=as_decimal(cells.get(amount_field)),
                    occurred_on=as_ledger_date(cells.get(date_field)),
                    category=as_text(cells.get(category_field)),
                )
            )

        has_more = data.get("has_more", False)
        if not isinstance(has_more, bool):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="search_records returned malformed has_more",
            )
        if not has_more:
            return rows

        page_token = data.get("page_token")
        if not isinstance(page_token, str) or not page_token:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="search_records reported more pages without a cursor",
            )
        if page_token in seen:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="search_records repeated a pagination cursor",
            )
        seen.add(page_token)

    raise AppError(
        ErrorCode.SOURCE_UNAVAILABLE,
        internal_detail="search_records did not terminate its pagination",
    )
