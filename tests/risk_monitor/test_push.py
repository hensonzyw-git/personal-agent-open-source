"""Risk push: severity -> alert fields, and the per-device delivery loop."""

from __future__ import annotations

from risk_monitor.push import ALERT_TITLE, push_risk_report, risk_alert


def _report(state="NORMAL", mbs=0.0, css=0.0, afrs=19.5, as_of="2026-08-21", action=None):
    from risk_monitor.report import SEVERITY_BY_STATE

    return {
        "as_of": as_of,
        "scores": {"mbs": mbs, "css": css, "afrs": afrs},
        "state": state,
        "action": action,
        "severity": SEVERITY_BY_STATE.get(state, "INFO"),
    }


def test_normal_is_background_priority():
    alert = risk_alert(_report("NORMAL"))
    assert alert["title"] == ALERT_TITLE
    assert alert["priority"] == "5"
    assert alert["collapse_id"] == "risk:2026-08-21"
    assert "MBS 0.0" in alert["body"] and "AFRS 19.5" in alert["body"]


def test_deleveraging_is_immediate_priority():
    alert = risk_alert(_report("DELEVERAGING", mbs=70.0, css=80.0, afrs=60.0))
    assert alert["priority"] == "10"
    assert "去杠杆" in alert["body"]


def test_push_body_carries_the_action_conclusion():
    alert = risk_alert(_report("CREDIT_CONFIRMATION", action="减仓（降低敞口）"))
    assert alert["body"].startswith("减仓（降低敞口）")


def test_credit_confirmation_is_immediate_priority():
    assert risk_alert(_report("CREDIT_CONFIRMATION"))["priority"] == "10"


def test_risk_accumulation_is_background_priority():
    assert risk_alert(_report("RISK_ACCUMULATION"))["priority"] == "5"


def test_missing_afrs_shows_dash_not_zero():
    alert = risk_alert(_report("NORMAL", afrs=None))
    assert "AFRS -" in alert["body"]
    assert "AFRS 0.0" not in alert["body"]


class _RecordingSender:
    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[dict] = []
        self._fail_on = fail_on or set()

    def send_alert(self, device_id, *, title, body, collapse_id, priority):
        self.calls.append(
            {"device_id": device_id, "title": title, "body": body,
             "collapse_id": collapse_id, "priority": priority}
        )
        if device_id in self._fail_on:
            raise RuntimeError("boom")


def test_push_risk_report_partitions_outcomes():
    sender = _RecordingSender(fail_on={"dev-2"})
    result = push_risk_report(
        sender, _report("NORMAL"), ["dev-1", "dev-2", "dev-3"]
    )
    assert result["accepted"] == ["dev-1", "dev-3"]
    assert result["failed"] == {"dev-2": "RuntimeError"}
    assert len(sender.calls) == 3
    # every device got the same alert body, keyed collapse id on the day
    assert all(c["collapse_id"] == "risk:2026-08-21" for c in sender.calls)


def test_the_daily_cli_wires_the_sender_rather_than_leaving_a_seam():
    """§7: a seam wired only in tests is not wiring.

    The push path must compose the real sender and hand it to
    ``push_risk_report``; otherwise the ``send_alert`` generalisation would be a
    seam that only the tests above exercise, and the daily job would silently
    stop pushing while every unit test kept passing.
    """
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "src/risk_monitor/daily_cli.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    nodes = list(ast.walk(tree))
    calls = [n for n in nodes if isinstance(n, ast.Call)]
    funcs = {
        n.func.id
        for n in calls
        if isinstance(n.func, ast.Name)
    }
    assert "build_push_sender" in funcs, "the CLI must compose the APNs sender"

    push_calls = [
        n
        for n in calls
        if isinstance(n.func, ast.Name) and n.func.id == "push_risk_report"
    ]
    assert push_calls, "the CLI must call push_risk_report"
    first_arg = push_calls[0].args[0]
    assert isinstance(first_arg, ast.Name) and first_arg.id == "sender", (
        "push_risk_report must receive the composed sender, not None"
    )
