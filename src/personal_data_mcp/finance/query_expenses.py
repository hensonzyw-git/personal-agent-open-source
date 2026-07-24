"""The governed read path behind ``finance.query_expenses`` (DEV-022).

The expense table's stored amount is not the amount this user personally bears:
family expenses are reduced by a Base formula and refunds / AA receipts are
negative.  This module therefore never reconstructs accounting policy from the
five writable fields.  It reads the validated ``个人支出`` formula, filters and
aggregates its signed Decimal values in deterministic code, and paginates the
source to exhaustion before it reports an aggregate.

The cursor is intentionally a server-signed continuation, not Feishu's
``page_token``.  Local filters (notably name fragments and formula amounts)
make a provider cursor insufficient, and exposing it would let a caller couple
to an implementation detail. A continuation binds the canonical filters,
``records`` view, active config, ordered result snapshot, offset and fixed
expiry; every continuation rescan is complete, so a mutable ledger cannot turn
an offset into duplicate or missing records.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Final

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json
from personal_agent_core.money import AmountError, format_cny, parse_signed_amount
from personal_agent_core.timeutil import (
    format_ledger_date,
    parse_ledger_date,
    to_rfc3339,
    to_utc,
    utc_now,
)
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.endpoints import SEARCH_RECORDS
from personal_data_mcp.finance.expense_record import as_decimal, as_ledger_date, as_text
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.finance.source_guard import require_validated_source


PAGE_SIZE: Final[int] = 500
MAX_PAGES: Final[int] = 200
RECORDS_PAGE_SIZE: Final[int] = 50
CURSOR_VERSION: Final[int] = 2
CURSOR_TTL: Final[timedelta] = timedelta(minutes=10)
MIN_CURSOR_SECRET_BYTES: Final[int] = 32


@dataclass(frozen=True)
class AmountRange:
    minimum: Decimal | None
    minimum_inclusive: bool
    maximum: Decimal | None
    maximum_inclusive: bool


@dataclass(frozen=True)
class QueryFilters:
    date_start: date | None
    date_end: date | None
    categories: tuple[str, ...]
    name_contains: tuple[str, ...]
    is_family_expense: str
    personal_amount: AmountRange | None


@dataclass(frozen=True)
class QueryExpense:
    record_id: str
    name: str | None
    occurred_on: date | None
    category: str | None
    is_family_expense: bool
    personal_spend_cny: Decimal


@dataclass(frozen=True)
class DecodedCursor:
    filters: QueryFilters
    offset: int
    expires_at: datetime
    config_checksum: str
    snapshot_checksum: str


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _as_bool(value: Any, *, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid(f"{name} must be boolean")
    return value


def _parse_filters(arguments: dict[str, Any]) -> QueryFilters:
    raw_dates = arguments.get("date_range")
    start: date | None = None
    end: date | None = None
    if raw_dates is not None:
        if not isinstance(raw_dates, dict):
            raise _invalid("date_range must be an object or null")
        try:
            start = parse_ledger_date(raw_dates.get("start"))
            end = parse_ledger_date(raw_dates.get("end"))
        except (TypeError, ValueError) as exc:
            raise _invalid("date_range requires absolute ISO dates") from exc
        if end < start:
            raise _invalid("date_range end must not precede start")

    raw_categories = arguments.get("categories", [])
    if not isinstance(raw_categories, list) or not all(
        isinstance(value, str) for value in raw_categories
    ):
        raise _invalid("categories must be an array of strings")
    if len(raw_categories) != len(set(raw_categories)):
        raise _invalid("categories must not contain duplicates")

    raw_names = arguments.get("name_contains", [])
    if not isinstance(raw_names, list) or not all(
        isinstance(value, str) and value and len(value) <= 40 for value in raw_names
    ):
        raise _invalid("name_contains must be short non-empty text fragments")

    family = arguments.get("is_family_expense", "all")
    if family not in {"all", "true", "false"}:
        raise _invalid("is_family_expense must be all, true or false")

    raw_amount = arguments.get("personal_amount_cny")
    amount: AmountRange | None = None
    if raw_amount is not None:
        if not isinstance(raw_amount, dict):
            raise _invalid("personal_amount_cny must be an object or null")
        try:
            minimum = (
                parse_signed_amount(raw_amount["min"])
                if raw_amount.get("min") is not None
                else None
            )
            maximum = (
                parse_signed_amount(raw_amount["max"])
                if raw_amount.get("max") is not None
                else None
            )
        except (AmountError, KeyError) as exc:
            raise _invalid("personal_amount_cny bounds must be exact decimals") from exc
        if minimum is None and maximum is None:
            raise _invalid("personal_amount_cny needs at least one bound")
        minimum_inclusive = _as_bool(
            raw_amount.get("min_inclusive"),
            default=True,
            name="min_inclusive",
        )
        maximum_inclusive = _as_bool(
            raw_amount.get("max_inclusive"),
            default=True,
            name="max_inclusive",
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _invalid("personal_amount_cny min must not exceed max")
        amount = AmountRange(
            minimum=minimum,
            minimum_inclusive=minimum_inclusive,
            maximum=maximum,
            maximum_inclusive=maximum_inclusive,
        )

    return QueryFilters(
        date_start=start,
        date_end=end,
        categories=tuple(raw_categories),
        name_contains=tuple(raw_names),
        is_family_expense=family,
        personal_amount=amount,
    )


def _serialise_filters(filters: QueryFilters) -> dict[str, Any]:
    return {
        "date_range": (
            {"start": format_ledger_date(filters.date_start), "end": format_ledger_date(filters.date_end)}
            if filters.date_start is not None and filters.date_end is not None
            else None
        ),
        "categories": list(filters.categories),
        "name_contains": list(filters.name_contains),
        "is_family_expense": filters.is_family_expense,
        "personal_amount_cny": (
            {
                "min": str(filters.personal_amount.minimum)
                if filters.personal_amount.minimum is not None
                else None,
                "min_inclusive": filters.personal_amount.minimum_inclusive,
                "max": str(filters.personal_amount.maximum)
                if filters.personal_amount.maximum is not None
                else None,
                "max_inclusive": filters.personal_amount.maximum_inclusive,
            }
            if filters.personal_amount is not None
            else None
        ),
    }


def _filters_from_cursor(raw: dict[str, Any]) -> QueryFilters:
    # Reuse the normal model-input parser rather than creating a weaker cursor
    # parser.  The cursor is signed, but rejecting malformed historic payloads
    # still gives a stable failure instead of a server exception.
    return _parse_filters(raw)


def _require_bounded(filters: QueryFilters) -> None:
    if (
        filters.date_start is None
        and not filters.categories
        and not filters.name_contains
        and filters.is_family_expense == "all"
        and filters.personal_amount is None
    ):
        raise AppError(
            ErrorCode.CLARIFICATION_REQUIRED,
            internal_detail="expense query has no date range or other limiting filter",
        )


def _encode_cursor(
    *,
    filters: QueryFilters,
    offset: int,
    expires_at: datetime,
    config_checksum: str,
    snapshot_checksum: str,
    secret: bytes,
) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "view": "records",
        "filters": _serialise_filters(filters),
        "offset": offset,
        "exp": int(expires_at.timestamp()),
        "config_checksum": config_checksum,
        "snapshot_checksum": snapshot_checksum,
    }
    raw = canonical_json(payload).encode("utf-8")
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return ".".join(
        base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        for value in (raw, signature)
    )


def _decode_cursor(
    cursor: str, *, secret: bytes, now: datetime
) -> DecodedCursor:
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        padding = "=" * (-len(encoded_payload) % 4)
        raw = base64.urlsafe_b64decode(encoded_payload + padding)
        padding = "=" * (-len(encoded_signature) % 4)
        supplied_signature = base64.urlsafe_b64decode(encoded_signature + padding)
        expected_signature = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature mismatch")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("v") != CURSOR_VERSION
            or payload.get("view") != "records"
            or not isinstance(payload.get("filters"), dict)
            or type(payload.get("offset")) is not int
            or payload["offset"] < 0
            or type(payload.get("exp")) is not int
            or payload["exp"] <= int(now.timestamp())
            or not isinstance(payload.get("config_checksum"), str)
            or len(payload["config_checksum"]) != 64
            or not isinstance(payload.get("snapshot_checksum"), str)
            or len(payload["snapshot_checksum"]) != 64
        ):
            raise ValueError("invalid cursor payload")
        return DecodedCursor(
            filters=_filters_from_cursor(payload["filters"]),
            offset=payload["offset"],
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
            config_checksum=payload["config_checksum"],
            snapshot_checksum=payload["snapshot_checksum"],
        )
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _invalid("cursor is invalid or expired") from exc


def _matches(row: QueryExpense, filters: QueryFilters) -> bool:
    if filters.date_start is not None and (
        row.occurred_on is None
        or row.occurred_on < filters.date_start
        or row.occurred_on > filters.date_end
    ):
        return False
    if filters.categories and row.category not in filters.categories:
        return False
    if filters.name_contains and (
        row.name is None
        or any(fragment not in row.name for fragment in filters.name_contains)
    ):
        return False
    if filters.is_family_expense == "true" and not row.is_family_expense:
        return False
    if filters.is_family_expense == "false" and row.is_family_expense:
        return False
    amount = filters.personal_amount
    if amount is not None:
        if amount.minimum is not None and (
            row.personal_spend_cny < amount.minimum
            or (
                row.personal_spend_cny == amount.minimum
                and not amount.minimum_inclusive
            )
        ):
            return False
        if amount.maximum is not None and (
            row.personal_spend_cny > amount.maximum
            or (
                row.personal_spend_cny == amount.maximum
                and not amount.maximum_inclusive
            )
        ):
            return False
    return True


def _as_personal_spend_formula(value: Any) -> Decimal | None:
    """Normalise the documented Bitable formula-cell envelope.

    ``search_records`` returns ordinary number cells directly, but the live
    expense formula is an envelope such as ``{"type": 2, "value": [20]}``.
    ``2`` is the formula result's number-cell type, not the schema field type;
    the latter has already been proven to be ``formula`` by the protected
    config and fresh validator. The one-element value list is a provider
    representation, not a collection of components to sum. Accept only that
    exact numeric shape; a changed formula response is unavailable, never
    silently interpreted as zero.
    """
    if not isinstance(value, dict) or value.get("type") != 2:
        return None
    raw = value.get("value")
    if not isinstance(raw, list) or len(raw) != 1:
        return None
    return as_decimal(raw[0])


async def _read_all_matching_source_rows(
    adapter: FeishuAdapter,
    *,
    source: BaseSource,
    config: LedgerConfig,
    view: str,
    filters: QueryFilters,
) -> tuple[list[QueryExpense], int]:
    fields = config.tables["expense"].fields
    # Request no more than the selected view and active filters require. The
    # formula is always necessary: every view uses signed personal spend, never
    # the raw stored amount.
    required = {"personal_spend"}
    if view == "records":
        required.update({"name", "occurred_on", "category", "is_family_expense"})
    elif view == "by_category":
        required.add("category")
    if filters.date_start is not None:
        required.add("occurred_on")
    if filters.categories:
        required.add("category")
    if filters.name_contains:
        required.add("name")
    if filters.is_family_expense != "all":
        required.add("is_family_expense")
    field_names = [fields[logical].expected_name for logical in sorted(required)]
    rows: list[QueryExpense] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()

    for page_number in range(1, MAX_PAGES + 1):
        query = {"page_size": str(PAGE_SIZE)}
        if page_token:
            query["page_token"] = page_token
        data = await adapter.request(
            SEARCH_RECORDS,
            params={
                "app_token": source.base_token,
                "table_id": source.tables["expense"],
            },
            json={"field_names": field_names, "automatic_fields": False},
            query=query,
        )
        items = data.get("items")
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="expense query returned malformed items",
            )
        for item in items:
            record_id = item.get("record_id")
            cells = item.get("fields")
            if not isinstance(record_id, str) or not record_id or not isinstance(cells, dict):
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail="expense query returned a malformed record",
                )
            personal_spend = _as_personal_spend_formula(
                cells.get(fields["personal_spend"].expected_name)
            )
            if personal_spend is None:
                # A missing/garbled formula is not a zero.  Treating it as one
                # would make an incomplete page look like a correct total.
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail="expense query could not read personal-spend formula",
                )
            raw_family = cells.get(fields["is_family_expense"].expected_name)
            if raw_family is None:
                family = False
            elif isinstance(raw_family, bool):
                family = raw_family
            else:
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail="expense query returned malformed family flag",
                )
            name = as_text(cells.get(fields["name"].expected_name))
            try:
                occurred_on = as_ledger_date(
                    cells.get(fields["occurred_on"].expected_name)
                )
            except (OSError, OverflowError, ValueError):
                occurred_on = None
            category = as_text(cells.get(fields["category"].expected_name))
            parsed_required = {
                "name": name,
                "occurred_on": occurred_on,
                "category": category,
            }
            malformed_required = [
                logical_name
                for logical_name, parsed in parsed_required.items()
                if logical_name in required and parsed is None
            ]
            if malformed_required:
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail=(
                        "expense query could not read required fields: "
                        + ", ".join(sorted(malformed_required))
                    ),
                )
            rows.append(
                QueryExpense(
                    record_id=record_id,
                    name=name,
                    occurred_on=occurred_on,
                    category=category,
                    is_family_expense=family,
                    personal_spend_cny=personal_spend,
                )
            )

        has_more = data.get("has_more")
        if not isinstance(has_more, bool):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="expense query returned malformed has_more",
            )
        if not has_more:
            return rows, page_number
        page_token = data.get("page_token")
        if not isinstance(page_token, str) or not page_token:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="expense query reported more pages without a cursor",
            )
        if page_token in seen_page_tokens:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="expense query repeated a source cursor",
            )
        seen_page_tokens.add(page_token)

    raise AppError(
        ErrorCode.SOURCE_UNAVAILABLE,
        internal_detail="expense query did not terminate source pagination",
    )


def _record_view(row: QueryExpense) -> dict[str, Any]:
    return {
        "record_id": row.record_id,
        "name": row.name,
        "occurred_on": format_ledger_date(row.occurred_on) if row.occurred_on else None,
        "category": row.category,
        "is_family_expense": row.is_family_expense,
        "personal_spend_cny": format_cny(row.personal_spend_cny),
    }


def _records_snapshot_checksum(rows: list[QueryExpense]) -> str:
    """Bind an offset cursor to the exact ordered matching result set."""
    return hashlib.sha256(
        canonical_json([_record_view(row) for row in rows]).encode("utf-8")
    ).hexdigest()


async def query_expenses(
    arguments: dict[str, Any],
    *,
    adapter: FeishuAdapter,
    source: BaseSource,
    config: LedgerConfig,
    validation: SchemaValidation,
    cursor_secret: bytes,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Apply the frozen query contract and return a structured, read-only result.

    The caller is responsible for authentication and JSON-Schema validation.  We
    nevertheless validate semantics here because this function is also the safe
    entry point for operator tooling and tests; no internal caller gets to turn
    an empty query into a whole-ledger read by bypassing the MCP boundary.
    """
    if (
        not isinstance(cursor_secret, bytes)
        or len(cursor_secret) < MIN_CURSOR_SECRET_BYTES
    ):
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail=(
                "query cursor signing key must contain at least 32 bytes"
            ),
        )
    if not isinstance(arguments, dict):
        raise _invalid("query arguments must be an object")
    view = arguments.get("view")
    if view not in {"total", "by_category", "records"}:
        raise _invalid("view must be total, by_category or records")

    started_at = to_utc(now())
    cursor = arguments.get("cursor")
    offset = 0
    decoded_cursor: DecodedCursor | None = None
    if cursor is not None:
        if view != "records" or not isinstance(cursor, str) or not cursor:
            raise _invalid("cursor is only valid for records view")
        if any(
            key in arguments
            for key in (
                "date_range",
                "categories",
                "name_contains",
                "is_family_expense",
                "personal_amount_cny",
            )
        ):
            raise _invalid("a cursor continuation must not replace its filters")
        decoded_cursor = _decode_cursor(
            cursor, secret=cursor_secret, now=started_at
        )
        filters = decoded_cursor.filters
        offset = decoded_cursor.offset
        if not hmac.compare_digest(
            decoded_cursor.config_checksum, config.checksum()
        ):
            raise _invalid("cursor does not match the active ledger config")
    else:
        filters = _parse_filters(arguments)
        _require_bounded(filters)
    known_categories = config.tables["expense"].fields["category"].options or ()
    if any(category not in known_categories for category in filters.categories):
        raise _invalid("categories contains a value not allowed by the active ledger")

    require_validated_source(
        config=config,
        validation=validation,
        source=source,
        operation="expense query",
    )
    source_rows, scanned_pages = await _read_all_matching_source_rows(
        adapter, source=source, config=config, view=view, filters=filters
    )
    matching = [row for row in source_rows if _matches(row, filters)]
    personal_total = sum(
        (row.personal_spend_cny for row in matching), Decimal("0.00")
    )
    completed_at = to_utc(now())
    result: dict[str, Any] = {
        "status": "ok",
        "view": view,
        "filters_applied": _serialise_filters(filters),
        "metric": "personal_spend_total_cny",
        "record_count": len(matching),
        "source_system": "feishu_bitable",
        "evidence": {
            "kind": "aggregate_query",
            "query_id": "qry_" + secrets.token_hex(12),
            "config_checksum": config.checksum(),
            "schema_snapshot_checksum": validation.snapshot_checksum,
            "scanned_pages": scanned_pages,
            "matched_count": len(matching),
            "started_at": to_rfc3339(started_at),
            "completed_at": to_rfc3339(completed_at),
        },
    }

    if view == "total":
        result["personal_spend_total_cny"] = format_cny(personal_total)
    elif view == "by_category":
        buckets: dict[str | None, list[QueryExpense]] = {}
        for row in matching:
            buckets.setdefault(row.category, []).append(row)
        result["personal_spend_total_cny"] = format_cny(personal_total)
        result["by_category"] = [
            {
                "category": category,
                "personal_spend_total_cny": format_cny(
                    sum((row.personal_spend_cny for row in rows), Decimal("0.00"))
                ),
                "record_count": len(rows),
                "share_of_total": (
                    format_cny(
                        sum((row.personal_spend_cny for row in rows), Decimal("0.00"))
                        / personal_total
                        * Decimal("100")
                    )
                    if personal_total != 0
                    else None
                ),
            }
            for category, rows in sorted(
                buckets.items(), key=lambda pair: (pair[0] is None, pair[0] or "")
            )
        ]
    else:
        # The cursor is an offset into this deterministic presentation order,
        # not an exposed provider token. Bind the offset to the complete ordered
        # matching result so a mutable ledger can never duplicate or skip rows.
        matching.sort(
            key=lambda row: (row.occurred_on or date.min, row.record_id), reverse=True
        )
        snapshot_checksum = _records_snapshot_checksum(matching)
        if decoded_cursor is not None and not hmac.compare_digest(
            decoded_cursor.snapshot_checksum, snapshot_checksum
        ):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail=(
                    "expense query result changed during pagination; restart query"
                ),
            )
        page = matching[offset : offset + RECORDS_PAGE_SIZE]
        next_offset = offset + len(page)
        result["records"] = [_record_view(row) for row in page]
        result["next_cursor"] = (
            _encode_cursor(
                filters=filters,
                offset=next_offset,
                expires_at=(
                    decoded_cursor.expires_at
                    if decoded_cursor is not None
                    else started_at + CURSOR_TTL
                ),
                config_checksum=config.checksum(),
                snapshot_checksum=snapshot_checksum,
                secret=cursor_secret,
            )
            if next_offset < len(matching)
            else None
        )
    return result
