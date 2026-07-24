"""DEV-023 (offline): the Frankfurter ECB FX connector and money resolution.

Everything here runs against an httpx MockTransport and a fake clock -- no
network and no real provider call. The one live read-only confirmation of the
provider's response shape is recorded in the handoff, separate from this fixture.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.finance.fx_connector import (
    DEFAULT_CACHE_TTL_SECONDS,
    RATE_SOURCE,
    FxConnector,
)
from personal_data_mcp.finance.money_resolution import (
    resolve_money,
    with_currency_suffix,
)


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _latest_body(base: str, cny: object, day: str = "2026-07-23") -> dict:
    return {"amount": 1.0, "base": base, "date": day, "rates": {"CNY": cny}}


class CountingHandler:
    """A MockTransport handler that records every request and is swappable."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.respond(request)


def connector(handler, clock: FakeClock, **kwargs) -> FxConnector:
    return FxConnector(
        now=clock,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# --- a successful quote ------------------------------------------------------


def test_a_quote_carries_a_decimal_rate_and_the_providers_date() -> None:
    clock = FakeClock()
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body("JPY", 0.04141))
    )
    with_conn = connector(handler, clock)

    quote = run(with_conn.rate("JPY"))

    assert quote.currency == "JPY"
    assert quote.rate == Decimal("0.04141")
    assert isinstance(quote.rate, Decimal)
    # The provider reported the 23rd (ECB had not published for the 24th yet);
    # the connector records that quote day, not "today".
    assert quote.quote_date.isoformat() == "2026-07-23"
    assert quote.source == RATE_SOURCE
    assert quote.fetched_at == clock()
    run(with_conn.aclose())


def test_the_request_pins_the_base_and_asks_only_for_cny() -> None:
    clock = FakeClock()
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body("USD", 7.1))
    )
    conn = connector(handler, clock)

    run(conn.rate("USD"))

    req = handler.calls[0]
    assert req.url.host == "api.frankfurter.dev"
    assert req.url.path == "/v1/latest"
    assert req.url.params["base"] == "USD"
    assert req.url.params["symbols"] == "CNY"
    run(conn.aclose())


def test_the_connector_does_not_accept_an_overridable_base_url() -> None:
    clock = FakeClock()
    with pytest.raises(TypeError):
        FxConnector(  # type: ignore[call-arg]
            now=clock,
            base_url="http://127.0.0.1:9999",
        )


# --- the cache ---------------------------------------------------------------


def test_a_fresh_quote_is_reused_without_a_second_call() -> None:
    clock = FakeClock()
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body("JPY", 0.04141))
    )
    conn = connector(handler, clock)

    first = run(conn.rate("JPY"))
    clock.advance(DEFAULT_CACHE_TTL_SECONDS - 1)
    second = run(conn.rate("JPY"))

    assert len(handler.calls) == 1
    assert second == first
    run(conn.aclose())


def test_the_cache_expires_after_its_ttl() -> None:
    clock = FakeClock()
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body("JPY", 0.04141))
    )
    conn = connector(handler, clock)

    run(conn.rate("JPY"))
    clock.advance(DEFAULT_CACHE_TTL_SECONDS)
    run(conn.rate("JPY"))

    assert len(handler.calls) == 2
    run(conn.aclose())


def test_a_backwards_clock_is_treated_as_stale() -> None:
    clock = FakeClock()
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body("JPY", 0.04141))
    )
    conn = connector(handler, clock)

    run(conn.rate("JPY"))
    clock.advance(-10)  # a clock that appears to run backwards refetches
    run(conn.rate("JPY"))

    assert len(handler.calls) == 2
    run(conn.aclose())


def test_different_currencies_are_cached_separately() -> None:
    clock = FakeClock()

    def respond(req: httpx.Request) -> httpx.Response:
        base = req.url.params["base"]
        rate = 0.04141 if base == "JPY" else 7.1
        return httpx.Response(200, json=_latest_body(base, rate))

    handler = CountingHandler(respond)
    conn = connector(handler, clock)

    jpy = run(conn.rate("JPY"))
    usd = run(conn.rate("USD"))

    assert len(handler.calls) == 2
    assert jpy.rate == Decimal("0.04141")
    assert usd.rate == Decimal("7.1")
    # And each is now individually cached.
    run(conn.rate("JPY"))
    run(conn.rate("USD"))
    assert len(handler.calls) == 2
    run(conn.aclose())


