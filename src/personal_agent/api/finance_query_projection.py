"""The strict, whitelisted projection of ``finance.query_expenses`` results.

The query path is one governed MCP read whose result the Agent turns into a
durable, client-visible outcome. The MCP output schema is the *connector's*
contract; the projection here is the narrower *display* contract. iOS renders a
query card from exactly the fields this module projects, and nothing else.

Everything a client could mis-render fails closed here instead:

- an unknown top-level field, an unknown view, a wrong field type or a result
  that is not a JSON object is refused, so raw MCP content can never be shown
  as a user answer;
- nested ``evidence`` is whitelisted field by field, so connector internals
  (checksums are hashes, but the fields are still a fixed contract) cannot be
  replaced by prose or credentials;
- ``to_dict()`` round-trips through the same decoder, so the durable
  ``safe_result`` carrier and the live projection can never disagree about what
  a query result is.

``summarise_query_projection`` is the deterministic text fallback for clients
that predate ``query_result``. It is derived from the projection only -- never
from the model -- so the old client sees a readable summary instead of a JSON
dump while the new client reads the structured card.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from personal_agent_core.manifest import canonical_json


#: The stable failure reason a non-projectable query result receives. It is a
#: closed, reviewed string like the other safe-failure reasons; it is never
#: derived from the result itself.
QUERY_RESULT_UNREADABLE = "query_result_unreadable"


class FinanceQueryProjectionError(ValueError):
    """A query result is not a safe, projectable shape.

    Raised by ``decode_finance_query_projection`` for unknown views, unknown
    fields, wrong field types and unparseable payloads. Callers fail closed --
    a safe failure, never a presentation of the raw result.
    """


_VIEWS = frozenset({"total", "by_category", "records", "by_trip"})
_METRIC = "personal_spend_total_cny"
_SOURCE_SYSTEM = "feishu_bitable"

#: Every top-level key a projectable query result may carry. Anything else is
#: refused rather than silently dropped, because a client that never saw the
#: field cannot be trusted to ignore it safely.
_TOP_LEVEL_FIELDS = frozenset(
    {
        "status",
        "view",
        "metric",
        "record_count",
        "filters_applied",
        "source_system",
        "evidence",
        "personal_spend_total_cny",
        "by_category",
        "by_trip",
        "coverage",
        "records",
        "next_cursor",
    }
)

#: The one evidence object the connector may attach to a query. Whitelisted
#: rather than copied so an out-of-contract evidence shape fails closed.
_EVIDENCE_FIELDS = frozenset(
    {
        "kind",
        "query_id",
        "config_checksum",
        "schema_snapshot_checksum",
        "scanned_pages",
        "matched_count",
        "started_at",
        "completed_at",
        "parser_version",
        "result_checksum",
    }
)


@dataclass(frozen=True)
class QueryCategoryBucket:
    """One ``by_category`` bucket, strictly typed from the MCP result."""

    category: str | None
    personal_spend_total_cny: str
    record_count: int
    share_of_total: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "personal_spend_total_cny": self.personal_spend_total_cny,
            "record_count": self.record_count,
            "share_of_total": self.share_of_total,
        }


@dataclass(frozen=True)
class QueryRecordRow:
    """One ``records`` row, strictly typed from the MCP result."""

    record_id: str
    name: str
    occurred_on: str | None
    category: str | None
    is_family_expense: bool
    personal_spend_cny: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "name": self.name,
            "occurred_on": self.occurred_on,
            "category": self.category,
            "is_family_expense": self.is_family_expense,
            "personal_spend_cny": self.personal_spend_cny,
        }


@dataclass(frozen=True)
class FinanceQueryProjection:
    """The versioned, whitelisted projection of one query result.

    Only fields the existing query contract allows a client to display are
    present. ``to_dict()`` is the durable carrier written into ``safe_result``
    and the object served as ``query_result``; both are parsed back through
    ``decode_finance_query_projection``, never trusted by shape alone.
    """

    view: str
    metric: str
    record_count: int
    filters_applied: dict[str, Any]
    source_system: str
    evidence: dict[str, Any]
    personal_spend_total_cny: str | None = None
    by_category: tuple[QueryCategoryBucket, ...] = ()
    records: tuple[QueryRecordRow, ...] = ()
    next_cursor: str | None = None
    by_trip: tuple[dict[str, Any], ...] = ()
    coverage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "status": "ok",
            "view": self.view,
            "metric": self.metric,
            "record_count": self.record_count,
            "filters_applied": self.filters_applied,
            "source_system": self.source_system,
            "evidence": self.evidence,
        }
        if self.coverage is not None:
            base['coverage'] = self.coverage
        if self.view == 'by_trip':
            base['personal_spend_total_cny'] = self.personal_spend_total_cny
            base['by_trip'] = list(self.by_trip)
        elif self.view == "total":
            base["personal_spend_total_cny"] = self.personal_spend_total_cny
        elif self.view == "by_category":
            base["personal_spend_total_cny"] = self.personal_spend_total_cny
            base["by_category"] = [bucket.to_dict() for bucket in self.by_category]
        else:
            base["records"] = [row.to_dict() for row in self.records]
            if self.next_cursor is not None:
                base["next_cursor"] = self.next_cursor
        return base


def decode_finance_query_projection(
    raw: dict[str, Any] | str,
) -> FinanceQueryProjection:
    """Strictly decode a query result into the display projection.

    Accepts the MCP ``trusted_result`` dict or its canonical JSON string (the
    durable ``safe_result`` carrier), so one decoder is the single reader for
    both live results and history.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FinanceQueryProjectionError(
                "query result is not valid JSON"
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise FinanceQueryProjectionError("query result is not an object")

    unknown = set(data) - _TOP_LEVEL_FIELDS
    if unknown:
        raise FinanceQueryProjectionError(
            f"query result carries unknown fields: {', '.join(sorted(unknown))}"
        )
    if data.get("status") != "ok":
        raise FinanceQueryProjectionError("query status is not ok")

    view = data.get("view")
    if view not in _VIEWS:
        raise FinanceQueryProjectionError(f"unknown query view {view!r}")
    if data.get("metric") != _METRIC:
        raise FinanceQueryProjectionError("query metric is not the expense total")

    record_count = data.get("record_count")
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise FinanceQueryProjectionError("record_count is not a non-negative integer")

    filters_applied = data.get("filters_applied")
    if not isinstance(filters_applied, dict):
        raise FinanceQueryProjectionError("filters_applied is not an object")
    if data.get("source_system") != _SOURCE_SYSTEM:
        raise FinanceQueryProjectionError("query source_system is not the ledger")

    evidence = _decode_evidence(data.get("evidence"))

    trip = view == 'by_trip' or filters_applied.get('trip_tag') is not None
    if trip:
        from personal_agent_core.trip_query import validate_trip_result
        try:
            validate_trip_result(data)
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            raise FinanceQueryProjectionError('invalid trip result') from exc
    elif 'coverage' in data or 'by_trip' in data:
        raise FinanceQueryProjectionError('unexpected trip extension')
    if view == 'by_trip':
        return FinanceQueryProjection(view=view, metric=_METRIC, record_count=record_count,
            filters_applied=filters_applied, source_system=_SOURCE_SYSTEM, evidence=evidence,
            personal_spend_total_cny=data['personal_spend_total_cny'], by_trip=tuple(data['by_trip']), coverage=data['coverage'])
    if view == "total":
        return FinanceQueryProjection(
            view=view,
            coverage=data.get("coverage"),
            metric=_METRIC,
            record_count=record_count,
            filters_applied=filters_applied,
            source_system=_SOURCE_SYSTEM,
            evidence=evidence,
            personal_spend_total_cny=_require_amount(
                data.get("personal_spend_total_cny")
            ),
        )

    if view == "by_category":
        buckets = data.get("by_category")
        if not isinstance(buckets, list):
            raise FinanceQueryProjectionError("by_category is not a list")
        return FinanceQueryProjection(
            view=view,
            coverage=data.get("coverage"),
            metric=_METRIC,
            record_count=record_count,
            filters_applied=filters_applied,
            source_system=_SOURCE_SYSTEM,
            evidence=evidence,
            personal_spend_total_cny=_require_amount(
                data.get("personal_spend_total_cny")
            ),
            by_category=tuple(_decode_bucket(item) for item in buckets),
        )

    rows = data.get("records")
    if not isinstance(rows, list):
        raise FinanceQueryProjectionError("records is not a list")
    next_cursor = data.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise FinanceQueryProjectionError("next_cursor is not a string or null")
    return FinanceQueryProjection(
        view=view,
        coverage=data.get("coverage"),
        metric=_METRIC,
        record_count=record_count,
        filters_applied=filters_applied,
        source_system=_SOURCE_SYSTEM,
        evidence=evidence,
        records=tuple(_decode_row(item) for item in rows),
        next_cursor=next_cursor,
    )


def summarise_query_projection(projection: FinanceQueryProjection) -> str:
    """Deterministic text fallback for clients that predate ``query_result``.

    Never model prose: it is derived solely from the validated projection, so
    the compatibility ``answer`` and the structured card can never disagree.
    """
    if projection.coverage is not None:
        c = projection.coverage
        years = '、'.join(str(y) for y in c['source_years'])
        scope = '净个人支出' if c['scope_coverage'] == 'complete' else f'已接入 {years} 账本内的净个人支出小计（覆盖有限）'
        label = projection.filters_applied.get('trip_tag') or '旅行场次'
        text = f"{label}：{scope} ¥{projection.personal_spend_total_cny or '—'}，共 {projection.record_count} 笔。"
        dates = projection.filters_applied.get('date_range')
        text += f" 账单日期 {dates['start']} 至 {dates['end']}。" if dates else ' 未限定账单日期。'
        if projection.record_count == 0 and c['scope_coverage'] != 'complete':
            text += ' 已接入账本未找到匹配记录，不能据此判断完整场次总花费为零。'
        if projection.view == 'by_trip':
            text += '\n' + '\n'.join(f"{b['trip_tag'] or '未归属场次'}：¥{b['personal_spend_total_cny']}，{b['record_count']} 笔" for b in projection.by_trip)
        if not c['assignment_complete']:
            text += f"\n扫描范围内 {c['unassigned_record_count']} 笔未能归属场次。"
        if projection.view == 'records':
            text += ' 本页为场次明细。' + ('还有更多。' if projection.next_cursor else '')
        return text
    if projection.view == "total":
        return (
            f"共 {projection.record_count} 条记录，"
            f"个人支出合计 ¥{projection.personal_spend_total_cny}"
        )
    if projection.view == "by_category":
        return (
            f"共 {projection.record_count} 条记录，"
            f"{len(projection.by_category)} 个分类"
        )
    more = "，还有更多" if projection.next_cursor else ""
    return f"共 {projection.record_count} 条记录{more}"


def canonical_projection_json(projection: FinanceQueryProjection) -> str:
    """The durable carrier: canonical JSON of the whitelisted projection."""
    return canonical_json(projection.to_dict())


def _decode_evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FinanceQueryProjectionError("evidence is not an object")
    unknown = set(raw) - _EVIDENCE_FIELDS
    if unknown:
        raise FinanceQueryProjectionError(
            f"evidence carries unknown fields: {', '.join(sorted(unknown))}"
        )
    for field in ("kind", "query_id", "config_checksum",
                  "schema_snapshot_checksum", "started_at", "completed_at"):
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise FinanceQueryProjectionError(
                f"evidence.{field} is not a non-empty string"
            )
    for field in ("scanned_pages", "matched_count"):
        value = raw.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FinanceQueryProjectionError(
                f"evidence.{field} is not a non-negative integer"
            )
    return dict(raw)


def _require_amount(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise FinanceQueryProjectionError(
            "personal_spend_total_cny is not a non-empty string"
        )
    return raw


def _decode_bucket(raw: Any) -> QueryCategoryBucket:
    if not isinstance(raw, dict):
        raise FinanceQueryProjectionError("by_category item is not an object")
    unknown = set(raw) - {
        "category",
        "personal_spend_total_cny",
        "record_count",
        "share_of_total",
    }
    if unknown:
        raise FinanceQueryProjectionError(
            f"by_category item carries unknown fields: {', '.join(sorted(unknown))}"
        )
    category = raw.get("category")
    if category is not None and not isinstance(category, str):
        raise FinanceQueryProjectionError("by_category category is not a string or null")
    count = raw.get("record_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise FinanceQueryProjectionError("by_category record_count is invalid")
    share = raw.get("share_of_total")
    if share is not None and not isinstance(share, str):
        raise FinanceQueryProjectionError(
            "by_category share_of_total is not a string or null"
        )
    return QueryCategoryBucket(
        category=category,
        personal_spend_total_cny=_require_amount(
            raw.get("personal_spend_total_cny")
        ),
        record_count=count,
        share_of_total=share,
    )


def _decode_row(raw: Any) -> QueryRecordRow:
    if not isinstance(raw, dict):
        raise FinanceQueryProjectionError("records item is not an object")
    unknown = set(raw) - {
        "record_id",
        "name",
        "occurred_on",
        "category",
        "is_family_expense",
        "personal_spend_cny",
    }
    if unknown:
        raise FinanceQueryProjectionError(
            f"records item carries unknown fields: {', '.join(sorted(unknown))}"
        )
    record_id = raw.get("record_id")
    name = raw.get("name")
    if not isinstance(record_id, str) or not record_id:
        raise FinanceQueryProjectionError("records item has no record_id")
    if not isinstance(name, str):
        raise FinanceQueryProjectionError("records item has no name")
    for field in ("occurred_on", "category"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise FinanceQueryProjectionError(
                f"records item {field} is not a string or null"
            )
    family = raw.get("is_family_expense")
    if not isinstance(family, bool):
        raise FinanceQueryProjectionError(
            "records item is_family_expense is not a boolean"
        )
    return QueryRecordRow(
        record_id=record_id,
        name=name,
        occurred_on=raw.get("occurred_on"),
        category=raw.get("category"),
        is_family_expense=family,
        personal_spend_cny=_require_amount(raw.get("personal_spend_cny")),
    )
