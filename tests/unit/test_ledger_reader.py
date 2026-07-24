"""The internal ledger read: whole year or nothing.

A trip that exists only on page two would be silently re-created, and a refund
whose original is on page two would look unmatched. So "did it read everything"
is a correctness property here, not a performance detail, and a source that
cannot be paginated safely must fail rather than return a partial year.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.ledger_reader import read_year_expenses
from personal_data_mcp.finance.trip_tags import distinct_tags


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
)
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)


def clock():
    return 1000.0


def run(coro):
    return asyncio.run(coro)


def record(
    name: str,
    amount,
    category: str = "旅行",
    record_id: str = "rec",
    family: bool | None = None,
):
    """A row in the shape `search_records` actually returns (see G3 evidence)."""
    fields = {
        "名称": [{"text": name, "type": "text"}],
        "原始金额": amount,
        "日期": 1784736000000,
        "分类": category,
    }
    if family is not None:
        fields["是否家庭支出"] = family
    return {
        "record_id": record_id,
        "fields": fields,
    }


def paged_handler(pages):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        assert request.method == "POST" and request.url.path.endswith("/search")
        # A body is mandatory even with no filter; Feishu answers 9499 without.
        assert json.loads(request.content)["field_names"]
        page = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, json={"code": 0, "data": page})

    return handler, calls


def read_with(pages):
    handler, calls = paged_handler(pages)

    async def scenario():
        async with FeishuAdapter(
            FeishuCredentials(app_id="cli", app_secret="s"),
            transport=httpx.MockTransport(handler),
            now=clock,
        ) as adapter:
            return await read_year_expenses(
                adapter, source=SOURCE, config=CONFIG
            )

    return run(scenario()), calls


def test_every_page_is_read_before_the_year_is_considered_complete() -> None:
    rows, calls = read_with(
        [
            {
                "items": [record("机票 #东京", 2000, record_id="r1")],
                "has_more": True,
                "page_token": "p2",
            },
            {
                "items": [record("酒店 #大阪", 800, record_id="r2")],
                "has_more": False,
            },
        ]
    )
    assert calls["n"] == 2
    assert [row.record_id for row in rows] == ["r1", "r2"]
    # The second page's trip would have been invisible to the resolver.
    assert distinct_tags([row.name for row in rows]) == {"东京", "大阪"}


def test_values_are_normalised_from_the_search_shape() -> None:
    rows, _ = read_with(
        [
            {
                "items": [record("机票 #东京", 2000, family=True)],
                "has_more": False,
            }
        ]
    )
    assert rows[0].name == "机票 #东京"
    assert rows[0].amount_cny == Decimal("2000.00")
    assert rows[0].category == "旅行"
    assert rows[0].occurred_on.isoformat() == "2026-07-23"
    assert rows[0].is_family_expense is True


def test_an_omitted_unchecked_family_box_reads_as_personal() -> None:
    rows, _ = read_with(
        [{"items": [record("午饭", 20, category="餐饮")], "has_more": False}]
    )
    assert rows[0].is_family_expense is False


def test_more_pages_without_a_cursor_fails_rather_than_truncating() -> None:
    with pytest.raises(AppError) as caught:
        read_with([{"items": [], "has_more": True}])
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_a_repeated_cursor_fails_rather_than_looping() -> None:
    page = {"items": [], "has_more": True, "page_token": "same"}
    with pytest.raises(AppError):
        read_with([page, page, page])


def test_an_empty_ledger_is_an_empty_year_not_an_error() -> None:
    rows, _ = read_with([{"items": [], "has_more": False}])
    assert rows == []