# --- fail closed -------------------------------------------------------------


@pytest.mark.parametrize(
    "respond",
    [
        pytest.param(
            lambda req: httpx.Response(503, json={"error": "down"}), id="http-503"
        ),
        pytest.param(
            lambda req: httpx.Response(200, content=b"not json"), id="non-json"
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", None)),
            id="null-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", "abc")),
            id="junk-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", 0)),
            id="zero-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", -1.0)),
            id="negative-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", True)),
            id="bool-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", "NaN")),
            id="nan-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("JPY", "Infinity")),
            id="infinite-rate",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json={"base": "JPY", "date": "2026-07-23"}),
            id="no-rates",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=_latest_body("EUR", 0.04141)),
            id="wrong-base",
        ),
        pytest.param(
            lambda req: httpx.Response(
                200, json=_latest_body("JPY", 0.04141, day="not-a-date")
            ),
            id="bad-date",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=[1, 2, 3]), id="not-an-object"
        ),
    ],
)
def test_a_bad_response_fails_closed_as_rate_unavailable(respond) -> None:
    clock = FakeClock()
    conn = connector(CountingHandler(respond), clock)

    with pytest.raises(AppError) as exc:
        run(conn.rate("JPY"))
    assert exc.value.code is ErrorCode.FX_RATE_UNAVAILABLE
    run(conn.aclose())


def test_a_transport_error_fails_closed() -> None:
    clock = FakeClock()

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=req)

    conn = connector(CountingHandler(boom), clock)

    with pytest.raises(AppError) as exc:
        run(conn.rate("JPY"))
    assert exc.value.code is ErrorCode.FX_RATE_UNAVAILABLE
    run(conn.aclose())


def test_a_failure_is_not_cached() -> None:
    clock = FakeClock()
    state = {"fail": True}

    def flaky(req: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=_latest_body("JPY", 0.04141))

    handler = CountingHandler(flaky)
    conn = connector(handler, clock)

    with pytest.raises(AppError):
        run(conn.rate("JPY"))
    state["fail"] = False
    quote = run(conn.rate("JPY"))  # no stale failure pinned; it refetches

    assert quote.rate == Decimal("0.04141")
    assert len(handler.calls) == 2
    run(conn.aclose())


def test_cny_is_refused_without_a_network_call() -> None:
    clock = FakeClock()
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body("CNY", 1.0))
    )
    conn = connector(handler, clock)

    with pytest.raises(AppError) as exc:
        run(conn.rate("CNY"))
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert handler.calls == []
    run(conn.aclose())


def test_a_non_iso_currency_is_refused() -> None:
    clock = FakeClock()
    handler = CountingHandler(lambda req: httpx.Response(200, json={}))
    conn = connector(handler, clock)

    with pytest.raises(ValueError):
        run(conn.rate("Yen"))
    assert handler.calls == []
    run(conn.aclose())


# --- money resolution --------------------------------------------------------


def _fx(clock: FakeClock, rate: object = 0.04141, base: str = "JPY") -> FxConnector:
    handler = CountingHandler(
        lambda req: httpx.Response(200, json=_latest_body(req.url.params["base"], rate))
    )
    conn = connector(handler, clock)
    conn._probe = handler  # type: ignore[attr-defined]  # for call-count assertions
    return conn


def test_a_settlement_amount_wins_and_queries_no_rate() -> None:
    clock = FakeClock()
    fx = _fx(clock)

    resolution = run(
        resolve_money(
            input_amount="10000",
            input_currency="JPY",
            settlement_amount_cny="415.00",
            fx=fx,
        )
    )

    assert resolution.amount_cny == Decimal("415.00")
    assert resolution.currency_suffix is None
    assert resolution.fx_audit is None
    assert fx._probe.calls == []  # type: ignore[attr-defined]
    run(fx.aclose())


