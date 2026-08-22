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


def test_push_summary_leads_with_action_conclusion():
    # The push must carry the deterministic conclusion, not raw scores to read.
    r = build_report(_run(state="CREDIT_CONFIRMATION", action="减仓（降低敞口）"))
    summary = push_summary(r)
    assert summary.startswith("减仓（降低敞口）")
    assert "信用确认" in summary


def test_push_summary_without_action_still_names_state():
    # A replay built before the action field existed must not break.
    assert push_summary(build_report(_run())).startswith("系统性风险 正常")


def test_render_markdown_includes_action():
    md = render_markdown(build_report(_run(state="DELEVERAGING", action="卖出 / 大幅减仓（强制人工确认）")))
    assert "- 操作：卖出 / 大幅减仓（强制人工确认）" in md


def test_render_markdown_includes_unavailable():
    md = render_markdown(build_report(_run()))
    assert "# 系统性风险周报" in md
    assert "market.fwd_eps_revisions" in md
    assert "credit.ai_basket" in md


def _comp(mid, band, available=True, value=None):
    return {"metric_id": mid, "available": available, "band": band, "value": value}


def test_build_report_components_format_indicators():
    """The per-indicator breakdown is formatted into label/value/band rows —
    numeric values with sign+unit, qualitative bands as a Chinese word — and the
    permanently-unavailable ``fwd_eps_revisions`` is dropped."""
    run = _run(
        mbs_components=[
            _comp("market.spx_vs_200dma_pct", "green", value=8.1549),
            _comp("market.spx_pct_above_200dma", "green", value=68.4),
            _comp("market.vix", "green", value=16.01),
            _comp("market.breadth_20d_change", "green", value=4.2717),
            _comp("market.fwd_eps_revisions", None, available=False),
        ],
        css_components=[
            _comp("credit.hy_oas_pct", "green", value=2.75),
            _comp("credit.hy_oas_20d_change", "green", value=-2.0),
            _comp("credit.bbb_oas_pct", "green", value=1.0),
            _comp("credit.ai_basket", "orange"),  # qualitative: value=None
            _comp("credit.term_financing", "green"),  # qualitative
        ],
    )
    components = build_report(run)["components"]
    assert components["mbs"] == [
        {"label": "SPX vs 200日均线", "value": "+8.2%", "band": "green"},
        {"label": "广度（>200日均线）", "value": "68.4%", "band": "green"},
        {"label": "VIX", "value": "16.0", "band": "green"},
        {"label": "广度 20 日变化", "value": "+4.3pp", "band": "green"},
    ]
    assert components["css"] == [
        {"label": "HY OAS", "value": "2.75%", "band": "green"},
        {"label": "HY OAS 20 日变化", "value": "-2.0bp", "band": "green"},
        {"label": "BBB OAS", "value": "1.00%", "band": "green"},
        {"label": "AI 篮子", "value": "警戒", "band": "orange"},
        {"label": "期限融资", "value": "正常", "band": "green"},
    ]


def test_build_report_components_skip_unavailable():
    """An indicator that failed closed (available=False) is dropped, not shown
    as a fabricated number — the score already renormalised over it."""
    run = _run(
        mbs_components=[
            _comp("market.spx_pct_above_200dma", None, available=False),
            _comp("market.vix", "green", value=16.0),
        ],
    )
    components = build_report(run)["components"]
    assert [r["label"] for r in components["mbs"]] == ["VIX"]
    assert components["css"] == []


def test_build_report_carries_quality_status():
    assert build_report(_run())["quality_status"] == "ok"
    assert (
        build_report(_run(quality_status="data_quality_warning"))["quality_status"]
        == "data_quality_warning"
    )


def test_push_summary_flags_a_degraded_day():
    degraded = build_report(_run(quality_status="data_quality_warning"))
    assert push_summary(degraded).startswith("⚠ 数据不完整 ")
    assert push_summary(build_report(_run())).startswith("系统性风险 正常")
