"""Console entrypoint for the daily risk run + push (`risk-monitor-daily`).

Run the monitor (ingest -> score -> persist), build the report, and push it to
every enrolled device by reusing the generalised APNs sender — the same
composition as ``review_cli.py``. When no APNs variable is set the job still
computes and persists the report but keeps the honest no-push behaviour; a
*partial* APNs configuration refuses to start, because that is a typo rather
than a decision to go without push.

Two databases are involved and they are deliberately distinct (ADR-0001): the
risk monitor's own SQLite holds scores and observations; the Personal Agent
database holds the enrolled devices whose push tokens the sender must open. The
sender is composed against the *latter*, never against the risk database.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from personal_agent.api.apns import ApnsConfigError, build_push_sender
from personal_agent.keys import load_agent_data_keyring
from personal_agent.storage.engine import (
    check_integrity,
    create_database_engine,
    session_factory,
)

from risk_monitor import daily
from risk_monitor.push import push_risk_report
from risk_monitor.report import build_report


def enrolled_device_ids(sessions) -> list[str]:
    """Enrolled devices that can receive a push, mirroring the Finance review
    query: a device with no push token is skipped rather than attempted."""
    from personal_agent.storage.models import Device

    with sessions() as session:
        return list(
            session.scalars(
                select(Device.device_id).where(
                    Device.status == "active",
                    Device.encrypted_push_token.is_not(None),
                )
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the US/AI systemic risk monitor and push the daily card."
    )
    parser.add_argument(
        "--agent-database",
        type=Path,
        required=True,
        help="Personal Agent database (source of enrolled device push tokens).",
    )
    parser.add_argument(
        "--risk-database",
        type=Path,
        default=None,
        help="Risk monitor SQLite database (defaults to data/risk_monitor.db).",
    )
    args = parser.parse_args()

    if not args.agent_database.exists():
        raise SystemExit(f"no Agent database at {args.agent_database}")

    engine = create_database_engine(args.agent_database)
    check_integrity(engine)
    sessions = session_factory(engine)
    keyring = load_agent_data_keyring()

    try:
        sender = build_push_sender(session_factory=sessions, keyring=keyring)
    except ApnsConfigError as exc:
        engine.dispose()
        raise SystemExit(str(exc)) from exc

    try:
        result = daily.run(
            db_path=str(args.risk_database) if args.risk_database else None
        )
        report = build_report(result)
        devices = enrolled_device_ids(sessions)
        if sender is not None:
            outcome = push_risk_report(sender, report, devices)
        else:
            outcome = {"accepted": [], "failed": {}}
    finally:
        if sender is not None:
            sender.close()
        engine.dispose()

    scores = report["scores"]
    print(
        f"as_of={report['as_of']} state={report['state']} "
        f"MBS={scores['mbs']} CSS={scores['css']} AFRS={scores['afrs']}"
    )
    print(
        f"devices={len(devices)} accepted={len(outcome['accepted'])} "
        f"failed={len(outcome['failed'])}"
    )
    if sender is None:
        print("no APNs configuration; the report was computed but not pushed")


if __name__ == "__main__":
    main()