def test_cny_passes_through_without_a_rate() -> None:
    clock = FakeClock()
    fx = _fx(clock)

    resolution = run(
        resolve_money(input_amount="88.50", input_currency="CNY", fx=fx)
    )

    assert resolution.amount_cny == Decimal("88.50")
    assert resolution.currency_suffix is None
    assert resolution.fx_audit is None
    assert fx._probe.calls == []  # type: ignore[attr-defined]
    run(fx.aclose())


def test_cny_is_the_default_currency() -> None:
    clock = FakeClock()
    fx = _fx(clock)

    resolution = run(resolve_money(input_amount="12.00", fx=fx))

    assert resolution.amount_cny == Decimal("12.00")
    assert resolution.fx_audit is None
    run(fx.aclose())


def test_a_foreign_amount_converts_and_records_a_removable_suffix() -> None:
    clock = FakeClock()
    fx = _fx(clock, rate=0.04141)

    resolution = run(
        resolve_money(input_amount="10000", input_currency="JPY", fx=fx)
    )

    # 10000 * 0.04141 = 414.10, quantised to cents; a positive magnitude, since
    # the accounting sign is applied downstream from entry_kind.
    assert resolution.amount_cny == Decimal("414.10")
    assert resolution.currency_suffix == "（10,000 JPY）"
    audit = resolution.fx_audit
    assert audit is not None
    assert audit.original_amount == Decimal("10000")
    assert audit.original_currency == "JPY"
    assert audit.rate == Decimal("0.04141")
    assert audit.rate_source == RATE_SOURCE
    assert audit.quote_date.isoformat() == "2026-07-23"
    assert audit.quoted_at == clock()
    assert audit.is_reference_estimate is True
    run(fx.aclose())


def test_conversion_rounds_half_up_at_cents() -> None:
    clock = FakeClock()
    fx = _fx(clock, rate="0.123456", base="USD")

    resolution = run(
        resolve_money(input_amount="100", input_currency="USD", fx=fx)
    )

    # 100 * 0.123456 = 12.3456 -> 12.35 half-up.
    assert resolution.amount_cny == Decimal("12.35")
    assert resolution.currency_suffix == "（100 USD）"
    run(fx.aclose())


def test_a_fractional_original_keeps_its_digits_in_the_suffix() -> None:
    clock = FakeClock()
    fx = _fx(clock, rate=7.1, base="USD")

    resolution = run(
        resolve_money(input_amount="1234.50", input_currency="USD", fx=fx)
    )

    assert resolution.currency_suffix == "（1,234.50 USD）"
    run(fx.aclose())


def test_the_suffix_lands_after_a_trip_tag_in_the_display_name() -> None:
    clock = FakeClock()
    fx = _fx(clock)

    resolution = run(
        resolve_money(input_amount="10000", input_currency="JPY", fx=fx)
    )
    # `display_name` has already put the trip tag on; the suffix lands last.
    assert with_currency_suffix("机票 #东京", resolution) == "机票 #东京（10,000 JPY）"
    run(fx.aclose())


def test_with_currency_suffix_leaves_a_cny_name_unchanged() -> None:
    clock = FakeClock()
    fx = _fx(clock)

    resolution = run(resolve_money(input_amount="20.00", fx=fx))
    assert with_currency_suffix("午饭", resolution) == "午饭"
    run(fx.aclose())


def test_a_foreign_amount_without_a_connector_is_an_internal_error() -> None:
    with pytest.raises(AppError) as exc:
        run(resolve_money(input_amount="10000", input_currency="JPY", fx=None))
    assert exc.value.code is ErrorCode.INTERNAL_ERROR


def test_an_unavailable_rate_propagates_and_blocks_the_write() -> None:
    clock = FakeClock()
    handler = CountingHandler(lambda req: httpx.Response(503, json={"error": "down"}))
    conn = connector(handler, clock)

    with pytest.raises(AppError) as exc:
        run(resolve_money(input_amount="10000", input_currency="JPY", fx=conn))
    assert exc.value.code is ErrorCode.FX_RATE_UNAVAILABLE
    run(conn.aclose())
