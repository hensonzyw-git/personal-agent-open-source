"""`personal-data-mcp-verify-ledger`: read the real annual ledger, never write it.

Two §13.2 gates can only be closed against the production annual ledger:

- *annual schema validation touches only approved Base/table/fields, and a drift
  fails writes closed*, which the design itself says "must be re-proven against
  the real annual ledger -- that is what G5 means, not something another test
  can supply";
- *query full pagination and the personal-expense formula agree with a manual
  Feishu reconciliation*, which is the one item on that list no code can close:
  Henson has to compare the numbers himself.

That produces an ordering problem. G5 authorises production access, and two of
G5's own gates need production access to close. Henson's decision on 2026-08-03
was to split it: **read-only production first**, writes still gated. This tool is
that read-only half.

**Why this is a separate entrypoint rather than a flag on the server.** A flag
would mean the same process that can write is asked, politely, not to. Here the
inability is structural and can be checked by reading the imports: this module
never constructs `FinanceWriteDependencies`, never opens the execution store,
never builds a key ring or an FX connector, and never starts the recovery worker
-- and recovery is the sharp one, because `reconcile_write` calls
`adapter.create_record` and would happily complete a stranded write against the
real ledger. Nothing here can reach that code path, because nothing here has the
`sessions` it would need.

The Feishu adapter is shared with the write path, so it does have `create_record`
on it. That is the one residual: the guarantee is "no caller here reaches it",
which a test pins by AST rather than by assertion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from personal_agent_core.timeutil import utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    BaseSource,
    LedgerSourceError,
    load_base_source,
    require_configured_base,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.finance.ledger_config import LedgerConfig, load_ledger_config
from personal_data_mcp.finance.query_expenses import query_expenses
from personal_data_mcp.finance.query_cursor_secret import (
    CURSOR_SECRET_ENV,
    QueryCursorSecretError,
    load_query_cursor_secret,
)
from personal_data_mcp.finance.schema_validator import (
    SchemaValidation,
    observed_field_from_feishu,
    validate_schema,
)


#: A full pagination has to stop somewhere. A ledger year of personal expenses is
#: thousands of rows, not millions; this bound exists so a cursor bug becomes a
#: refusal instead of an unbounded read against the real ledger.
MAX_PAGES = 500

@dataclass(frozen=True)
class ReadOnlyLedger:
    """Everything a *read* needs, and deliberately nothing a write needs.

    Not `FinanceWriteDependencies` with fields left empty -- a different type.
    There is no `sessions`, no `keyring` and no `fx` here, so `build_expense_handler`
    and friends cannot be constructed from it at all.
    """

    adapter: FeishuAdapter
    source: BaseSource
    config: LedgerConfig
    now: Callable[[], datetime] = utc_now


def load_config_for_read_only(path: Path) -> LedgerConfig:
    """Load a protected ledger config for reading, whatever kind it declares.

    The synthetic-only refusal lives in `server/composition.load_protected_config`,
    which is the *write* door. This is the read door, and it is the only place a
    production config may enter the process.
    """
    return load_ledger_config(json.loads(path.read_text(encoding="utf-8")))


async def open_ledger(config_path: Path) -> ReadOnlyLedger:
    """Bind the environment's Base to the protected config, or refuse."""
    config = load_config_for_read_only(config_path)
    source = require_configured_base(
        load_base_source(),
        approved_base_token=config.base_token,
        approved_tables={
            kind: table.table_id for kind, table in config.tables.items()
        },
        approved_ledger_kind=config.ledger_kind,
    )
    return ReadOnlyLedger(
        # `now` is the monotonic clock the adapter uses for its token cache
        # and rate limiter -- keyword-only and required, exactly as the write
        # composition passes it.
        adapter=FeishuAdapter(load_credentials(), now=time.monotonic),
        source=source,
        config=config,
    )


