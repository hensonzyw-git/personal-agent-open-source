"""CAP-001 slice A: the typed context configuration.

Covers failure set F-A1..F-A8 (`docs/CAP-001失败集_v0.1.md` §3). The property
being defended is that no model input can ever be measured against a budget the
service did not fully validate at startup.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from personal_agent.context.config import (
    CAP001_PROVISIONAL_VALUES,
    CONFIG_KEYS,
    ContextConfig,
    ContextConfigError,
    default_context_config,
    operator_override_from_env,
)


def _values(**overrides: object) -> dict[str, object]:
    values = dict(CAP001_PROVISIONAL_VALUES)
    values.update(overrides)
    return values


def _config(**overrides: object) -> ContextConfig:
    return ContextConfig.from_mapping("ctx-test", _values(**overrides))


def test_default_configuration_is_valid_and_provisionally_named() -> None:
    config = default_context_config()
    # The name states its own status: the numbers are frozen only after the
    # CAP-001 eval, so evidence cannot claim the frozen version by accident.
    assert config.name == "ctx-cap001-provisional-1"
    assert config.config_version.startswith("ctx-cap001-provisional-1.")


# -- F-A1: a missing key fails closed, it is never defaulted ---------------


@pytest.mark.parametrize("key", CONFIG_KEYS)
def test_missing_key_fails_closed(key: str) -> None:
    values = _values()
    del values[key]
    with pytest.raises(ContextConfigError) as excinfo:
        ContextConfig.from_mapping("ctx-test", values)
    assert key in str(excinfo.value)


def test_unknown_key_is_refused_rather_than_ignored() -> None:
    # An ignored typo looks exactly like an applied value.
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping(
            "ctx-test", _values(CONTEXT_SOFT_LIMIT_TOKEN=16000)
        )


def test_configuration_needs_a_name() -> None:
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping("  ", _values())


# -- F-A2: non-positive or non-integer values are refused ------------------


@pytest.mark.parametrize(
    "key",
    [key for key in CONFIG_KEYS if key != "CONTEXT_ESTIMATE_SAFETY_MARGIN"],
)
@pytest.mark.parametrize("bad", [0, -1, "16000", 16000.0, True, None])
def test_non_positive_or_non_integer_is_refused(key: str, bad: object) -> None:
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping("ctx-test", _values(**{key: bad}))


@pytest.mark.parametrize("bad", [0.15, 1, None, "nope", "NaN"])
def test_safety_margin_must_be_an_exact_decimal(bad: object) -> None:
    # A binary float margin would quietly shrink the safety it exists to give.
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping(
            "ctx-test", _values(CONTEXT_ESTIMATE_SAFETY_MARGIN=bad)
        )


@pytest.mark.parametrize("bad", ["1", "1.5", "-0.1"])
def test_safety_margin_must_be_between_zero_and_one(bad: str) -> None:
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping(
            "ctx-test", _values(CONTEXT_ESTIMATE_SAFETY_MARGIN=bad)
        )


def test_safety_margin_accepts_a_decimal_instance() -> None:
    config = _config(CONTEXT_ESTIMATE_SAFETY_MARGIN=Decimal("0.2"))
    assert config.estimate_safety_margin == Decimal("0.2")


# -- F-A3 / F-A4: the §7.1 inequality holds or the service does not start ---


@pytest.mark.parametrize("soft", [24000, 30000])
def test_soft_limit_must_be_below_hard_limit(soft: int) -> None:
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping(
            "ctx-test", _values(CONTEXT_SOFT_LIMIT_TOKENS=soft)
        )


def test_total_budget_must_fit_the_product_ceiling() -> None:
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping(
            "ctx-test",
            _values(
                CONTEXT_HARD_LIMIT_TOKENS=31000,
                CONTEXT_SOFT_LIMIT_TOKENS=16000,
            ),
        )


def test_total_budget_may_exactly_meet_the_product_ceiling() -> None:
    config = _config(
        CONTEXT_PRODUCT_CEILING_TOKENS=24000 + 2048 + 4096,
    )
    assert config.hard_limit_tokens == 24000


def test_timeline_page_default_may_not_exceed_the_maximum() -> None:
    with pytest.raises(ContextConfigError):
        ContextConfig.from_mapping(
            "ctx-test", _values(CONTEXT_TIMELINE_PAGE_DEFAULT=200)
        )


# -- F-A5: the adapter's declared model limit is authoritative -------------


def test_config_exceeding_adapter_model_limit_is_refused() -> None:
    config = default_context_config()
    with pytest.raises(ContextConfigError):
        config.require_within_model_limit(8000)


def test_config_within_adapter_model_limit_is_accepted() -> None:
    config = default_context_config()
    config.require_within_model_limit(200_000)


def test_absent_adapter_limit_is_not_read_as_unlimited() -> None:
    # `None` means "nothing to compare"; the product ceiling still bound the
    # configuration at construction time.
    config = default_context_config()
    config.require_within_model_limit(None)
    assert (
        config.hard_limit_tokens
        + config.reserved_output_tokens
        + config.reserved_tool_tokens
        <= config.product_ceiling_tokens
    )


@pytest.mark.parametrize("bad", [0, -1, True, "200000", 200000.0])
def test_malformed_adapter_limit_is_refused(bad: object) -> None:
    with pytest.raises(ContextConfigError):
        default_context_config().require_within_model_limit(bad)  # type: ignore[arg-type]


# -- F-A6: a request may only tighten, never widen -------------------------


def test_page_limit_is_clamped_down_never_up() -> None:
    config = default_context_config()
    assert config.page_limit(None) == config.timeline_page_default
    assert config.page_limit(3) == 3
    assert config.page_limit(10_000) == config.timeline_page_max


@pytest.mark.parametrize("bad", [0, -5, True, "30", 30.0])
def test_malformed_page_limit_is_refused(bad: object) -> None:
    with pytest.raises(ContextConfigError):
        default_context_config().page_limit(bad)  # type: ignore[arg-type]


def test_config_is_frozen_against_in_flight_mutation() -> None:
    config = default_context_config()
    with pytest.raises(Exception):
        config.hard_limit_tokens = 999_999  # type: ignore[misc]


# -- F-A7: the version is derived from the content -------------------------


def test_config_version_is_deterministic() -> None:
    assert default_context_config().config_version == (
        default_context_config().config_version
    )


def test_config_version_changes_with_any_value() -> None:
    baseline = _config().config_version
    assert _config(CONTEXT_SOFT_LIMIT_TOKENS=15999).config_version != baseline
    assert (
        _config(CONTEXT_ESTIMATE_SAFETY_MARGIN="0.16").config_version != baseline
    )
    assert (
        ContextConfig.from_mapping("ctx-other", _values()).config_version
        != baseline
    )


# -- F-A8: the configuration carries nothing sensitive ---------------------


def test_config_carries_no_secret_or_resource_id() -> None:
    body = default_context_config().as_dict()
    assert set(body) == set(CONFIG_KEYS)
    for value in body.values():
        assert isinstance(value, (int, str))
    # Every value is a number or the decimal margin: there is no field a
    # credential, user text or external resource id could be carried in.
    assert all(
        isinstance(value, int) or value == "0.15" for value in body.values()
    )


# -- operator overrides (2026-08-30): the idle threshold is retunable -------


def test_override_replaces_only_the_named_value() -> None:
    config = default_context_config({"CONTEXT_SESSION_IDLE_MINUTES": 1})
    baseline = default_context_config()
    assert config.session_idle_minutes == 1
    assert config.hard_limit_tokens == baseline.hard_limit_tokens
    assert config.config_version != baseline.config_version


def test_override_rejects_unknown_keys() -> None:
    with pytest.raises(ContextConfigError, match="override has unknown keys"):
        default_context_config({"CONTEXT_UNKNOWN_KEY": 1})


def test_override_rejects_non_positive_values() -> None:
    with pytest.raises(ContextConfigError, match="must be positive"):
        default_context_config({"CONTEXT_SESSION_IDLE_MINUTES": 0})


@pytest.mark.parametrize(
    "raw", ["abc", "480m", "", "   ", "1.5", "+60", "6_0", "６０", "-5"]
)
def test_env_override_fails_closed_on_malformed_values(raw: str) -> None:
    with pytest.raises(ContextConfigError):
        operator_override_from_env({"CONTEXT_SESSION_IDLE_MINUTES": raw})


def test_env_override_rejects_values_beyond_the_upper_bound() -> None:
    # int() would happily parse an unbounded digit string; without a cap an
    # accidental huge value silently disables the idle boundary.
    with pytest.raises(ContextConfigError, match="must not exceed"):
        operator_override_from_env(
            {"CONTEXT_SESSION_IDLE_MINUTES": "99999999999999999999"}
        )


def test_env_override_accepts_the_bound_itself() -> None:
    assert operator_override_from_env(
        {"CONTEXT_SESSION_IDLE_MINUTES": "1440"}
    ) == {"CONTEXT_SESSION_IDLE_MINUTES": 1440}


def test_env_override_accepts_a_positive_integer() -> None:
    overrides = operator_override_from_env(
        {"CONTEXT_SESSION_IDLE_MINUTES": " 90 "}
    )
    assert overrides == {"CONTEXT_SESSION_IDLE_MINUTES": 90}


def test_env_override_absent_or_unrelated_keys_yield_nothing() -> None:
    assert operator_override_from_env({}) == {}
    assert operator_override_from_env(
        {"GLM_MODEL": "glm-5.3", "PERSONAL_AGENT_USER_ID": "henson"}
    ) == {}
