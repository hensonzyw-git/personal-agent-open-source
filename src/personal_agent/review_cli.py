"""Console entrypoint for the daily review job (`personal-agent-review`).

`DEV-028`. A systemd timer runs this at 00:05 `Asia/Shanghai` (the timer itself
is `DEV-036`); it may also be run by hand. Both are safe: the job is idempotent
on `review_date`, so a second run the same night adds nothing, and a run after
an outage compensates the last seven days.

It composes only what it needs -- the Agent database and the Finance control
channel. No model, no MCP session, no data key. The summary it prints is
deliberately counts and dates: record ids are resource identifiers, and this
output goes to the journal.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from personal_agent.api.composition import build_review_control
from personal_agent.api.daily_review import MAX_CATCH_UP_DAYS
from personal_agent.api.review_job import run_daily_review
from personal_agent.storage.engine import (
    check_integrity,
    create_database_engine,
    session_factory,
)
from personal_agent_core.timeutil import parse_ledger_date


DEFAULT_FINANCE_CONTROL_URL = "http://127.0.0.1:8811"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the daily Finance review cards and queue their pushes."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--finance-control-url",
        default=DEFAULT_FINANCE_CONTROL_URL,
        help="Loopback base URL of the Finance MCP internal control API.",
    )
    parser.add_argument(
        "--today",
        default=None,
        help=(
            "Override the Asia/Shanghai date the run treats as today, for "
            "compensating a specific window. Cards are built for the days "
            "*before* it."
        ),
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=MAX_CATCH_UP_DAYS,
        help="How many completed days to compensate (design 7.7 caps this at 7).",
    )
    args = parser.parse_args()

    if args.max_days < 1 or args.max_days > MAX_CATCH_UP_DAYS:
        raise SystemExit(
            f"--max-days must be between 1 and {MAX_CATCH_UP_DAYS}: design 7.7 "
            "compensates a week, not an unbounded history"
        )

    today: date | None = None
    if args.today is not None:
        try:
            today = parse_ledger_date(args.today)
        except ValueError as exc:
            raise SystemExit("--today must be YYYY-MM-DD") from exc

    if not args.database.exists():
        # Creating one here would silently produce an empty review history.
        raise SystemExit(f"no Agent database at {args.database}")

    engine = create_database_engine(args.database)
    check_integrity(engine)
    sessions = session_factory(engine)
    control = build_review_control(finance_control_url=args.finance_control_url)

    try:
        report = run_daily_review(
            sessions, control, today=today, max_days=args.max_days
        )
    finally:
        engine.dispose()

    for outcome in report.outcomes:
        print(f"{outcome.review_date}: {outcome.result} ({outcome.item_count} items)")
    print(
        f"cards created: {len(report.created)}; "
        f"notification rows queued: {report.queued_notifications}; "
        f"delivery attempts: {report.attempted_notifications}"
    )
    if report.created and report.queued_notifications == 0:
        # Not an error, but the operator should not read silence as delivery.
        print("no enrolled device could receive a push; the cards are in the app")
    if report.stopped_early_on is not None:
        raise SystemExit(
            f"stopped at {report.stopped_early_on}: the Finance control plane "
            "could not be read; the remaining days are left for the next run"
        )


if __name__ == "__main__":
    main()
