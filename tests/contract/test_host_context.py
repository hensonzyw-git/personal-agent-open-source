"""DEV-009: the internal token binds one call and nothing else."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import (
    ALGORITHM,
    BOUND_CLAIMS,
    HOST_ONLY_FIELDS,
    MAX_TTL,
    HostContext,
    HostContextError,
    ServiceKey,
    ServiceKeyRing,
    arguments_hash,
    sign_host_context,
    strip_host_only_fields,
    verify_host_context,
)


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
TOOL = "finance.log_expense"
KEY = "018f0000-0000-4000-8000-000000000001"
REQUEST = "018f0000-0000-4000-8000-000000000009"
ARGUMENTS = {
    "name": "午饭",
    "input_amount": "45.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}


@pytest.fixture()
def ring() -> ServiceKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return ServiceKeyRing(
        active=ServiceKey("svc-2026-01", private, private.public_key())
    )


def context(**overrides) -> HostContext:
    fields = {
        "agent_id": "agent-1",
        "device_id": "018f0000-0000-4000-8000-000000000002",
        "user_id": "henson",
        "scopes": ("finance.expense.write",),
        "tool": TOOL,
        "request_id": REQUEST,
        "trace_id": "tr-1",
        "idempotency_key": KEY,
        "request_fingerprint": "fp",
        "allowed_tools_version": "v1",
    }
    fields.update(overrides)
    return HostContext(**fields)


def verify(ring, token, **overrides):
    arguments = {
        "tool": TOOL,
        "idempotency_key": KEY,
        "request_id": REQUEST,
        "user_id": "henson",
        "trace_id": "tr-1",
        "timezone": "Asia/Shanghai",
        "arguments": ARGUMENTS,
        "now": NOW,
    }
    arguments.update(overrides)
    return verify_host_context(ring, token, **arguments)


def test_a_matching_call_verifies(ring) -> None:
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    claims = verify(ring, token)
    for claim in BOUND_CLAIMS:
        assert claim in claims
    assert claims["arguments_hash"] == arguments_hash(ARGUMENTS)


# --- what the binding is for ------------------------------------------------


@pytest.mark.parametrize(
    "swap",
    [
        {"tool": "finance.log_income"},
        {"idempotency_key": "018f0000-0000-4000-8000-0000000000ff"},
        {"request_id": "018f0000-0000-4000-8000-0000000000ee"},
        {"user_id": "other-user"},
        {"trace_id": "other-trace"},
        {"timezone": "UTC"},
    ],
)
def test_replaying_a_token_with_different_headers_fails(ring, swap) -> None:
    # A leaked internal token must be useless for anything but the one call it
    # was minted for.
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    with pytest.raises(AppError) as excinfo:
        verify(ring, token, **swap)
    assert excinfo.value.code is ErrorCode.HOST_CONTEXT_MISMATCH


@pytest.mark.parametrize(
    "tampered",
    [
        {**ARGUMENTS, "input_amount": "4500.00"},
        {**ARGUMENTS, "is_family_expense": True},
        {**ARGUMENTS, "category": "旅行"},
        {key: value for key, value in ARGUMENTS.items() if key != "trip_tag"}
        | {"trip_tag": "东京"},
    ],
)
def test_arguments_modified_in_flight_fail(ring, tampered) -> None:
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    with pytest.raises(AppError) as excinfo:
        verify(ring, token, arguments=tampered)
    assert excinfo.value.code is ErrorCode.HOST_CONTEXT_MISMATCH


def test_the_error_never_leaks_the_internal_detail(ring) -> None:
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    with pytest.raises(AppError) as excinfo:
        verify(ring, token, tool="finance.log_income")
    envelope = excinfo.value.to_envelope().model_dump(mode="json")
    assert "finance.log_income" not in str(envelope)
    assert envelope["code"] == ErrorCode.HOST_CONTEXT_MISMATCH


# --- the model cannot supply Host fields ------------------------------------


def test_host_only_fields_are_dropped_from_model_arguments() -> None:
    hostile = {
        **ARGUMENTS,
        "device_id": "someone-elses-device",
        "granted_scopes": ["finance.expense.write", "device.manage"],
        "idempotency_key": "chosen-by-the-model",
        "duplicate_override": {"approved": True},
    }
    cleaned = strip_host_only_fields(hostile)
    assert cleaned == ARGUMENTS
    for field in HOST_ONLY_FIELDS:
        assert field not in cleaned


def test_a_model_supplied_host_field_cannot_change_the_hash() -> None:
    # Otherwise a model could make an honest Host signature cover arguments the
    # Host never saw.
    forged = {**ARGUMENTS, "duplicate_override": {"approved": True}}
    assert arguments_hash(forged) == arguments_hash(ARGUMENTS)


def test_a_forged_duplicate_override_does_not_survive_verification(ring) -> None:
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    claims = verify(ring, token, arguments={**ARGUMENTS, "duplicate_override": True})
    assert "duplicate_override" not in claims


# --- a contract may declare a host-only name for itself ---------------------


CALENDAR_TOOL = "calendar.create_event"
CALENDAR_ARGUMENTS = {
    "title": "东京行",
    "start": "2027-01-01T00:00:00+08:00",
    "end": "2027-01-04T00:00:00+08:00",
    "all_day": True,
    "calendar": "出游计划",
    "timezone": "Asia/Tokyo",
    "start_date": "2027-01-01",
    "end_date": "2027-01-04",
}
#: What the tool's own schema declares, derived the same way the bridge derives
#: it — from the contract, never from the request.
DECLARED = frozenset(CALENDAR_ARGUMENTS)


def test_without_a_declaration_every_host_field_is_dropped() -> None:
    assert strip_host_only_fields({**ARGUMENTS, "timezone": "Asia/Tokyo"}) == ARGUMENTS


def test_a_declared_business_field_survives_the_strip() -> None:
    hostile = {
        **CALENDAR_ARGUMENTS,
        "device_id": "someone-elses-device",
        "user_id": "henson",
    }
    cleaned = strip_host_only_fields(hostile, declared=DECLARED)
    assert cleaned == CALENDAR_ARGUMENTS
    assert cleaned["timezone"] == "Asia/Tokyo"


def test_the_declared_field_is_bound_into_the_hash() -> None:
    # The exemption must not open a hole: a field the model controls has to be
    # covered by the binding, or a tampered timezone would verify against an
    # honest signature.
    forged_host_field = {**CALENDAR_ARGUMENTS, "device_id": "injected"}
    assert arguments_hash(forged_host_field, declared=DECLARED) == arguments_hash(
        CALENDAR_ARGUMENTS, declared=DECLARED
    )
    tampered = {**CALENDAR_ARGUMENTS, "timezone": "Asia/Shanghai"}
    assert arguments_hash(tampered, declared=DECLARED) != arguments_hash(
        CALENDAR_ARGUMENTS, declared=DECLARED
    )


def test_sign_and_verify_agree_on_the_declared_field(ring) -> None:
    token = sign_host_context(
        ring,
        context(tool=CALENDAR_TOOL),
        CALENDAR_ARGUMENTS,
        now=NOW,
        declared=DECLARED,
    )
    claims = verify(
        ring, token, tool=CALENDAR_TOOL, arguments=CALENDAR_ARGUMENTS, declared=DECLARED
    )
    assert claims["arguments_hash"] == arguments_hash(
        CALENDAR_ARGUMENTS, declared=DECLARED
    )


def test_a_tampered_declared_field_fails_verification(ring) -> None:
    token = sign_host_context(
        ring,
        context(tool=CALENDAR_TOOL),
        CALENDAR_ARGUMENTS,
        now=NOW,
        declared=DECLARED,
    )
    with pytest.raises(AppError) as excinfo:
        verify(
            ring,
            token,
            tool=CALENDAR_TOOL,
            arguments={**CALENDAR_ARGUMENTS, "timezone": "Asia/Shanghai"},
            declared=DECLARED,
        )
    assert excinfo.value.code is ErrorCode.HOST_CONTEXT_MISMATCH


# --- key handling -----------------------------------------------------------


def test_finance_mcp_holds_no_signing_key(ring) -> None:
    receiver = ring.public_only()
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    verify(receiver, token)
    with pytest.raises(HostContextError):
        sign_host_context(receiver, context(), ARGUMENTS, now=NOW)


def test_a_token_from_an_unknown_key_is_refused(ring) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    stranger = ServiceKeyRing(
        active=ServiceKey("svc-2099-99", other, other.public_key())
    )
    token = sign_host_context(stranger, context(), ARGUMENTS, now=NOW)
    with pytest.raises(AppError) as excinfo:
        verify(ring, token)
    assert excinfo.value.code is ErrorCode.HOST_CONTEXT_MISMATCH


def test_an_access_token_cannot_be_used_as_a_service_token(ring) -> None:
    # The audiences differ on purpose: an iOS token must never open Finance MCP.
    forged = jwt.encode(
        {
            "iss": "personal-agent-api",
            "aud": "personal-agent-api",
            "sub": "device:x",
            "iat": int(NOW.timestamp()),
            "nbf": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(minutes=5)).timestamp()),
            "jti": REQUEST,
        },
        ring.signing_key(),
        algorithm=ALGORITHM,
        headers={"kid": ring.active_kid},
    )
    with pytest.raises(AppError):
        verify(ring, forged)


def test_alg_none_is_refused(ring) -> None:
    forged = jwt.encode({"tool": TOOL}, key="", algorithm="none")
    with pytest.raises(AppError):
        verify(ring, forged)


def test_rotation_keeps_calls_in_flight_verifiable(ring) -> None:
    before = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    new_private = ec.generate_private_key(ec.SECP256R1())
    rotated = ring.rotated(
        ServiceKey("svc-2026-02", new_private, new_private.public_key())
    )
    assert rotated.active_kid == "svc-2026-02"
    verify(rotated, before, now=NOW + timedelta(seconds=30))
    verify(rotated, sign_host_context(rotated, context(), ARGUMENTS, now=NOW))


# --- lifetime ---------------------------------------------------------------


def test_an_expired_service_token_is_refused(ring) -> None:
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    with pytest.raises(AppError):
        verify(ring, token, now=NOW + timedelta(minutes=2))


def test_a_lifetime_beyond_the_hard_cap_cannot_be_minted(ring) -> None:
    with pytest.raises(HostContextError):
        sign_host_context(
            ring, context(), ARGUMENTS, now=NOW, ttl=timedelta(hours=1)
        )
    assert MAX_TTL == timedelta(minutes=5)


def test_the_service_audience_is_finance_mcp(ring) -> None:
    token = sign_host_context(ring, context(), ARGUMENTS, now=NOW)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == "personal-data-mcp"
    assert claims["sub"] == "service:personal-agent-api"
