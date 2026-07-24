"""DEV-022 query semantics: complete source scans, formula accounting and cursors."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import ledger_day_epoch_millis
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.onboarding import observed_from_snapshot
from personal_data_mcp.finance.query_expenses import query_expenses
from personal_data_mcp.finance.schema_validator import validate_schema


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads((LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8"))
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(encoding="utf-8")
)
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)
VALIDATION = validate_schema(CONFIG, observed_from_snapshot(SNAPSHOT))
CURSOR_SECRET = b"test-only-query-cursor-secret-32bytes"
NOW = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def record(
    record_id: str,
    *,
    name: str = "午饭",
    day: str = "2026-07-23",
    category: str = "餐饮",
    family: bool | None = False,
    personal_spend: str | int = "20",
) -> dict:
    fields = {
        "名称": [{"text": name, "type": "text"}],
        "日期": ledger_day_epoch_millis(datetime.fromisoformat(day).date()),
        "分类": category,
        "个人支出": {"type": 2, "value": [personal_spend]},
    }
    if family is not None:
        fields["是否家庭支出"] = family
    return {"record_id": record_id, "fields": fields}


def response_handler(pages, seen):
    calls = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        assert request.url.path.endswith("/search")
        seen.append(json.loads(request.content)["field_names"])
        page = pages[calls["page"]]
        calls["page"] += 1
        return httpx.Response(200, json={"code": 0, "data": page})

    return handler


def query_with(
    pages,
    arguments,
    *,
    now=lambda: NOW,
    cursor_secret=CURSOR_SECRET,
    config=CONFIG,
    validation=VALIDATION,
):
    seen: list[list[str]] = []
    handler = response_handler(pages, seen)

    async def scenario():
        async with FeishuAdapter(
            FeishuCredentials(app_id="cli", app_secret="s"),
            transport=httpx.MockTransport(handler),
            now=lambda: 1000.0,
        ) as adapter:
            return await query_expenses(
                arguments,
                adapter=adapter,
                source=SOURCE,
                config=config,
                validation=validation,
                cursor_secret=cursor_secret,
                now=now,
            )

    return run(scenario()), seen


def test_total_reads_every_source_page_and_uses_personal_formula() -> None:
    result, fields = query_with(
        [
            {"items": [record("r1", personal_spend="20")], "has_more": True, "page_token": "p2"},
            {
                "items": [
                    record("r2", name="机票 #东京", category="旅行", family=True, personal_spend="100"),
                    record("r3", name="退款", personal_spend="-15"),
                ],
                "has_more": False,
            },
        ],
        {"view": "total", "date_range": {"start": "2026-07-01", "end": "2026-07-31"}},
    )
    assert result["record_count"] == 3
    assert result["personal_spend_total_cny"] == "105.00"
    assert result["evidence"]["scanned_pages"] == 2
    # Total needs its formula and date filter, not the raw amount or text.
    assert fields == [["日期", "个人支出"], ["日期", "个人支出"]]


def test_category_and_amount_filters_apply_to_signed_formula_values() -> None:
    result, _ = query_with(
        [
            {
                "items": [
                    record("r1", name="网球场地", category="日常生活", personal_spend="120"),
                    record("r2", name="网球退款", category="日常生活", personal_spend="-30"),
                    record("r3", name="晚饭", category="餐饮", personal_spend="80"),
                ],
                "has_more": False,
            }
        ],
        {
            "view": "by_category",
            "categories": ["日常生活"],
            "name_contains": ["网球"],
            "personal_amount_cny": {"min": "0", "min_inclusive": False},
        },
    )
    assert result["personal_spend_total_cny"] == "120.00"
    assert result["by_category"] == [
        {
            "category": "日常生活",
            "personal_spend_total_cny": "120.00",
            "record_count": 1,
            "share_of_total": "100.00",
        }
    ]


def test_live_formula_envelope_is_normalised_without_reconstructing_its_value() -> None:
    wrapped = record("r1", personal_spend="unused")
    wrapped["fields"]["个人支出"] = {"type": 2, "value": [120]}
    result, _ = query_with(
        [{"items": [wrapped], "has_more": False}],
        {"view": "total", "date_range": {"start": "2026-07-01", "end": "2026-07-31"}},
    )
    assert result["personal_spend_total_cny"] == "120.00"


def test_plain_numeric_formula_shape_is_refused_as_source_unavailable() -> None:
    malformed = record("r1")
    malformed["fields"]["个人支出"] = 120
    with pytest.raises(AppError) as caught:
        query_with(
            [{"items": [malformed], "has_more": False}],
            {
                "view": "total",
                "date_range": {"start": "2026-07-01", "end": "2026-07-31"},
            },
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


@pytest.mark.parametrize(
    ("field_name", "malformed_value"),
    [
        ("名称", {"unexpected": "shape"}),
        ("日期", "not-an-epoch"),
        ("分类", ["not", "a", "select"]),
    ],
)
def test_a_malformed_required_record_field_never_silently_undercounts(
    field_name, malformed_value
) -> None:
    malformed = record("r1")
    malformed["fields"][field_name] = malformed_value
    with pytest.raises(AppError) as caught:
        query_with(
            [{"items": [malformed], "has_more": False}],
            {"view": "records", "name_contains": ["午"]},
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_a_malformed_date_used_by_total_filter_never_silently_undercounts() -> None:
    malformed = record("r1", personal_spend="100")
    malformed["fields"]["日期"] = "not-an-epoch"
    with pytest.raises(AppError) as caught:
        query_with(
            [{"items": [malformed], "has_more": False}],
            {
                "view": "total",
                "date_range": {"start": "2026-07-01", "end": "2026-07-31"},
            },
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_records_cursor_is_signed_filter_bound_and_returns_the_next_page() -> None:
    rows = [record(f"r{index:03}", name=f"账单 {index}") for index in range(51)]
    first, _ = query_with(
        [{"items": rows, "has_more": False}],
        {"view": "records", "name_contains": ["账单"]},
    )
    assert len(first["records"]) == 50
    assert first["next_cursor"]

    second, _ = query_with(
        [{"items": rows, "has_more": False}],
        {"view": "records", "cursor": first["next_cursor"]},
    )
    assert [item["record_id"] for item in second["records"]] == ["r000"]
    assert second["next_cursor"] is None

    with pytest.raises(AppError) as caught:
        query_with(
            [{"items": rows, "has_more": False}],
            {"view": "records", "cursor": first["next_cursor"], "name_contains": ["other"]},
        )
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_records_cursor_refuses_a_changed_matching_result_set() -> None:
    rows = [record(f"r{index:03}", name=f"账单 {index}") for index in range(51)]
    first, _ = query_with(
        [{"items": rows, "has_more": False}],
        {"view": "records", "name_contains": ["账单"]},
    )
    changed_rows = [
        record("newest", name="账单 新增", day="2026-07-24"),
        *rows,
    ]

    with pytest.raises(AppError) as caught:
        query_with(
            [{"items": changed_rows, "has_more": False}],
            {"view": "records", "cursor": first["next_cursor"]},
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_records_cursor_is_bound_to_the_active_config() -> None:
    rows = [record(f"r{index:03}", name=f"账单 {index}") for index in range(51)]
    first, _ = query_with(
        [{"items": rows, "has_more": False}],
        {"view": "records", "name_contains": ["账单"]},
    )
    changed_document = CONFIG.model_dump(mode="json")
    changed_document["config_version"] = "2026.cursor-test"
    changed_config = load_ledger_config(changed_document)
    changed_validation = validate_schema(
        changed_config, observed_from_snapshot(SNAPSHOT)
    )

    with pytest.raises(AppError) as caught:
        query_with(
            [],
            {"view": "records", "cursor": first["next_cursor"]},
            config=changed_config,
            validation=changed_validation,
        )
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_cursor_expiry_is_fixed_across_continuation_pages() -> None:
    rows = [record(f"r{index:03}", name=f"账单 {index}") for index in range(101)]
    first, _ = query_with(
        [{"items": rows, "has_more": False}],
        {"view": "records", "name_contains": ["账单"]},
        now=lambda: NOW,
    )
    second, _ = query_with(
        [{"items": rows, "has_more": False}],
        {"view": "records", "cursor": first["next_cursor"]},
        now=lambda: NOW.replace(minute=9),
    )
    assert second["next_cursor"]

    with pytest.raises(AppError) as caught:
        query_with(
            [{"items": rows, "has_more": False}],
            {"view": "records", "cursor": second["next_cursor"]},
            now=lambda: NOW.replace(minute=10),
        )
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("secret", [b"", b"x", "not-bytes"])
def test_cursor_signing_secret_must_be_at_least_32_bytes(secret) -> None:
    with pytest.raises(AppError) as caught:
        query_with(
            [],
            {"view": "total", "name_contains": ["午"]},
            cursor_secret=secret,
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_query_evidence_identifies_the_validated_schema_snapshot() -> None:
    result, _ = query_with(
        [{"items": [record("r1")], "has_more": False}],
        {"view": "total", "name_contains": ["午"]},
    )
    assert result["evidence"]["schema_snapshot_checksum"] == (
        VALIDATION.snapshot_checksum
    )
    assert len(result["evidence"]["schema_snapshot_checksum"]) == 64


def test_an_unbounded_query_requires_clarification_before_any_source_read() -> None:
    with pytest.raises(AppError) as caught:
        query_with([], {"view": "total"})
    assert caught.value.code is ErrorCode.CLARIFICATION_REQUIRED


@pytest.mark.parametrize(
    "page",
    [
        {"items": [], "has_more": True},
        {"items": "not-a-list", "has_more": False},
    ],
)
def test_malformed_source_pagination_never_returns_a_partial_total(page) -> None:
    with pytest.raises(AppError) as caught:
        query_with(
            [page],
            {"view": "total", "date_range": {"start": "2026-07-01", "end": "2026-07-31"}},
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_a_drifted_schema_refuses_the_query_before_source_read() -> None:
    invalid = validate_schema(CONFIG, {"expense": [], "income": [], "family_fund": []})

    async def scenario():
        async with FeishuAdapter(
            FeishuCredentials(app_id="cli", app_secret="s"),
            transport=httpx.MockTransport(lambda request: pytest.fail("must not call Feishu")),
            now=lambda: 1000.0,
        ) as adapter:
            return await query_expenses(
                {"view": "total", "date_range": {"start": "2026-07-01", "end": "2026-07-31"}},
                adapter=adapter,
                source=SOURCE,
                config=CONFIG,
                validation=invalid,
                cursor_secret=CURSOR_SECRET,
                now=lambda: NOW,
            )

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED
