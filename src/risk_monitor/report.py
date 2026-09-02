"""Report assembly: daily JSON, weekly Markdown, and a one-line push summary.

Pure and deterministic given a snapshot; the only input is the structured
result of a daily run (or a replay of stored snapshots). No network, no
database. The push summary is deliberately *concise* — it names the state and
the four scores, and never carries a raw metric value that would belong in the
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
    "rates.10y_yield_pct": ("10Y 美债收益率", "%", False, 2),
    "rates.10y_real_yield_pct": ("10Y 实际利率", "%", False, 2),
    "rates.10y_20d_change_bp": ("10Y 20 日变化", "bp", True, 1),
    "rates.10y_real_20d_change_bp": ("10Y 实际利率 20 日变化", "bp", True, 1),
    "rates.10y_2y_spread_bp": ("10Y-2Y 曲线", "bp", True, 1),
    "rates.10y_3m_spread_bp": ("10Y-3M 曲线", "bp", True, 1),
    "rates.30y_20d_change_bp": ("30Y 20 日变化", "bp", True, 1),
    "rates.hy_oas_20d_change_bp": ("HY OAS 20 日变化", "bp", True, 1),
    "rates.move_index": ("MOVE", "", False, 1),
    "rates.treasury_liquidity": ("国债流动性", None, False, 0),
    "rates.fed_funds_futures": ("联邦基金期货", None, False, 0),
    "market.valuation": ("市场估值", None, False, 0),
}

_BAND_LABEL = {
    "green": "正常",
    "yellow": "关注",
    "orange": "警戒",
    "red": "高危",
}

_STALE_DAYS_THRESHOLD = 7  # as_of more than this many days behind today -> stale


def _component_rows(raw: list[dict]) -> list[dict]:
    """Format one score's serialised indicator results into card rows.

    Unavailable indicators are skipped — they never contribute to the score
    (their weight is renormalised away), so they are not part of "why the score
    is what it is". This also drops ``market.fwd_eps_revisions``, which is
    permanently unavailable in MVP. A metric absent from ``_METRIC_LABELS``
    (contract drift) still renders its band or raw value rather than a bare "-"."""
    rows: list[dict] = []
    for item in raw:
        if not item.get("available"):
            continue
        mid = item["metric_id"]
        band = item.get("band")
        spec = _METRIC_LABELS.get(mid)
        if spec is None:
            value = (
                _BAND_LABEL.get(band or "", band or "未知")
                if item.get("value") is None
                else str(item.get("value"))
            )
            rows.append({"label": mid, "value": value, "band": band})
            continue
        label, suffix, signed, decimals = spec
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


def _unavailable_component_rows(metric_ids: list[str]) -> list[dict]:
    """Render the RCS evidence deliberately absent from the free-data baseline.

    Unlike permanently unavailable MBS inputs, these rows are material to how a
    Treasury/credit reading should be interpreted.  Keep them in the sealed card
    instead of silently dropping them during presentation.
    """
    rows: list[dict] = []
    for metric_id in dict.fromkeys(metric_ids):
        spec = _METRIC_LABELS.get(metric_id)
        rows.append({
            "label": spec[0] if spec is not None else metric_id,
            "value": "不可用",
            "band": "unavailable",
        })
    return rows


def _rates_credit_component_rows(run: dict) -> list[dict]:
    rows = _component_rows(run.get("rates_credit_components") or [])
    metadata = run.get("rates_credit_meta") or {}
    missing = [
        *(run.get("rates_credit_unavailable") or []),
        *(metadata.get("missing_core") or []),
        *(metadata.get("optional_missing") or []),
    ]
    return rows + _unavailable_component_rows(missing)


def build_report(run: dict) -> dict:
    """The daily JSON report. ``run`` is the ``daily.run()`` result (or the
    equivalent dict replayed from stored snapshots)."""
    scores = {
        "mbs": run["mbs"],
        "css": run["css"],
        "afrs": run["afrs"],
    }
    if "rates_credit" in run:
        scores["rates_credit"] = run["rates_credit"]
    return {
        "as_of": run["as_of"],
        "scores": scores,
        "state": run["state"],
        "action": run.get("action"),
        "indication": run.get("indication"),
        "severity": SEVERITY_BY_STATE.get(run.get("state", "NORMAL"), "INFO"),
        "reasons": run.get("reasons") or [],
        "mbs_unavailable": run.get("mbs_unavailable") or [],
        "css_unavailable": run.get("css_unavailable") or [],
        "rates_credit_unavailable": run.get("rates_credit_unavailable") or [],
        "breadth": run.get("breadth") or {},
        "quality_status": run.get("quality_status", "ok"),
        "stale_days": run.get("stale_days", 0),
        "anomalous": run.get("anomalous", False),
        "components": {
            "mbs": _component_rows(run.get("mbs_components") or []),
            "css": _component_rows(run.get("css_components") or []),
            "rates_credit": _rates_credit_component_rows(run),
        },
    }


def push_summary(report: dict) -> str:
    """The one line that belongs on a lock screen: state + the four scores.

    ``afrs`` may be ``None`` before the fundamental layer is wired; it is shown
    as ``-``, never as a fabricated zero."""
    scores = report["scores"]
    mbs = _fmt(scores.get("mbs"))
    css = _fmt(scores.get("css"))
    afrs = _fmt(scores.get("afrs"))
    rates_credit = _fmt(scores.get("rates_credit"))
    state = report.get("state", "NORMAL")
    label = _STATE_LABEL.get(state, state)
    action = report.get("action")
    head = f"{action}；系统性风险 {label}" if action else f"系统性风险 {label}"
    prefix = ""
    if report.get("anomalous"):
        prefix += "⚠ 异常 "
    if report.get("stale_days", 0) > _STALE_DAYS_THRESHOLD:
        prefix += "⚠ 数据过期 "
    if report.get("quality_status") == "data_quality_warning":
        prefix += "⚠ 数据不完整 "
    suffix = f" / RCS {rates_credit}" if "rates_credit" in scores else ""
    return f"{prefix}{head} — MBS {mbs} / CSS {css} / AFRS {afrs}{suffix}"


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
        f"- MBS：{_fmt(scores.get('mbs'))}  / CSS：{_fmt(scores.get('css'))}  / AFRS：{_fmt(scores.get('afrs'))}"
        + (f"  / RCS：{_fmt(scores.get('rates_credit'))}" if "rates_credit" in scores else ""),
    ]
    reasons = report.get("reasons") or []
    if reasons:
        lines.append("- 触发原因：" + "；".join(reasons))
    for label, key in (
        ("MBS", "mbs_unavailable"),
        ("CSS", "css_unavailable"),
        ("RCS", "rates_credit_unavailable"),
    ):
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
