"""Report assembly tests (pure)."""

from __future__ import annotations

from risk_monitor.report import build_report, push_summary, render_markdown


def _run(**overrides):
    base = {
        "as_of": "2026-08-20",
        "mbs": 0.0,
        "css": 0.0,
        "afrs": None,
        "state": "NORMAL",
        "indication": "NORMAL",
        "reasons": ["no upgrade condition met"],
        "mbs_unavailable": ["market.fwd_eps_revisions"],
        "css_unavailable": ["credit.ai_basket", "credit.term_financing"],
        "breadth": {"coverage": 0.996, "tickers_ok": 502, "tickers_requested": 503},
    }
    base.update(overrides)
    return base


def test_build_report_severity_mapping():
    assert build_report(_run())["severity"] == "INFO"
    assert build_report(_run(state="DELEVERAGING"))["severity"] == "CRITICAL"
    assert build_report(_run(state="CREDIT_CONFIRMATION"))["severity"] == "ACTION_REVIEW"


def test_push_summary_afrs_none_is_dash():
    r = build_report(_run())
    assert push_summary(r) == "系统性风险 正常 — MBS 0.0 / CSS 0.0 / AFRS -"


def test_push_summary_with_afrs():
    r = build_report(_run(afrs=42.0, state="RISK_ACCUMULATION"))
    assert "风险累积" in push_summary(r)
    assert "AFRS 42.0" in push_summary(r)


def test_render_markdown_includes_unavailable():
    md = render_markdown(build_report(_run()))
    assert "# 系统性风险周报" in md
    assert "market.fwd_eps_revisions" in md
    assert "credit.ai_basket" in md
