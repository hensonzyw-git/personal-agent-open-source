"""The G3 operator command: one expense write against the synthetic test Base.

This exists because the first real write must be a deliberate, single, fully
specified act -- not a side effect of a model call. `finance.log_expense` is not
registered as an MCP tool yet: the pre-write duplicate check (DEV-020) and the
category/trip-tag resolvers (DEV-021) are not built, and the registry only
advertises tools that can actually execute. So the entry arrives here already
resolved, from a human, on the command line.

Everything the write path enforces still applies, and two guards are added
around it:

- the Base must be the synthetic test one, bound to the protected config exactly
  as the probe binds it; `production` is refused outright, before G5;
- the live schema is fetched and validated *in this run*. A stale validation is
  not accepted, because the payload is addressed by field name and only a fresh
  validation proves those names still belong to the configured ids.

It prints the external evidence -- record id and the fields as the ledger now
holds them -- because that, not a success message, is what proves a write.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from decimal import InvalidOperation
from pathlib import Path

from personal_agent_core.errors import AppError
from personal_agent_core.manifest import canonical_json
from personal_agent_core.money import apply_entry_sign, parse_amount
from personal_agent_core.timeutil import ledger_date, parse_ledger_date, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    SYNTHETIC_TEST_KIND,
    LedgerSourceError,
    load_base_source,
    require_synthetic_test_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.finance.expense_record import ExpenseEntry
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.schema_validator import (
    observed_field_from_feishu,
    validate_schema,
)
from personal_data_mcp.finance.write_path import write_expense
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)


def request_fingerprint(entry: ExpenseEntry) -> str:
    """A stable fingerprint of the semantic request.

    It binds an idempotency key to *this* entry: replaying the key with any
    different field is a conflict rather than a silent second write.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "tool": "finance.log_expense",
                "name": entry.name,
                "amount_cny": str(entry.amount_cny),
                "occurred_on": entry.occurred_on.isoformat(),
                "is_family_expense": entry.is_family_expense,
                "category": entry.category,
            }
        ).encode("utf-8")
    ).hexdigest()


async def run(
    entry: ExpenseEntry, *, config_path: Path, db_path: Path, idempotency_key: str
) -> dict:
    config = load_ledger_config(
        json.loads(config_path.read_text(encoding="utf-8"))
    )
    if config.ledger_kind != SYNTHETIC_TEST_KIND:
        raise LedgerSourceError(
            f"refusing to write: this command only accepts a "
            f"{SYNTHETIC_TEST_KIND!r} config. A production write is a G5 "
            "decision."
        )
    credentials = load_credentials()
    source = require_synthetic_test_base(
        load_base_source(),
        approved_base_token=config.base_token,
        approved_tables={
            kind: table.table_id for kind, table in config.tables.items()
        },
        approved_ledger_kind=config.ledger_kind,
    )

    engine = create_database_engine(db_path)
    create_all(engine)
    sessions = session_factory(engine)
    try:
        async with FeishuAdapter(credentials, now=time.monotonic) as adapter:
            # Fresh schema validation, this run, before any write.
            observed = {}
            for kind, table_id in source.tables.items():
                fields = await adapter.list_fields(source.base_token, table_id)
                observed[kind] = [
                    observed_field_from_feishu(field) for field in fields
                ]
            validation = validate_schema(config, observed)

            outcome = await write_expense(
                entry,
                sessions=sessions,
                adapter=adapter,
                config=config,
                validation=validation,
                source=source,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint(entry),
                trace_id=f"g3-{idempotency_key}",
            )
    finally:
        engine.dispose()

    return {
        "status": outcome.status,
        "record_id": outcome.record_id,
        "source_system": "feishu_bitable",
        "table": "expense",
        "ledger_kind": source.ledger_kind,
        "config_checksum": config.checksum(),
        "committed_at": outcome.committed_at.isoformat(),
        "stored_fields": outcome.stored_fields,
    }


def build_entry(args: argparse.Namespace) -> ExpenseEntry:
    amount = parse_amount(args.amount)
    if args.entry_kind != "expense":
        # A refund or AA receipt is stored negative; the sign comes from the
        # kind, never from a typed minus (`money.apply_entry_sign`).
        amount = apply_entry_sign(amount, args.entry_kind)
    return ExpenseEntry(
        name=args.name,
        amount_cny=amount,
        # The default is *the ledger's* today, not the host's: `date.today()`
        # would silently use the machine's timezone and could book an entry on
        # the wrong ledger day.
        occurred_on=(
            parse_ledger_date(args.date) if args.date else ledger_date(utc_now())
        ),
        is_family_expense=args.scope == "family",
        category=args.category,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Write one fully resolved expense to the synthetic test Base (G3). "
            "Refuses any non-synthetic config."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--name", required=True, help="the user's text, verbatim")
    parser.add_argument("--amount", required=True, help="absolute value, no sign")
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    parser.add_argument(
        "--scope",
        required=True,
        choices=["personal", "family"],
        help="must be stated explicitly; there is no default and no inference",
    )
    parser.add_argument("--category", required=True)
    parser.add_argument(
        "--entry-kind", default="expense", choices=["expense", "refund", "aa_receipt"]
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="reuse a key to prove a replay writes nothing new",
    )
    args = parser.parse_args()

    try:
        entry = build_entry(args)
    except (InvalidOperation, ValueError) as exc:
        print(redact_for_log(f"invalid entry: {exc}"), file=sys.stderr)
        sys.exit(2)

    key = args.idempotency_key or str(uuid.uuid4())
    try:
        evidence = asyncio.run(
            run(
                entry,
                config_path=args.config,
                db_path=args.db,
                idempotency_key=key,
            )
        )
    except (AppError, LedgerSourceError) as exc:
        code = exc.code.value if isinstance(exc, AppError) else "LEDGER_SOURCE"
        print(
            redact_for_log(f"write refused or unresolved: {code} ({exc})"),
            file=sys.stderr,
        )
        print(f"idempotency_key={key}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"idempotency_key={key}")
    sys.exit(0)
