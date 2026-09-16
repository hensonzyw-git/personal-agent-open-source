"""Correcting one expense's 分类, and nothing else about it.

Henson asked (2026-08-15) to fix a mis-categorised expense straight from the
receipt card. The model gets 分类 wrong often enough that re-describing the
whole entry to the Agent is the wrong repair; a picker is. But an edit to a
committed ledger row is still a governed write, so it goes through the same
`prepared → submitting → committed_unverified → succeeded` skeleton as a create,
with the same audit, the same external receipt and the same read-back rule.

Four things differ from `execute_governed_write`, each because an update is a
genuinely different operation and not because this path is a shortcut:

- **The payload is one field, structurally.** Bitable's update is partial, so a
  body naming only 分类 cannot address 名称, 金额, 日期 or 是否家庭支出. This is
  the primary safety property: a correction that is *wrong about the row* still
  cannot rewrite the amount. `_category_payload` is the only thing that builds
  the body and it takes a category, not a dict.

- **There is no `client_token`.** Feishu offers create idempotency, not update
  idempotency, so replaying a lost update would be a guess. Safety comes from
  the compare-and-swap instead: the caller states the category it believes the
  row currently holds, this path re-reads the row and refuses if reality
  disagrees. A replay after a lost response then finds the row already at the
  target value and reports success without sending anything -- which is what
  makes the operation naturally idempotent rather than nominally so.

- **Read-back proves two things, not one.** That 分类 is now the requested
  value, *and* that every other configured field still equals what the pre-read
  saw. Verifying only the changed field would accept a Base automation, a
  concurrent edit or a malformed request that moved something else at the same
  time. A mismatch on any field is `needs_manual_review`, never a silent pass.

- **A stale expectation is a refusal, not a merge.** If the row's category is
  neither the expected value nor the requested one, someone or something else
  changed it since the card was drawn. Overwriting would discard a decision this
  process cannot see. `CATEGORY_CHANGED_ELSEWHERE` sends the fresh value back so
  the card can show what the ledger actually holds and let Henson decide again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import to_rfc3339, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.expense_record import (
    as_checkbox,
    as_decimal,
    as_ledger_date,
    as_text,
)
from personal_data_mcp.finance.ledger_config import FieldType, LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.finance.source_guard import require_validated_source
from personal_data_mcp.storage.execution_store import (
    append_audit_event,
    mark_receipt_verified,
    prepare_execution,
    record_receipt,
    transition,
)


TOOL = "finance.update_expense_category"
TABLE_KIND = "expense"

#: How many times the read-back may be attempted before the outcome is unknown.
#: Matched to the create path's budget for the same reason: a read that will not
#: answer is not evidence either way.
READ_BACK_ATTEMPTS = 3


@dataclass(frozen=True)
class CategoryUpdateOutcome:
    """Proof, not a claim: every field came back from the ledger."""

    #: `"updated"` when this call sent the change; `"already_current"` when the
    #: row was found at the requested value and nothing was sent. Both are
    #: successes, and the distinction is kept because "we changed it" and "it was
    #: already so" are different facts about the world.
    status: str
    record_id: str
    category: str
    updated_at: datetime
    #: The row as the ledger holds it now, keyed by Feishu field name.
    stored_fields: dict[str, Any]


def _audit(
    session: Session,
    *,
    trace_id: str,
    event_type: str,
    summary: str,
    now: datetime,
) -> None:
    append_audit_event(
        session,
        event_id=str(uuid.uuid4()),
        trace_id=trace_id,
        event_type=event_type,
        redacted_summary=summary,
        now=now,
    )


def _expense_fields(config: LedgerConfig) -> dict[str, Any]:
    table = config.tables.get(TABLE_KIND)
    if table is None:  # pragma: no cover - the config validator forbids this
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail="the ledger config has no expense table",
        )
    return table.fields


def _require_allowed_category(category: str, config: LedgerConfig) -> None:
    """Only an option the ledger already has.

    The connector never creates a select option (design 9.2), so an unknown
    category is a refusal here rather than a request Feishu would reject with a
    less legible error -- and rather than a new option quietly appearing in
    Henson's ledger because a client sent a typo.
    """
    spec = _expense_fields(config)["category"]
    if category not in (spec.options or ()):
        raise AppError(
            ErrorCode.CATEGORY_NOT_ALLOWED,
            internal_detail=f"category {category!r} is not a ledger option",
        )


def _category_payload(category: str, config: LedgerConfig) -> dict[str, Any]:
    """The entire update body: one field.

    Deliberately not a `dict[str, Any]` parameter anywhere in this module. A
    caller that could pass a dict could pass 原始金额, and Bitable would apply
    it. The type signature is the guard.
    """
    return {_expense_fields(config)["category"].expected_name: category}


def _stored_category(stored: dict[str, Any], config: LedgerConfig) -> str | None:
    return as_text(stored.get(_expense_fields(config)["category"].expected_name))


def _unchanged_except_category(
    before: dict[str, Any], after: dict[str, Any], *, config: LedgerConfig
) -> list[str]:
    """Logical names of any field other than 分类 that moved during the update.

    Compared through the same normalisers a create's read-back uses, so a cell
    that legitimately round-trips differently (`20` vs `20.0`, rich text vs a
    plain string) is not reported as a change. The formula field is skipped
    because it is *expected* to move: 个人支出 may well be derived from 分类,
    which is the whole reason the receipt drops its cached value after an edit.
    """
    moved: list[str] = []
    for logical, spec in _expense_fields(config).items():
        if logical == "category" or spec.type is FieldType.FORMULA:
            continue
        name = spec.expected_name
        if spec.type is FieldType.NUMBER:
            same = as_decimal(before.get(name)) == as_decimal(after.get(name))
        elif spec.type is FieldType.DATETIME:
            same = as_ledger_date(before.get(name)) == as_ledger_date(after.get(name))
        elif spec.type is FieldType.CHECKBOX:
            same = as_checkbox(before.get(name)) == as_checkbox(after.get(name))
        else:
            same = as_text(before.get(name)) == as_text(after.get(name))
        if not same:
            moved.append(logical)
    return moved


def _changed_elsewhere(found: str | None) -> AppError:
    """Someone else moved this row's category since the card was drawn.

    The current value travels in `internal_detail` rather than being swallowed:
    the caller turns it into the fresh value the card shows, so Henson decides
    again with the truth in front of him instead of being told only "no".
    """
    return AppError(
        ErrorCode.CATEGORY_CHANGED_ELSEWHERE,
        internal_detail=f"the ledger now holds category {found!r}",
    )


async def update_expense_category(
    *,
    record_id: str,
    category: str,
    expected_current_category: str | None,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    config: LedgerConfig,
    validation: SchemaValidation,
    source: BaseSource,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    now: Callable[[], datetime] = utc_now,
) -> CategoryUpdateOutcome:
    """Move one expense row to `category`, or refuse and change nothing."""
    require_validated_source(
        config=config,
        validation=validation,
        source=source,
        operation="update_expense_category",
    )
    _require_allowed_category(category, config)
    table_id = source.tables[TABLE_KIND]

    # --- step 0: read the row, before any execution row exists ---------------
    # A read has no side effect, so every refusal reachable from here -- an
    # unknown record, a stale expectation, an already-current row -- costs
    # nothing and leaves nothing to reconcile.
    before = _fields_of(await adapter.get_record(source.base_token, table_id, record_id))
    found = _stored_category(before, config)

    if found == category:
        # Already where the caller wants it. This is the replay case after a
        # lost response, and it is a success with no request sent. Reporting a
        # failure here would push Henson to press the button again, which is
        # exactly the loop idempotency exists to prevent.
        return CategoryUpdateOutcome(
            status="already_current",
            record_id=record_id,
            category=category,
            updated_at=now(),
            stored_fields=before,
        )
    if found != expected_current_category:
        raise _changed_elsewhere(found)

    # --- step 1: prepared, committed before any network ----------------------
    with sessions() as session:
        execution = prepare_execution(
            session,
            idempotency_key=idempotency_key,
            tool=TOOL,
            request_fingerprint=request_fingerprint,
            # An update has no provider-side idempotency token. The column is
            # not nullable, so the key itself is stored: it is never sent, and
            # storing something meaningless would be worse than storing the one
            # value that at least identifies the request.
            client_token=idempotency_key,
            encrypted_payload=None,
            now=now(),
        )
        if execution.state == "succeeded":
            # A completed replay. The row read above was not at the target
            # value, so the ledger has moved since -- report that rather than
            # re-sending under a spent key.
            session.commit()
            raise _changed_elsewhere(found)
        if execution.state != "prepared":
            raise AppError(
                ErrorCode.SOURCE_COMMIT_UNKNOWN,
                internal_detail=(
                    f"{idempotency_key} is at {execution.state}; "
                    "recovery belongs to the reconciler"
                ),
            )
        state_version = execution.state_version
        _audit(
            session,
            trace_id=trace_id,
            event_type="category_update_prepared",
            summary=f"prepared {TOOL} for record {record_id}",
            now=now(),
        )
        session.commit()

    # --- step 2: submitting, committed before the request leaves -------------
    with sessions() as session:
        state_version = transition(
            session,
            idempotency_key=idempotency_key,
            current_state="prepared",
            current_version=state_version,
            target_state="submitting",
            now=now(),
        )
        session.commit()

    # --- step 3: the one update ----------------------------------------------
    try:
        await adapter.update_record(
            source.base_token,
            table_id,
            record_id,
            fields=_category_payload(category, config),
        )
    except AppError as error:
        with sessions() as session:
            transition(
                session,
                idempotency_key=idempotency_key,
                current_state="submitting",
                current_version=state_version,
                target_state="commit_unknown",
                now=now(),
                failure_code=error.code.value,
            )
            _audit(
                session,
                trace_id=trace_id,
                event_type="category_update_commit_unknown",
                summary=f"update failed with {error.code.value}",
                now=now(),
            )
            session.commit()
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail=f"category update failed as {error.code.value}",
        ) from error

    # --- step 4: receipt first, then committed_unverified --------------------
    # The record id was known before the call, but the receipt is still written
    # here and not earlier: it records that *this execution* touched that row,
    # and writing it before the request left would claim something untrue.
    with sessions() as session:
        record_receipt(
            session,
            receipt_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            table_kind=TABLE_KIND,
            record_id=record_id,
            now=now(),
        )
        state_version = transition(
            session,
            idempotency_key=idempotency_key,
            current_state="submitting",
            current_version=state_version,
            target_state="committed_unverified",
            now=now(),
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="category_update_committed_unverified",
            summary="update accepted; read-back pending",
            now=now(),
        )
        session.commit()

    # --- step 5: read back ---------------------------------------------------
    after: dict[str, Any] | None = None
    last_error: AppError | None = None
    for _ in range(READ_BACK_ATTEMPTS):
        try:
            after = _fields_of(
                await adapter.get_record(source.base_token, table_id, record_id)
            )
        except AppError as error:
            last_error = error
            continue
        break

    if after is None:
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail=(
                f"category updated but read-back unavailable after "
                f"{READ_BACK_ATTEMPTS} attempts"
                + (f" ({last_error.code.value})" if last_error else "")
            ),
        )

    mismatches: list[str] = []
    if _stored_category(after, config) != category:
        mismatches.append("category")
    mismatches.extend(
        _unchanged_except_category(before, after, config=config)
    )
    if mismatches:
        with sessions() as session:
            transition(
                session,
                idempotency_key=idempotency_key,
                current_state="committed_unverified",
                current_version=state_version,
                target_state="needs_manual_review",
                now=now(),
                failure_code=ErrorCode.SOURCE_COMMITTED_MISMATCH.value,
            )
            _audit(
                session,
                trace_id=trace_id,
                event_type="category_update_mismatch",
                summary="read-back mismatch on " + ",".join(mismatches),
                now=now(),
            )
            session.commit()
        raise AppError(
            ErrorCode.SOURCE_COMMITTED_MISMATCH,
            internal_detail="read-back mismatch on " + ",".join(mismatches),
        )

    # --- step 6: verified, and only now succeeded ----------------------------
    updated_at = now()
    with sessions() as session:
        mark_receipt_verified(
            session, idempotency_key=idempotency_key, now=updated_at
        )
        transition(
            session,
            idempotency_key=idempotency_key,
            current_state="committed_unverified",
            current_version=state_version,
            target_state="succeeded",
            now=updated_at,
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="category_update_succeeded",
            # The values themselves are ledger content and stay out of the audit
            # summary, in line with the write audit's `never_recorded` list.
            summary="category changed and every other field verified unchanged",
            now=updated_at,
        )
        session.commit()

    return CategoryUpdateOutcome(
        status="updated",
        record_id=record_id,
        category=category,
        updated_at=updated_at,
        stored_fields=after,
    )


def _fields_of(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="the ledger record carried no fields object",
        )
    return fields


def format_updated_at(moment: datetime) -> str:
    """The RFC 3339 stamp the receipt card shows beside an edited category."""
    return to_rfc3339(moment)