async def validate_live_schema(ledger: ReadOnlyLedger) -> SchemaValidation:
    """List fields for every configured table and validate them.

    `list_fields` is the only Feishu call made. It reads field *definitions*, not
    rows, so this subcommand never retrieves a single ledger record.
    """
    observed: dict[str, list] = {}
    for kind, table_id in ledger.source.tables.items():
        fields = await ledger.adapter.list_fields(ledger.source.base_token, table_id)
        observed[kind] = [observed_field_from_feishu(field) for field in fields]
    return validate_schema(ledger.config, observed)


async def run_query(
    ledger: ReadOnlyLedger,
    arguments: dict[str, Any],
    *,
    cursor_secret: bytes,
    page_all: bool,
) -> list[dict[str, Any]]:
    """Run one query, following its cursor to the end when asked.

    Full pagination is the point of the gate: a first page called "all the data"
    is exactly the failure §5 warns about, so `--page-all` keeps following the
    cursor and refuses rather than stopping quietly if the ledger produces more
    pages than `MAX_PAGES`.
    """
    validation = await validate_live_schema(ledger)
    pages: list[dict[str, Any]] = []
    page_arguments = dict(arguments)
    for _ in range(MAX_PAGES):
        result = await query_expenses(
            page_arguments,
            adapter=ledger.adapter,
            source=ledger.source,
            config=ledger.config,
            validation=validation,
            cursor_secret=cursor_secret,
            now=ledger.now,
        )
        pages.append(result)
        cursor = result.get("next_cursor")
        if not page_all or not cursor:
            return pages
        # A continuation must not restate its filters, per the query contract.
        page_arguments = {"view": arguments["view"], "cursor": cursor}
    raise LedgerSourceError(
        f"query did not finish within {MAX_PAGES} pages; refusing to keep reading"
    )


def _cursor_secret(env: dict[str, str] | None = None) -> bytes:
    try:
        secret = load_query_cursor_secret(env)
    except QueryCursorSecretError as error:
        raise SystemExit(str(error)) from error
    assert secret is not None  # required=True above
    return secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only verification against a ledger, including the real annual "
            "one. Validates the live schema and runs the frozen query contract. "
            "It cannot write: no execution store, no key ring, no recovery."
        )
    )
    parser.add_argument("--ledger-config", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "schema",
        help="validate the live schema against the frozen config; reads no rows",
    )
    query = sub.add_parser("query", help="run the frozen query contract")
    query.add_argument(
        "--view", choices=["total", "by_category", "records"], required=True
    )
    query.add_argument("--start", help="inclusive start date, YYYY-MM-DD")
    query.add_argument("--end", help="inclusive end date, YYYY-MM-DD")
    query.add_argument(
        "--page-all",
        action="store_true",
        help="follow the cursor to the end; the gate asks for full pagination",
    )
    args = parser.parse_args(argv)

    async def scenario() -> int:
        try:
            ledger = await open_ledger(args.ledger_config)
        except LedgerSourceError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        try:
            print(
                f"ledger_kind={ledger.config.ledger_kind} "
                f"config_version={ledger.config.config_version}"
            )
            if args.command == "schema":
                validation = await validate_live_schema(ledger)
                print(f"status={validation.status}")
                print(f"config_checksum={validation.config_checksum}")
                print(f"snapshot_checksum={validation.snapshot_checksum}")
                for drift in validation.drifts:
                    print(f"  DRIFT {drift}")
                return 0 if validation.status == "valid" else 1

            arguments: dict[str, Any] = {"view": args.view}
            if args.start or args.end:
                if not (args.start and args.end):
                    print("--start and --end must be given together", file=sys.stderr)
                    return 2
                arguments["date_range"] = {"start": args.start, "end": args.end}
            pages = await run_query(
                ledger,
                arguments,
                cursor_secret=_cursor_secret(),
                page_all=args.page_all,
            )
            print(f"pages={len(pages)}")
            print(json.dumps(pages, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        finally:
            await ledger.adapter.aclose()

    return asyncio.run(scenario())


if __name__ == "__main__":  # pragma: no cover - console entrypoint
    raise SystemExit(main())
