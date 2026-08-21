"""Risk-monitor push: map a daily report to an APNs alert and deliver it.

This is the *reuse* half of the "复用并泛化现有发送器" decision. The *generalise*
half lives in ``personal_agent.api.apns``, where ``ApnsPushSender`` gained a
``send_alert`` method: the Finance review card and the risk card are now two
concrete alerts over the one sender, sharing token resolution, the provider JWT,
and permanent-failure handling. This module owns only what is risk-specific — the
title / body / collapse-id / priority derived from severity — and the per-device
loop, and it never talks to Apple itself.

The risk monitor has its own database (ADR-0001), so it cannot use the review
outbox; but the *sender* reads device tokens from the Personal Agent database and
opens them with the agent key ring, which the caller composes and passes in. This
module is deliberately pure and takes ``device_ids`` rather than a session, so
the whole severity/collapse/priority mapping is testable without a database.
"""

from __future__ import annotations

from typing import Literal, Protocol

from risk_monitor.report import SEVERITY_BY_STATE, push_summary

ALERT_TITLE = "系统性风险监控"

#: APNs priority "10" delivers immediately, "5" at a convenient time (battery).
#: Surfacing the two upper states is the point of the monitor, so only those take
#: the immediate path; a NORMAL / RISK_ACCUMULATION card can wait.
_IMMEDIATE_SEVERITIES = frozenset({"ACTION_REVIEW", "CRITICAL"})

ApnsPriority = Literal["5", "10"]


class AlertSender(Protocol):
    def send_alert(
        self,
        device_id: str,
        *,
        title: str,
        body: str,
        collapse_id: str,
        priority: ApnsPriority,
    ) -> None: ...


def risk_alert(report: dict) -> dict:
    """The APNs alert fields for a ``report.py.build_report`` output.

    ``collapse_id`` keys on the as-of date so a same-day re-run replaces a
    still-pending banner rather than stacking a second one.
    """
    severity = report.get("severity") or SEVERITY_BY_STATE.get(
        report.get("state", "NORMAL"), "INFO"
    )
    return {
        "title": ALERT_TITLE,
        "body": push_summary(report),
        "collapse_id": f"risk:{report.get('as_of')}",
        "priority": "10" if severity in _IMMEDIATE_SEVERITIES else "5",
    }


def push_risk_report(
    sender: AlertSender,
    report: dict,
    device_ids: list[str],
) -> dict:
    """Send the risk alert to each device. Returns counts, never a receipt:
    ``accepted`` means Apple took it, which is all a provider can ever say. A
    device that fails is recorded by error type and does not stop the others.
    """
    alert = risk_alert(report)
    accepted: list[str] = []
    failed: dict[str, str] = {}
    for device_id in device_ids:
        try:
            sender.send_alert(
                device_id,
                title=alert["title"],
                body=alert["body"],
                collapse_id=alert["collapse_id"],
                priority=alert["priority"],
            )
            accepted.append(device_id)
        except Exception as exc:  # noqa: BLE001 - one dead device must not drop the batch
            failed[device_id] = type(exc).__name__
    return {"accepted": accepted, "failed": failed}
