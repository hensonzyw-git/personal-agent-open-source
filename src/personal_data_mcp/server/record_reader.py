"""Read one card's ledger records with one fresh schema validation.

`DEV-028`. This is the only read path in the service that exists to *show* a
record rather than to verify a write, so it is worth being explicit about what
it deliberately keeps:

- **it revalidates the live schema once per batch.** Record cells are addressed by
  field *name*, and a name is only trustworthy while the configured id still
  carries it. That is the same rule the write path follows, and it is why a
  renamed column produces a refusal instead of a card full of empty fields.
- **it reads through the protected config.** A table kind that is not in the
  config cannot be requested, and only configured fields are projected.
- **it never writes and never repairs.** A cell that cannot be parsed is
  reported as unreadable by `project_record`; nothing here fills in a default.
"""

from __future__ import annotations

from typing import Any

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.finance.record_view import project_record
from personal_data_mcp.server.finance_write import (
    FinanceWriteDependencies,
    fresh_validation,
)


def build_record_reader(dependencies: FinanceWriteDependencies):
    """Bind the adapter, source and protected config into one batch read."""

    async def read(records: list[tuple[str, str]]) -> list[dict[str, Any]]:
        table_ids: dict[str, str] = {}
        for table_kind, _ in records:
            if table_kind not in dependencies.config.tables:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail=f"unknown table kind {table_kind!r}",
                )
            table_id = dependencies.source.tables.get(table_kind)
            if table_id is None:
                raise AppError(
                    ErrorCode.SOURCE_SCHEMA_CHANGED,
                    internal_detail=f"the bound source has no {table_kind} table",
                )
            table_ids[table_kind] = table_id

        validation = await fresh_validation(dependencies)
        if not validation.is_valid:
            raise AppError(
                ErrorCode.SOURCE_SCHEMA_CHANGED,
                internal_detail=(
                    "the live schema drifted, so field names cannot be trusted"
                ),
            )

        results: list[dict[str, Any]] = []
        for table_kind, record_id in records:
            try:
                record = await dependencies.adapter.get_record(
                    dependencies.source.base_token,
                    table_ids[table_kind],
                    record_id,
                )
                fields = record.get("fields")
                if not isinstance(fields, dict):
                    raise AppError(
                        ErrorCode.SOURCE_UNAVAILABLE,
                        internal_detail="get_record returned no fields object",
                    )
                view = project_record(
                    fields,
                    config=dependencies.config,
                    table_kind=table_kind,
                    record_id=record_id,
                )
            except AppError:
                # One missing/unreadable record stays visible as unavailable
                # without hiding the other rows on the card.
                results.append(
                    {
                        "status": "unavailable",
                        "table_kind": table_kind,
                        "record_id": record_id,
                    }
                )
                continue
            results.append({"status": "found", "record": view.to_json()})
        return results

    return read
