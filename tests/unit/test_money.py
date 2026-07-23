"""DEV-003: amounts are exact decimals and floats never enter the ledger."""

from __future__ import annotations

from decimal import Decimal

import pytest

from personal_agent_core.money import (
    AmountError,
    CurrencyError,
    apply_entry_sign,
    convert_to_cny,
    format_cny,
    parse_amount,
    parse_currency,
    parse_signed_amount,
    quantize_cny,
)


@pytest.mark.parametrize("raw", ["45", "45.0", "45.00", "0.01", "6000"])
def test_valid_amounts_parse_exactly(raw: str) -> None:
    assert parse_amount(raw) == Decimal(raw)


@pytest.mark.parametrize("raw", [45.0, 45, Decimal("45"), True, None])
def test_non_string_amounts_are_rejected_not_coerced(raw: object) -> None:
    with pytest.raises(AmountError):
        parse_amount(raw)


@pytest.mark.parametrize(
    "raw",
    ["1e3", "NaN", "Infinity", "-1", "1.234", "", " 45", "45 ", "٤٥"],
)
def test_malformed_amounts_are_rejected(raw: str) -> None:
    with pytest.raises(AmountError):
        parse_amount(raw)


@pytest.mark.parametrize("raw", ["0", "0.0", "0.00"])
def test_zero_is_always_rejected(raw: str) -> None:
    with pytest.raises(AmountError):
        parse_amount(raw)


def test_signed_amounts_are_only_for_query_filters() -> None:
    assert parse_signed_amount("-1000.00") == Decimal("-1000.00")
    with pytest.raises(AmountError):
        parse_amount("-1000.00")


def test_quantize_uses_round_half_up_not_bankers_rounding() -> None:
    assert quantize_cny(Decimal("0.005")) == Decimal("0.01")
    assert quantize_cny(Decimal("0.015")) == Decimal("0.02")
    assert quantize_cny(Decimal("2.675")) == Decimal("2.68")


def test_quantize_rejects_float() -> None:
    with pytest.raises(AmountError):
        quantize_cny(0.005)  # type: ignore[arg-type]


def test_conversion_is_decimal_and_rounds_to_cents() -> None:
    converted = convert_to_cny(Decimal("10000"), Decimal("0.0479"))
    assert converted == Decimal("479.00")


def test_conversion_rejects_float_rate_and_non_positive_rate() -> None:
    with pytest.raises(AmountError):
        convert_to_cny(Decimal("100"), 0.0479)  # type: ignore[arg-type]
    with pytest.raises(AmountError):
        convert_to_cny(Decimal("100"), Decimal("0"))


@pytest.mark.parametrize(
    ("entry_kind", "expected"),
    [
        ("expense", Decimal("45.00")),
        ("refund", Decimal("-45.00")),
        ("aa_reimbursement", Decimal("-45.00")),
    ],
)
def test_entry_kind_decides_the_sign(entry_kind: str, expected: Decimal) -> None:
    assert apply_entry_sign(Decimal("45.00"), entry_kind) == expected


def test_an_already_negative_amount_is_never_double_negated() -> None:
    assert apply_entry_sign(Decimal("-500.00"), "refund") == Decimal("-500.00")
    assert apply_entry_sign(Decimal("-500.00"), "expense") == Decimal("500.00")


def test_unknown_entry_kind_is_rejected() -> None:
    with pytest.raises(AmountError):
        apply_entry_sign(Decimal("45.00"), "income")


def test_currency_codes_must_be_iso_alphabetic() -> None:
    assert parse_currency("JPY") == "JPY"
    for raw in ["$", "jpy", "JPYY", "12", "", None]:
        with pytest.raises(CurrencyError):
            parse_currency(raw)


def test_format_is_always_two_decimals() -> None:
    assert format_cny(Decimal("45")) == "45.00"
    assert format_cny(Decimal("-500")) == "-500.00"
