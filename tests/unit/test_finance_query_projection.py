"""The strict `finance.query_expenses` display projection (`FinanceQueryProjection`).

This is the boundary that stopped raw MCP JSON from reaching the iOS screen as
a user answer. The tests here are the fail-closed cases enumerated before the
decoder was written: unknown view, unknown fields, wrong field types, a result
that is not a JSON object, and the round-trip of the durable `safe_result`
carrier back through the same decoder.
"""

from __future__ import annotations

import pytest

from personal_agent.api.finance_query_projection import (
    FinanceQueryProjectionError,
    canonical_projection_json,
    decode_finance_query_projection,
    summarise_query_projection,
)

EVIDENCE = {
    "kind": "aggregate_query",
    "query_id": "qry_1",
    "config_checksum": "cfg",
    "schema_snapshot_checksum": "schema",
    "scanned_pages": 1,
    "matched_count": 3,
    "started_at": "2026-08-12T00:00:00Z",
    "completed_at": "2026-08-12T00:00:01Z",
}
FILTERS = {
    "date_range": {"start": "2026-01-01", "end": "2026-12-31"},
    "categories": ["网球"],
    "name_contains": [],
    "is_family_expense": "all",
    "personal_amount_cny": None,
}


def _total(**overrides) -> dict:
    base = {
        "status": "ok",
        "view": "total",
        "metric": "personal_spend_total_cny",
        "record_count": 3,
        "filters_applied": FILTERS,
        "personal_spend_total_cny": "1200.00",
        "source_system": "feishu_bitable",
        "evidence": EVIDENCE,
    }
    base.update(overrides)
    return base


def _by_category(**overrides) -> dict:
    base = {
        "status": "ok",
        "view": "by_category",
        "metric": "personal_spend_total_cny",
        "record_count": 3,
        "filters_applied": FILTERS,
        "personal_spend_total_cny": "1500.00",
        "by_category": [
            {
                "category": "餐饮",
                "personal_spend_total_cny": "1000.00",
                "record_count": 2,
                "share_of_total": "66.67",
            },
            {
                "category": None,
                "personal_spend_total_cny": "500.00",
                "record_count": 1,
                "share_of_total": "33.33",
            },
        ],
        "source_system": "feishu_bitable",
        "evidence": EVIDENCE,
    }
    base.update(overrides)
    return base


def _records(**overrides) -> dict:
    base = {
        "status": "ok",
        "view": "records",
        "metric": "personal_spend_total_cny",
        "record_count": 5,
        "filters_applied": FILTERS,
        "records": [
            {
                "record_id": "rec1",
                "name": "网球场地费",
                "occurred_on": "2026-05-01",
                "category": "运动",
                "is_family_expense": False,
                "personal_spend_cny": "200.00",
            },
            {
                "record_id": "rec2",
                "name": "未分类",
                "occurred_on": None,
                "category": None,
                "is_family_expense": True,
                "personal_spend_cny": "-50.00",
            },
        ],
        "next_cursor": "cursor-v1",
        "source_system": "feishu_bitable",
        "evidence": EVIDENCE,
    }
    base.update(overrides)
    return base


def test_total_view_projects_the_allowed_fields() -> None:
    projection = decode_finance_query_projection(_total())
    assert projection.view == "total"
    assert projection.personal_spend_total_cny == "1200.00"
    assert projection.record_count == 3
    assert projection.by_category == ()
    assert projection.records == ()
    assert projection.next_cursor is None


def test_by_category_view_projects_buckets() -> None:
    projection = decode_finance_query_projection(_by_category())
    assert projection.view == "by_category"
    assert projection.personal_spend_total_cny == "1500.00"
    assert len(projection.by_category) == 2
    assert projection.by_category[0].category == "餐饮"
    assert projection.by_category[1].category is None
    assert projection.by_category[1].share_of_total == "33.33"
    assert projection.records == ()


def test_records_view_preserves_the_cursor_semantics() -> None:
    projection = decode_finance_query_projection(_records())
    assert projection.view == "records"
    assert len(projection.records) == 2
    assert projection.records[0].record_id == "rec1"
    assert projection.records[0].is_family_expense is False
    assert projection.records[1].occurred_on is None
    assert projection.next_cursor == "cursor-v1"


def test_records_without_a_next_page_has_no_cursor() -> None:
    projection = decode_finance_query_projection(
        _records(next_cursor=None)
    )
    assert projection.next_cursor is None


def test_the_durable_carrier_round_trips_through_the_same_decoder() -> None:
    for raw in (_total(), _by_category(), _records()):
        projection = decode_finance_query_projection(raw)
        # The stored `safe_result` is canonical JSON; re-reading it must give the
        # exact same projection, so history can never disagree with the live one.
        decoded = decode_finance_query_projection(
            canonical_projection_json(projection)
        )
        assert decoded == projection
        assert decoded.to_dict() == projection.to_dict()


def test_unknown_top_level_field_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(
            _total(provider_prose="已写入成功")
        )


def test_unknown_view_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(_total(view="pie_chart"))


def test_wrong_field_type_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(_total(record_count="two"))


def test_unknown_evidence_field_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(
            _total(evidence={**EVIDENCE, "internal_debug": "x"})
        )


def test_a_non_object_result_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(["not", "an", "object"])


def test_damaged_json_string_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection("{not json")


def test_a_bucket_with_unknown_fields_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(
            _by_category(
                by_category=[_by_category()["by_category"][0] | {"note": "x"}]
            )
        )


def test_a_row_with_a_bad_amount_fails_closed() -> None:
    with pytest.raises(FinanceQueryProjectionError):
        decode_finance_query_projection(
            _records(records=[_records()["records"][0] | {"personal_spend_cny": 200}])
        )


def test_the_deterministic_summary_comes_from_the_projection_only() -> None:
    assert (
        summarise_query_projection(decode_finance_query_projection(_total()))
        == "共 3 条记录，个人支出合计 ¥1200.00"
    )
    assert (
        summarise_query_projection(decode_finance_query_projection(_by_category()))
        == "共 3 条记录，2 个分类"
    )
    assert (
        summarise_query_projection(decode_finance_query_projection(_records()))
        == "共 5 条记录，还有更多"
    )
    assert (
        summarise_query_projection(
            decode_finance_query_projection(_records(next_cursor=None))
        )
        == "共 5 条记录"
    )
