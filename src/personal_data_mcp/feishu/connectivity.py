"""G2 read-only connectivity probe against the synthetic test Base.

This is the first action that uses real credentials and a real Base, and it is
deliberately read-only: mint a tenant token, list each configured table's fields,
and emit a redacted discovery report. It never writes -- G3 is the first write --
and it refuses to run unless the Base is marked the synthetic test one.

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
from typing import Any

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    BaseSource,
    load_base_source,
    require_synthetic_test_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.finance.schema_validator import observed_field_from_feishu


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


async def probe(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the read-only probe, returning a redacted discovery report."""
    credentials = load_credentials(env)
    source = require_synthetic_test_base(load_base_source(env))

    async with FeishuAdapter(credentials, now=time.monotonic) as adapter:
        token = await adapter.tenant_token()
        token_obtained = bool(token)

        tables: dict[str, Any] = {}
        for kind, table_id in source.tables.items():
            fields = await adapter.list_fields(source.base_token, table_id)
            tables[kind] = {
                "table_id_hash": _hash(table_id),
                "field_count": len(fields),
                "fields": [_field_view(f) for f in fields],
            }

    return {
        "ledger_kind": source.ledger_kind,
        "base_token_hash": _hash(source.base_token),
        "tenant_token_obtained": token_obtained,
        "tables": tables,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only G2 connectivity probe against the synthetic test Base. "
            "Reads credentials and Base ids from the environment; writes nothing."
        )
    )
    parser.parse_args()

    report = asyncio.run(probe())
    # Redact the serialised report as a backstop before it reaches stdout.
    print(redact_for_log(json.dumps(report, ensure_ascii=False, indent=2)))
    sys.exit(0 if report["tenant_token_obtained"] else 1)
