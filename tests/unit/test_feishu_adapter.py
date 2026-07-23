"""DEV-017 (offline): the Feishu adapter, allowlist, token cache, limits.

Everything here runs against an httpx MockTransport with a placeholder app id
and secret. No network, no real Base, no credential file. Pointing the adapter
at the real test Base is the G2 step and is deliberately not done here.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.credentials import (
    FeishuCredentials,
    MissingCredentialError,
    load_credentials,
)
from personal_data_mcp.feishu.endpoints import (
    CREATE_RECORD,
    GET_RECORD,
    LIST_FIELDS,
    EndpointNotAllowed,
    OperationClass,
    build_path,
    full_url,
)
from personal_data_mcp.feishu.rate_limit import TokenBucket
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.feishu.token_cache import TenantTokenCache


PLACEHOLDER = FeishuCredentials(app_id="cli_placeholder", app_secret="secret_xyz")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def run(coro):
    return asyncio.run(coro)


# --- endpoint allowlist ------------------------------------------------------


def test_only_clean_single_segment_params_fill_a_template() -> None:
    good = build_path(GET_RECORD, {"app_token": "bascn1", "table_id": "tbl1", "record_id": "rec1"})
    assert good.endswith("/records/rec1")


@pytest.mark.parametrize(
    "params",
    [
        {"app_token": "bascn1", "table_id": "tbl1"},  # missing record_id
        {"app_token": "a/b", "table_id": "tbl1", "record_id": "rec1"},  # slash
        {"app_token": "bascn1", "table_id": "tbl1", "record_id": "rec1", "x": "y"},  # extra
        {"app_token": "../etc", "table_id": "tbl1", "record_id": "rec1"},  # traversal
    ],
)
def test_unsafe_or_wrong_params_are_refused(params) -> None:
    with pytest.raises(EndpointNotAllowed):
        build_path(GET_RECORD, params)


def test_the_full_url_is_pinned_to_the_feishu_host() -> None:
    url = full_url(LIST_FIELDS, {"app_token": "bascn1", "table_id": "tbl1"})
    assert url.startswith("https://open.feishu.cn/open-apis/bitable/")


# --- credentials -------------------------------------------------------------


def test_missing_credentials_are_an_error() -> None:
    with pytest.raises(MissingCredentialError):
        load_credentials(env={})


def test_credentials_never_appear_in_repr() -> None:
    creds = load_credentials(
        env={
            "FEISHU_FINANCE_APP_ID": "cli_x",
            "FEISHU_FINANCE_APP_SECRET": "topsecret",
        }
    )
    assert "topsecret" not in repr(creds)
    assert "cli_x" not in repr(creds)


# --- redaction ---------------------------------------------------------------


def test_redaction_masks_secrets_and_resource_ids() -> None:
    line = (
        "Authorization: Bearer t-abc123.def "
        'app_secret="hunter2" base bascnABCDEF12 table tblXYZ98765 '
        "field fldQQQ12345 record recPPP54321"
    )
    out = redact_for_log(line)
    for leaked in ["t-abc123.def", "hunter2", "bascnABCDEF12", "tblXYZ98765", "fldQQQ12345", "recPPP54321"]:
        assert leaked not in out


# --- token bucket ------------------------------------------------------------


def test_token_bucket_grants_then_makes_the_next_caller_wait() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=2.0, now=clock)
    assert bucket.acquire() == 0.0  # 2 -> 1, free
    assert bucket.acquire() == 0.0  # 1 -> 0, free
    wait = bucket.acquire()  # 0 -> -1, must wait ~1s at 1 tok/s
    assert wait == pytest.approx(1.0)
    clock.advance(2.0)  # refills +2 -> back above one token
    assert bucket.acquire() == 0.0


# --- token cache -------------------------------------------------------------


def test_the_token_is_cached_and_refreshed_before_expiry() -> None:
    clock = FakeClock()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return f"token-{calls['n']}", 7200

    cache = TenantTokenCache(fetch, now=clock, margin_seconds=300)

    async def scenario():
        a = await cache.get()
        b = await cache.get()  # still fresh, no new fetch
        clock.advance(7200 - 299)  # inside the 300s margin
        c = await cache.get()  # refreshes
        return a, b, c

    a, b, c = run(scenario())
    assert a == b == "token-1"
    assert c == "token-2"
    assert calls["n"] == 2


# --- adapter over a mock transport ------------------------------------------


def mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def token_and_then(data_handler):
    """A handler that answers the token mint, then delegates data calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "tenant_access_token": "t-1", "expire": 7200},
            )
        return data_handler(request)

    return handler


def test_a_data_call_carries_the_bearer_token(monkeypatch) -> None:
    clock = FakeClock()
    seen = {}

    def data_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"items": []}})

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=mock_transport(token_and_then(data_handler)),
            now=clock,
        ) as adapter:
            return await adapter.request(
                LIST_FIELDS, params={"app_token": "bascn1", "table_id": "tbl1"}
            )

    data = run(scenario())
    assert data == {"items": []}
    assert seen["auth"] == "Bearer t-1"


def test_the_token_mint_does_not_carry_a_bearer() -> None:
    clock = FakeClock()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            seen["token_auth"] = request.headers.get("authorization")
            body = json.loads(request.content)
            seen["sent_secret"] = body.get("app_secret")
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "tenant_access_token": "t-1", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER, transport=mock_transport(handler), now=clock
        ) as adapter:
            await adapter.tenant_token()

    run(scenario())
    assert seen["token_auth"] is None  # secret mints the token; no bearer yet
    assert seen["sent_secret"] == "secret_xyz"


def test_a_non_zero_feishu_code_becomes_a_stable_error_without_provider_text() -> None:
    clock = FakeClock()

    def data_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 91402, "msg": "NOTEXIST: base bascnLEAK123456 is gone", "data": {}},
        )

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=mock_transport(token_and_then(data_handler)),
            now=clock,
        ) as adapter:
            return await adapter.request(
                LIST_FIELDS, params={"app_token": "bascn1", "table_id": "tbl1"}
            )

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code == ErrorCode.SOURCE_UNAVAILABLE
    # The provider's message, which named a Base token, is not in the detail.
    assert "bascnLEAK123456" not in (caught.value.internal_detail or "")


def test_a_write_timeout_is_unknown_not_failed() -> None:
    clock = FakeClock()

    def data_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=mock_transport(token_and_then(data_handler)),
            now=clock,
        ) as adapter:
            return await adapter.request(
                CREATE_RECORD,
                params={"app_token": "bascn1", "table_id": "tbl1"},
                json={"fields": {}},
            )

    with pytest.raises(AppError) as caught:
        run(scenario())
    # A write that timed out may have committed: it is unknown, not unavailable.
    assert caught.value.code == ErrorCode.SOURCE_TIMEOUT_UNKNOWN


def test_a_read_timeout_is_cleanly_unavailable() -> None:
    clock = FakeClock()

    def data_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=mock_transport(token_and_then(data_handler)),
            now=clock,
        ) as adapter:
            return await adapter.request(
                GET_RECORD,
                params={"app_token": "bascn1", "table_id": "tbl1", "record_id": "rec1"},
            )

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code == ErrorCode.SOURCE_UNAVAILABLE
