"""`DEV-034`: the registry must refuse, and the catalog must stay printable.

Design 10.4's bar is `secret scan 为零`. This file tries to *reach* that bar by
attacking it rather than by scanning for it: each test below is an attempt to
get a secret-shaped string into a metric through a different door. A scan-based
test would only ever prove that today's patterns do not match today's data.
"""

from __future__ import annotations

import pytest

from personal_agent_core.metric_catalog import CATALOG, NOT_YET_EMITTED
from personal_agent_core.metrics import (
    MetricError,
    MetricRegistry,
    MetricSpec,
    percentile,
)

# Values a caller might plausibly try to pass, each of which would be a leak.
SECRET_SHAPED = [
    "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "bascnCONFIDENTIALtoken",
    "tblSECRETtable01",
    "recABCDEF123456",
    "cli_a1b2c3d4e5f6",
    "TESTTEAM01",
    "/home/example/personal-agent/.env.local",
]


@pytest.fixture()
def registry() -> MetricRegistry:
    return MetricRegistry(CATALOG)


# --- the acceptance criterion, as an attack ----------------------------------


@pytest.mark.parametrize("hostile", SECRET_SHAPED)
def test_a_secret_shaped_label_value_is_refused(registry, hostile) -> None:
    with pytest.raises(MetricError):
        registry.increment("policy_denied_total", reason=hostile)
    with pytest.raises(MetricError):
        registry.increment(
            "provider_response_total", provider="feishu", status_class=hostile
        )
    assert registry.snapshot() == [], "a refused label must record nothing at all"


@pytest.mark.parametrize("hostile", SECRET_SHAPED)
def test_the_refusal_message_does_not_repeat_the_value(registry, hostile) -> None:
    """The exception travels to the same journal the metric would have.

    Refusing a value and then printing it in the error is not a refusal; it is a
    slower leak with an extra step. This is the test that keeps the message
    describing the *allowed* set instead of the rejected input.
    """
    with pytest.raises(MetricError) as raised:
        registry.increment("policy_denied_total", reason=hostile)
    assert hostile not in str(raised.value)


def test_a_snapshot_contains_only_catalog_strings(registry) -> None:
    """Every string in a snapshot must be traceable to the catalog.

    This is the structural statement of `secret scan 为零`: not "we scanned and
    found nothing" but "there is nowhere for anything else to have come from".
    """
    registry.increment("policy_denied_total", reason="schema_drift")
    registry.observe("api_request_seconds", 0.1, route="chat", outcome="ok")
    registry.increment(
        "provider_response_total", provider="feishu", status_class="429"
    )

    allowed = {"metric", "kind", "unit", "labels", "value", "count", "p50", "p95", "max"}
    for spec in CATALOG.values():
        allowed.add(spec.name)
        allowed.add(spec.unit)
        allowed.update(spec.labels)
        for values in spec.labels.values():
            allowed.update(values)
    allowed.update({"counter", "latency"})

    for row in registry.snapshot():
        for key, value in row.items():
            assert key in allowed
            if isinstance(value, str):
                assert value in allowed
            if key == "labels":
                for label, label_value in value.items():
                    assert label in allowed
                    assert label_value in allowed


# --- the registry refuses everything the catalog did not authorise ------------


def test_an_unknown_metric_is_refused(registry) -> None:
    with pytest.raises(MetricError, match="not in the metric catalog"):
        registry.increment("something_someone_invented_total")


def test_a_missing_label_is_refused_rather_than_defaulted(registry) -> None:
    # Defaulting would silently merge unrelated series under one name.
    with pytest.raises(MetricError, match="requires labels"):
        registry.increment("provider_response_total", provider="feishu")


def test_an_extra_label_is_refused_rather_than_dropped(registry) -> None:
    # Dropping is how a caller's `table=tbl123` becomes invisible instead of
    # impossible; the leak would then depend on a future change re-enabling it.
    with pytest.raises(MetricError, match="does not declare labels"):
        registry.increment(
            "policy_denied_total", reason="schema_drift", table="expense"
        )


