"""The G3 operator command: one expense write against the synthetic test Base.

This exists because the first real writes must be deliberate, single, fully
specified acts -- not side effects of a model call. `finance.log_expense` is not
registered as an MCP tool yet, and the registry only advertises tools that can
actually execute. So the semantics arrive here from a human, in exactly the
shape the model is contracted to produce, and go through the same resolvers
(DEV-021) and the same pre-write duplicate gate (DEV-020) a tool call will.

A `possible_duplicate` result is a question, not a failure: it returns a
`duplicate_check_id` and writes nothing, and re-running with
`--duplicate-override <id>` releases exactly that decision -- the server
re-checks and refuses if the candidate set has moved.

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
from dataclasses import dataclass
from datetime import date
from decimal import InvalidOperation
from pathlib import Path

from personal_agent_core.errors import AppError
from personal_agent_core.manifest import canonical_json
from personal_agent_core.tool_ir import ENTRY_KINDS
from personal_agent_core.timeutil import ledger_date, parse_ledger_date, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    PRODUCTION_KIND,
    SYNTHETIC_TEST_KIND,
    LedgerSourceError,
    load_base_source,
    production_write_allowed,
    require_synthetic_test_base,
    require_write_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.finance.expense_record import ExpenseEntry
from personal_data_mcp.finance.expense_policy import (
    Clarification,
    resolve_expense,
)
from personal_data_mcp.finance.ledger_reader import read_year_expenses
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.schema_validator import (
    observed_field_from_feishu,
    validate_schema,
)
from personal_data_mcp.crypto.keys import load_data_keyring
from personal_data_mcp.finance.duplicate_check import DuplicateFinding, OverrideRefused
from personal_data_mcp.finance.write_path import submit_expense
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


@dataclass(frozen=True)
class RawEntry:
    """What a human states on the command line, before the ledger is consulted.

    This is the same shape the model is contracted to produce: item text,
    amount, date, scope, entry kind, and at most a bare destination. Which trip
    that destination means, and what category a refund inherits, are decided
    against the ledger by `resolve_expense`, never here.
    """

    name: str
    input_amount: str
    occurred_on: date
    is_family_expense: bool
    entry_kind: str
    category: str | None
    trip_tag: str | None
    destination: str | None


async def run(
    raw: RawEntry,
    *,
    config_path: Path,
    db_path: Path,
    idempotency_key: str,
    duplicate_override: str | None = None,
) -> dict:
    config = load_ledger_config(
        json.loads(config_path.read_text(encoding="utf-8"))
    )
    if config.ledger_kind == SYNTHETIC_TEST_KIND:
        source_loader = require_synthetic_test_base
    elif config.ledger_kind == PRODUCTION_KIND and production_write_allowed():
        source_loader = require_write_base
    else:
        raise LedgerSourceError(
            f"refusing to write: this command accepts a {SYNTHETIC_TEST_KIND!r} "
            "config, or a production config only with "
            "PERSONAL_AGENT_ALLOW_PRODUCTION_WRITE=1 (the G5 switch)"
        )
    credentials = load_credentials()
    source = source_loader(
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

            # The duplicate check compares against the whole year including
            # rows Henson typed into Feishu himself, so the scan is no longer
            # conditional on what resolution needs.
            rows = await read_year_expenses(
                adapter, source=source, config=config
            )
            resolved = resolve_expense(
                name=raw.name,
                input_amount=raw.input_amount,
                occurred_on=raw.occurred_on,
                is_family_expense=raw.is_family_expense,
                entry_kind=raw.entry_kind,
                category=raw.category,
                trip_tag=raw.trip_tag,
                destination=raw.destination,
                ledger_rows=rows,
            )
            if isinstance(resolved, Clarification):
                # A question is a complete outcome: nothing was written, and no
                # execution row exists to reconcile.
                return {
                    "status": "clarification_required",
                    "reason": resolved.reason.value,
                    "options": list(resolved.options),
                    "scanned_rows": len(rows),
                }
            entry = resolved.entry

            outcome = await submit_expense(
                entry,
                ledger_rows=rows,
                keyring=load_data_keyring(),
                duplicate_override=duplicate_override,
                sessions=sessions,
                adapter=adapter,
                config=config,
                validation=validation,
                source=source,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint(entry),
                trace_id=f"g3-{idempotency_key}",
            )
            if isinstance(outcome, DuplicateFinding):
                return {
                    "status": "possible_duplicate",
                    "duplicate_check_id": outcome.check_id,
                    "candidates": [c.card() for c in outcome.candidates],
                    "scanned_rows": len(rows),
                }
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
        "stored_name": entry.name,
        "trip_resolution": (
            resolved.trip_resolution.value if resolved.trip_resolution else None
        ),
        "inherited_category_from": resolved.inherited_from_record_id,
        "scanned_rows": len(rows),
        "stored_fields": outcome.stored_fields,
    }


def build_entry(args: argparse.Namespace) -> RawEntry:
    return RawEntry(
        name=args.name,
        input_amount=args.amount,
        # The default is *the ledger's* today, not the host's: `date.today()`
        # would silently use the machine's timezone and could book an entry on
        # the wrong ledger day.
        occurred_on=(
            parse_ledger_date(args.date) if args.date else ledger_date(utc_now())
        ),
        is_family_expense=args.scope == "family",
        entry_kind=args.entry_kind,
        category=args.category,
        trip_tag=args.trip_tag,
        destination=args.destination,
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
    parser.add_argument(
        "--category",
        help=(
            "required for a plain expense; omit on a refund/AA receipt to "
            "inherit from a unique matching original"
        ),
    )
    parser.add_argument(
        "--trip-tag", help="a trip Henson wrote explicitly; used as written"
    )
    parser.add_argument(
        "--destination",
        help=(
            "a bare place name; the ledger decides which trip it means, and "
            "asks when several same-destination trips exist"
        ),
    )
    parser.add_argument(
        "--entry-kind", default="expense", choices=list(ENTRY_KINDS)
    )
    parser.add_argument(
        "--duplicate-override",
        help=(
            "a duplicate_check_id this server issued; releasing re-runs the "
            "check and refuses if the candidate set moved"
        ),
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
                duplicate_override=args.duplicate_override,
            )
        )
    except (AppError, LedgerSourceError, OverrideRefused) as exc:
        code = (
            exc.code.value
            if isinstance(exc, AppError)
            else "DUPLICATE_OVERRIDE_REFUSED"
            if isinstance(exc, OverrideRefused)
            else "LEDGER_SOURCE"
        )
        print(
            redact_for_log(f"write refused or unresolved: {code} ({exc})"),
            file=sys.stderr,
        )
        print(f"idempotency_key={key}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"idempotency_key={key}")
    # A clarification is a legitimate outcome, but it is not a write. Exiting 0
    # would let a script treat "I asked a question" as "it is recorded".
    sys.exit(
        3
        if evidence["status"] in ("clarification_required", "possible_duplicate")
        else 0
    )
