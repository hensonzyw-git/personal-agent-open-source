"""G2 read-only connectivity probe against the synthetic test Base.

This is the first action that uses real credentials and a real Base, and it is
deliberately read-only: mint a tenant token, list each configured table's fields,
validate all of them against a protected annual configuration, and emit a
redacted discovery report. It never writes -- G3 is the first write -- and it
refuses to run unless the environment exactly matches a protected synthetic-test
configuration.

The report is safe to show and to commit: every Base token, table id and field id
is hashed, only field names, types, formula flags and option sets survive in the
clear, and the whole thing is passed through the log redactor as a backstop. The
tenant token itself is never printed; the report records only that a token was
obtained.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    BaseSource,
    load_base_source,
    require_synthetic_test_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.finance.ledger_config import LedgerConfig, load_ledger_config
from personal_data_mcp.finance.schema_validator import (
    observed_field_from_feishu,
    validate_schema,
)


def _hash(raw: str) -> str:
    return "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _field_view(raw_field: dict[str, Any]) -> dict[str, Any]:
    observed = observed_field_from_feishu(raw_field)
    return {
        "field_id_hash": _hash(observed.field_id),
        "name": observed.name,
        "type": observed.type.value if observed.type else None,
        "is_formula": observed.is_formula,
        "is_auto_number": observed.is_auto_number,
        "options": list(observed.options) if observed.options else None,
    }


async def probe(
    env: dict[str, str] | None = None,
    *,
    config: LedgerConfig,
) -> dict[str, Any]:
    """Run the read-only probe, returning a redacted discovery report."""
    credentials = load_credentials(env)
    source = require_synthetic_test_base(
        load_base_source(env),
        approved_base_token=config.base_token,
        approved_tables={
            kind: table.table_id for kind, table in config.tables.items()
        },
        approved_ledger_kind=config.ledger_kind,
    )

    async with FeishuAdapter(credentials, now=time.monotonic) as adapter:
        token = await adapter.tenant_token()
        token_obtained = bool(token)

        tables: dict[str, Any] = {}
        observed = {}
        for kind, table_id in source.tables.items():
            fields = await adapter.list_fields(source.base_token, table_id)
            observed[kind] = [
                observed_field_from_feishu(field) for field in fields
            ]
            tables[kind] = {
                "table_id_hash": _hash(table_id),
                "field_count": len(fields),
                "fields": [_field_view(f) for f in fields],
            }
    validation = validate_schema(config, observed)

    return {
        "ledger_kind": source.ledger_kind,
        "base_token_hash": _hash(source.base_token),
        "tenant_token_obtained": token_obtained,
        "config_checksum": config.checksum(),
        "schema_status": validation.status,
        "schema_drifts": [
            {
                "table": drift.table,
                "logical_name": drift.logical_name,
                "kind": drift.kind.value,
            }
            for drift in validation.drifts
        ],
        "tables": tables,
    }


def probe_succeeded(report: dict[str, Any]) -> bool:
    """The complete G2 acceptance condition; token-only success is insufficient."""
    return (
        report.get("tenant_token_obtained") is True
        and report.get("schema_status") == "valid"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only G2 connectivity probe against the synthetic test Base. "
            "Reads credentials and Base ids from the environment; writes nothing."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="protected annual-ledger JSON config to validate against",
    )
    args = parser.parse_args()
    config = load_ledger_config(
        json.loads(args.config.read_text(encoding="utf-8"))
    )

    report = asyncio.run(probe(config=config))
    # Redact the serialised report as a backstop before it reaches stdout.
    print(redact_for_log(json.dumps(report, ensure_ascii=False, indent=2)))
    sys.exit(0 if probe_succeeded(report) else 1)
