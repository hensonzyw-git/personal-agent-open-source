"""Treasury and credit-condition derivations used by the daily risk card.

The module is deliberately pure.  It turns collected FRED observations plus
the already-derived HY OAS change into the inputs for the RCS score and the
position-action overlay.  A high Treasury yield is a financing headwind, not
by itself a crash signal; the exposure-review branch requires credit and
market confirmation as well.
"""

from __future__ import annotations

from typing import Optional

from risk_monitor import derive


def _latest_value(item: object) -> Optional[float]:
    if not isinstance(item, (tuple, list)) or len(item) < 2:
        return None
    value = item[1]
    return float(value) if value is not None else None


def derive_values(
    raw: dict[str, object],
    *,
    hy_oas_20d_change_bp: Optional[float] = None,
) -> tuple[dict[str, float], dict[str, object]]:
    """Return ``(RCS values, provenance metadata)`` from raw daily inputs.

    Missing history or latest observations are omitted, never replaced with a
    green value.  ``metadata`` records the inputs that were not available so
    the card can distinguish a complete RCS reading from the free-source
    baseline's intentionally unavailable MOVE/valuation evidence.
    """
    values: dict[str, float] = {}

    ten_y = _latest_value(raw.get("dgs10_latest"))
    real_ten_y = _latest_value(raw.get("dfii10_latest"))
    two_y = _latest_value(raw.get("dgs2_latest"))
    three_m = _latest_value(raw.get("dgs3mo_latest"))

    if ten_y is not None:
        values["rates.10y_yield_pct"] = ten_y
    if real_ten_y is not None:
        values["rates.10y_real_yield_pct"] = real_ten_y
    if ten_y is not None and two_y is not None:
        values["rates.10y_2y_spread_bp"] = round((ten_y - two_y) * 100.0, 4)
    if ten_y is not None and three_m is not None:
        values["rates.10y_3m_spread_bp"] = round((ten_y - three_m) * 100.0, 4)

    ten_y_history = raw.get("dgs10_history")
    real_history = raw.get("dfii10_history")
    thirty_history = raw.get("dgs30_history")
    if ten_y_history:
        change = derive.change_over_days(ten_y_history, 20, basis_points=True)  # type: ignore[arg-type]
        if change is not None:
            values["rates.10y_20d_change_bp"] = change
    if real_history:
        change = derive.change_over_days(real_history, 20, basis_points=True)  # type: ignore[arg-type]
        if change is not None:
            values["rates.10y_real_20d_change_bp"] = change
    if thirty_history:
        change = derive.change_over_days(thirty_history, 20, basis_points=True)  # type: ignore[arg-type]
        if change is not None:
            values["rates.30y_20d_change_bp"] = change
    if hy_oas_20d_change_bp is not None:
        values["rates.hy_oas_20d_change_bp"] = hy_oas_20d_change_bp

    expected = {
        "rates.10y_yield_pct",
        "rates.10y_real_yield_pct",
        "rates.10y_20d_change_bp",
        "rates.10y_real_20d_change_bp",
        "rates.10y_2y_spread_bp",
        "rates.10y_3m_spread_bp",
        "rates.30y_20d_change_bp",
        "rates.hy_oas_20d_change_bp",
    }
    metadata = {
        "available": sorted(values),
        "missing_core": sorted(expected - values.keys()),
        "optional_missing": [
            "rates.move_index",
            "rates.treasury_liquidity",
            "rates.fed_funds_futures",
            "market.valuation",
        ],
    }
    return values, metadata


def position_action(
    policy: dict,
    *,
    state: str,
    values: dict[str, float],
    mbs: Optional[float],
    css: Optional[float],
) -> tuple[str, list[str]]:
    """Apply the conversation's position overlay to the confirmed state.

    The overlay is intentionally conservative:

    * 4.7--5.0% 10Y: slow new USD-equity buying and stage new cash;
    * >=5.0% plus rising real yields and widening HY OAS, with market/credit
      confirmation: ask for an active equity-exposure review;
    * below 4.5% with easing real yields and non-widening HY OAS: restore the
      normal staged-buying pace.

    A confirmed CREDIT_CONFIRMATION or DELEVERAGING state is always stronger
    than this overlay, so it remains authoritative even when rates are moving.
    The easing branch is deliberately limited to NORMAL: lower rates do not
    cancel a separate risk-accumulation signal.
    """
    fallback = policy["actions"].get(state, state)
    if state in {"CREDIT_CONFIRMATION", "DELEVERAGING"}:
        return fallback, ["confirmed_state_action"]

    ten_y = values.get("rates.10y_yield_pct")
    real_change = values.get("rates.10y_real_20d_change_bp")
    hy_change = values.get("rates.hy_oas_20d_change_bp")
    if ten_y is None:
        return fallback, ["rates_credit_missing_10y"]

    if crash_trigger(values, mbs=mbs, css=css):
        return (
            "考虑主动降低权益总仓位（人工确认）",
            [
                "10y>=5.0%",
                "10y_real_yield_20d_rising",
                "hy_oas_20d_widening",
                "market_or_credit_confirmation",
            ],
        )

    if state in {"NORMAL", "RISK_ACCUMULATION"} and 4.7 <= ten_y < 5.0:
        return "暂停激进加仓，新增美元资金分批投入", ["10y_in_4.7%-5.0%_caution_zone"]

    if (
        ten_y < 4.5
        and real_change is not None
        and real_change <= 0.0
        and hy_change is not None
        and hy_change < 25.0
        and state == "NORMAL"
    ):
        return "恢复正常加仓速度（分批投入）", ["rates_credit_easing_without_credit_stress"]

    return fallback, []


def crash_trigger(
    values: dict[str, float],
    *,
    mbs: Optional[float],
    css: Optional[float],
) -> bool:
    """Return the rates-to-crash confirmation trigger.

    This is intentionally stricter than the RCS score: nominal 10Y >= 5% and
    a rising real yield are joined by HY OAS widening plus either market or
    credit confirmation.  MOVE and valuation remain explicit optional inputs
    in policy, but are not fabricated here when no verified daily source is
    available.
    """
    return bool(
        values.get("rates.10y_yield_pct", 0.0) >= 5.0
        and values.get("rates.10y_real_20d_change_bp", 0.0) > 0.0
        and values.get("rates.hy_oas_20d_change_bp", 0.0) >= 50.0
        and (
            (mbs is not None and mbs >= 55.0)
            or (css is not None and css >= 55.0)
        )
    )
