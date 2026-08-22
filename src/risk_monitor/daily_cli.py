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
sender is composed against the *latter*, never against the risk database — and
this job also *writes* to that database, sealing the day's risk card as a
`risk_report` Timeline event (a trust-boundary widening beyond the original
read-only token query; see `seal_risk_event`).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sqlalchemy import select

from personal_agent.api import events
from personal_agent.api.apns import ApnsConfigError, build_push_sender
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import SessionManager
from personal_agent.keys import load_agent_data_keyring
from personal_agent.storage.engine import (
    check_integrity,
    create_database_engine,
    session_factory,
)
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from risk_monitor import daily
from risk_monitor.push import push_risk_report
from risk_monitor.report import build_report

logger = logging.getLogger(__name__)


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


def _card_content(report: dict) -> dict:
    """The ``risk_report`` Timeline event content (frozen contract with the iOS
    decoder). ``as_of``/``state`` are mandatory — a missing value makes the
    client treat the card as unrecognised rather than render a half card.
    ``components`` carries the per-indicator breakdown behind MBS/CSS (each row
    ``label``/``value``/``band``); it is optional so an older card still decodes."""
    scores = report["scores"]
    return {
        "as_of": report["as_of"],
        "state": report["state"],
        "mbs": scores.get("mbs"),
        "css": scores.get("css"),
        "afrs": scores.get("afrs"),
        "action": report.get("action"),
        "quality_status": report.get("quality_status", "ok"),
        "components": report.get("components"),
    }


def seal_risk_event(sessions, keyring, session_manager, report) -> str | None:
    """Seal today's risk card as a frozen ``risk_report`` Timeline event.

    Idempotent on ``as_of``: one card per trading day. A repeat fire (a weekend,
    a holiday, a manual rerun) finds the existing card and returns ``None``
    without appending, so the Timeline never stacks identical cards. The content
    is read from ``build_report`` output — no Feishu round-trip — and the
    existence check plus the append stay inside one ``run_write_transaction``
    (§5.2), so check-then-append is a single unit.
    """
    now = utc_now()
    content = _card_content(report)
    as_of = content["as_of"]

    with sessions() as session:

        def append_if_absent() -> str | None:
            if events.event_exists_with(
                session,
                keyring,
                event_type=events.RISK_REPORT,
                content_key="as_of",
                content_value=as_of,
            ):
                return None
            timeline_id = events.canonical_timeline_id(session, now=now)
            return events.append_event(
                session,
                keyring,
                conversation_id=timeline_id,
                session_id=session_manager.system_event_session(
                    session, conversation_id=timeline_id, now=now
                ),
                turn_id=events.new_turn_id(),
                event_type=events.RISK_REPORT,
                content=content,
                operation_id=None,
                now=now,
            )

        return run_write_transaction(session, append_if_absent)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
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
    session_manager = SessionManager(default_context_config())

    try:
        sender = build_push_sender(session_factory=sessions, keyring=keyring)
    except ApnsConfigError as exc:
        engine.dispose()
        raise SystemExit(str(exc)) from exc

    seal_failed = False
    try:
        result = daily.run(
            db_path=str(args.risk_database) if args.risk_database else None
        )
        report = build_report(result)
        try:
            event_id = seal_risk_event(sessions, keyring, session_manager, report)
            if event_id is None:
                logger.info("risk card already sealed for %s; skipped", report["as_of"])
            else:
                logger.info("risk card sealed: %s", event_id)
        except Exception as exc:  # noqa: BLE001 - a failed seal must not drop the push
            seal_failed = True
            logger.error(
                "risk card seal failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
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
    if seal_failed:
        # The push went out but the card did not land. Exit non-zero so systemd
        # surfaces a failed unit instead of silently "succeeding" forever while
        # the app shows no card (a systematic seal failure would otherwise be
        # invisible in a oneshot job nobody watches).
        raise SystemExit(1)


if __name__ == "__main__":
    main()
