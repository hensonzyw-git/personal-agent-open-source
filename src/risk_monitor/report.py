"""Report assembly: daily JSON, weekly Markdown, and a one-line push summary.

Pure and deterministic given a snapshot; the only input is the structured
result of a daily run (or a replay of stored snapshots). No network, no
database. The push summary is deliberately *concise* — it names the state and
the three scores, and never carries a raw metric value that would belong in the
full report rather than a lock screen.
"""

from __future__ import annotations

from typing import Optional

SEVERITY_BY_STATE = {
    "NORMAL": "INFO",
    "RISK_ACCUMULATION": "WARNING",
    "CREDIT_CONFIRMATION": "ACTION_REVIEW",
    "DELEVERAGING": "CRITICAL",
}

_STATE_LABEL = {
    "NORMAL": "正常",
    "RISK_ACCUMULATION": "风险累积",
    "CREDIT_CONFIRMATION": "信用确认",
    "DELEVERAGING": "去杠杆",
}

# metric_id -> (Chinese card label, unit suffix, show sign, decimals). A ``None``
# suffix marks a qualitative indicator: its value is the band's Chinese word, not
# a number. This is the frozen human-readable vocabulary for the in-app card.
_METRIC_LABELS = {
    "market.spx_vs_200dma_pct": ("SPX vs 200日均线", "%", True, 1),
    "market.spx_pct_above_200dma": ("广度（>200日均线）", "%", False, 1),
    "market.vix": ("VIX", "", False, 1),
    "market.breadth_20d_change": ("广度 20 日变化", "pp", True, 1),
    "credit.hy_oas_pct": ("HY OAS", "%", False, 2),
    "credit.hy_oas_20d_change": ("HY OAS 20 日变化", "bp", True, 1),
    "credit.bbb_oas_pct": ("BBB OAS", "%", False, 2),
    "credit.ai_basket": ("AI 篮子", None, False, 0),
    "credit.term_financing": ("期限融资", None, False, 0),
}

_BAND_LABEL = {
    "green": "正常",
    "yellow": "关注",
    "orange": "警戒",
    "red": "高危",
}


def _component_rows(raw: list[dict]) -> list[dict]:
    """Format one score's serialised indicator results into card rows.

    Unavailable indicators are skipped — they never contribute to the score
    (their weight is renormalised away), so they are not part of "why the score
    is what it is". This also drops ``market.fwd_eps_revisions``, which is
    permanently unavailable in MVP."""
    rows: list[dict] = []
    for item in raw:
        if not item.get("available"):
            continue
        mid = item["metric_id"]
        label, suffix, signed, decimals = _METRIC_LABELS.get(mid, (mid, "", False, 1))
        band = item.get("band")
        if suffix is None:  # qualitative: the band is the value
            value = _BAND_LABEL.get(band or "", band or "未知")
        else:
            v = item.get("value")
            if v is None:
                value = "-"
            else:
                fmt = f"{{:+.{decimals}f}}" if signed else f"{{:.{decimals}f}}"
                value = fmt.format(v) + suffix
        rows.append({"label": label, "value": value, "band": band})
    return rows


def build_report(run: dict) -> dict:
    """The daily JSON report. ``run`` is the ``daily.run()`` result (or the
    equivalent dict replayed from stored snapshots)."""
    return {
        "as_of": run["as_of"],
        "scores": {
            "mbs": run["mbs"],
            "css": run["css"],
            "afrs": run["afrs"],
        },
        "state": run["state"],
        "action": run.get("action"),
        "indication": run.get("indication"),
        "severity": SEVERITY_BY_STATE.get(run.get("state", "NORMAL"), "INFO"),
        "reasons": run.get("reasons") or [],
        "mbs_unavailable": run.get("mbs_unavailable") or [],
        "css_unavailable": run.get("css_unavailable") or [],
        "breadth": run.get("breadth") or {},
        "components": {
            "mbs": _component_rows(run.get("mbs_components") or []),
            "css": _component_rows(run.get("css_components") or []),
        },
    }


def push_summary(report: dict) -> str:
    """The one line that belongs on a lock screen: state + the three scores.

    ``afrs`` may be ``None`` before the fundamental layer is wired; it is shown
    as ``-``, never as a fabricated zero."""
    scores = report["scores"]
    mbs = _fmt(scores.get("mbs"))
    css = _fmt(scores.get("css"))
    afrs = _fmt(scores.get("afrs"))
    state = report.get("state", "NORMAL")
    label = _STATE_LABEL.get(state, state)
    action = report.get("action")
    head = f"{action}；系统性风险 {label}" if action else f"系统性风险 {label}"
    return f"{head} — MBS {mbs} / CSS {css} / AFRS {afrs}"


def render_markdown(report: dict) -> str:
    """The weekly (or on-demand) human-readable summary."""
    scores = report["scores"]
    lines = [
        "# 系统性风险周报",
        "",
        f"- 截至：{report['as_of']}",
        f"- 状态：{report['state']}（{_STATE_LABEL.get(report['state'], report['state'])}）",
        f"- 操作：{report.get('action', '-')}",
        f"- 严重度：{report['severity']}",
        f"- MBS：{_fmt(scores.get('mbs'))}  / CSS：{_fmt(scores.get('css'))}  / AFRS：{_fmt(scores.get('afrs'))}",
    ]
    reasons = report.get("reasons") or []
    if reasons:
        lines.append("- 触发原因：" + "；".join(reasons))
    for label, key in (("MBS", "mbs_unavailable"), ("CSS", "css_unavailable")):
        unavail = report.get(key) or []
        if unavail:
            lines.append(f"- {label} 不可用指标：" + "、".join(unavail))
    breadth = report.get("breadth") or {}
    if breadth:
        lines.append(
            f"- 宽度覆盖：{breadth.get('coverage', '-')} "
            f"（{breadth.get('tickers_ok', '-')}/{breadth.get('tickers_requested', '-')} 成分）"
        )
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"
