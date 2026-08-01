"""`DEV-034`: the alert one-shot. Runs from a systemd timer, reports to journald.

Henson chose journald plus systemd unit failure as the alert channel on
2026-08-01, which fixes the shape of this program: a short-lived process that
prints a redacted report and exits non-zero when something is wrong. `systemctl
status personal-data-mcp-observe` then shows a failed unit, and the journal
carries the reason. Same contract as the DEV-033 certificate check, on purpose --
one way to look at whether this box is unhappy, not two.

Exit codes are the interface:

  0  nothing above `info`
  1  at least one `warning` or `critical` finding
  2  the report could not be produced at all

2 is separate from 1 deliberately. "I checked and found problems" and "I could
not check" are different states, and collapsing them means a broken monitor
looks exactly like a healthy one having a bad day -- or worse, that a monitor
which cannot open its database reports zero problems.

Nothing here prints a value that did not come from the metric catalog, the
finding vocabulary, or arithmetic. There is no code path that formats a ledger
field, a record id or an environment variable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from personal_agent_core.timeutil import utc_now
from personal_data_mcp.observability import (
    collect,
    evaluate,
    missing_capabilities,
    worst_severity,
)
from personal_data_mcp.storage.engine import create_database_engine, session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report Personal Agent health from durable facts and exit non-zero "
            "when something needs attention."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="the Finance MCP SQLite database to read (read-only)",
    )
    parser.add_argument(
        "--disk-path",
        type=Path,
        default=None,
        help="filesystem to check for free space; defaults to the database's",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object instead of human-readable lines",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.database.exists():
        # Exit 2, not 1: a missing database is "cannot check", and reporting it
        # as "no problems found" is the failure mode this whole file exists to
        # avoid.
        print(
            f"cannot check: no database at {args.database}",
            file=sys.stderr,
        )
        return 2

    disk_path = args.disk_path or args.database.parent
    engine = create_database_engine(args.database)
    try:
        with session_factory(engine)() as session:
            facts = collect(
                finance_session=session,
                agent_session=None,
                databases={"finance": args.database},
                disk_path=disk_path,
                now=utc_now(),
            )
    except Exception as exc:  # noqa: BLE001 - any failure here means "cannot check"
        print(f"cannot check: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    findings = evaluate(facts) + missing_capabilities()
    severity = worst_severity(findings)

    if args.json:
        print(
            json.dumps(
                {
                    "kind": "personal_agent_observe",
                    "facts": facts.as_dict(),
                    "findings": [f.as_dict() for f in findings],
                    "worst_severity": severity,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for name, value in facts.as_dict().items():
            print(f"{name}: {value}")
        print()
        for finding in findings:
            stream = sys.stderr if finding.severity != "info" else sys.stdout
            print(
                f"[{finding.severity.upper()}] {finding.code}: {finding.detail}",
                file=stream,
            )

    return 1 if severity in ("critical", "warning") else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