def test_using_a_counter_as_a_latency_is_refused(registry) -> None:
    with pytest.raises(MetricError, match="is a counter, not a latency"):
        registry.observe("policy_denied_total", 0.5, reason="schema_drift")
    with pytest.raises(MetricError, match="is a latency, not a counter"):
        registry.increment("api_request_seconds", route="chat", outcome="ok")


def test_a_counter_cannot_go_backwards(registry) -> None:
    with pytest.raises(MetricError, match="monotonic"):
        registry.increment("review_decision_total", amount=-1, decision="ack")


def test_a_negative_duration_is_refused(registry) -> None:
    with pytest.raises(MetricError, match="negative duration"):
        registry.observe("review_build_seconds", -0.01, outcome="ok")


def test_a_non_string_label_is_refused(registry) -> None:
    # `outcome=True` would otherwise become the string "True" somewhere later.
    with pytest.raises(MetricError, match="must be a string"):
        registry.increment(
            "provider_response_total", provider="feishu", status_class=429
        )


def test_a_catalog_whose_key_disagrees_with_its_spec_is_refused() -> None:
    bad = {"one_name": MetricSpec("another_name", "counter", "x", "help", {})}
    with pytest.raises(MetricError, match="does not match spec name"):
        MetricRegistry(bad)


# --- what it actually records -------------------------------------------------


def test_counters_accumulate_per_label_set(registry) -> None:
    registry.increment("review_decision_total", decision="ack")
    registry.increment("review_decision_total", decision="ack")
    registry.increment("review_decision_total", decision="defer")
    values = {
        row["labels"]["decision"]: row["value"] for row in registry.snapshot()
    }
    assert values == {"ack": 2, "defer": 1}


def test_a_latency_series_reports_its_own_sample_count(registry) -> None:
    """`count` exists so a P95 is never read without knowing what it summarises."""
    for value in (0.1, 0.2, 0.3, 0.4):
        registry.observe("review_build_seconds", value, outcome="ok")
    row = registry.snapshot()[0]
    assert row["count"] == 4
    assert row["max"] == pytest.approx(0.4)


def test_the_window_bounds_memory_and_the_count_says_so() -> None:
    registry = MetricRegistry(CATALOG, window=3)
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        registry.observe("review_build_seconds", value, outcome="ok")
    row = registry.snapshot()[0]
    # Last three only, and the count does not claim otherwise.
    assert row["count"] == 3
    assert row["p50"] == pytest.approx(4.0)


def test_snapshot_order_is_stable(registry) -> None:
    registry.increment("review_decision_total", decision="defer")
    registry.increment("review_decision_total", decision="ack")
    first = [row["labels"]["decision"] for row in registry.snapshot()]
    assert first == sorted(first)


# --- percentiles ---------------------------------------------------------------


def test_percentiles_are_nearest_rank_not_interpolated() -> None:
    """An interpolated P95 reports a latency no request ever had.

    With a window this small that is not pedantry: the reported number should be
    findable in a log.
    """
    samples = [float(n) for n in range(1, 101)]
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 95) == 95.0
    assert percentile(samples, 100) == 100.0
    # Every reported value is one of the observations.
    assert percentile([1.0, 2.0], 95) in {1.0, 2.0}


def test_percentile_of_nothing_is_none_not_zero() -> None:
    # Zero would read as "instantaneous", which is the opposite of "unknown".
    assert percentile([], 95) is None


def test_a_single_sample_is_its_own_percentile() -> None:
    assert percentile([0.42], 50) == 0.42
    assert percentile([0.42], 95) == 0.42


# --- the declared-but-unwired gap must stay honest ----------------------------


def test_the_unemitted_metrics_are_named_and_real(registry) -> None:
    """A zero from an unwired metric reads as health; the name is the antidote."""
    assert NOT_YET_EMITTED
    for name in NOT_YET_EMITTED:
        assert name in CATALOG, "an unemitted name must still be a real metric"
        assert "NOT YET EMITTED" in CATALOG[name].help
    # And it really is absent from a fresh snapshot rather than silently zero.
    assert registry.snapshot() == []
