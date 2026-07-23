"""The Feishu HTTP adapter: allowlisted, budgeted, authenticated, redacted.

This is the only place the connector reaches Feishu. It enforces four things so
the layers above it cannot get them wrong:

- **allowlist.** Only an endpoint from `endpoints.ALLOWLIST` can be called, and
  its path is built from clean single-segment parameters. There is no method
  that takes a url.
- **budget.** Timeouts come from the operation class (design 6.3): a token or
  read failure is safely unavailable, but a *write* timeout is unknown, not
  failed, because the record may have been created. The adapter never
  auto-retries a write; that decision belongs to the execution state machine.
- **auth.** Every call except the token mint carries the cached tenant token.
  The credentials are used only to mint the token and never travel on a data
  call.
- **redaction.** Nothing here logs a body or a token, and the one diagnostic
  string it raises internally is passed through `redact_for_log`.

It is credential-free to test: a mock transport and a placeholder app id/secret
exercise every path without a network or a real Base. Pointing it at the real
test Base is the G2 step and needs the confirmed external inputs, not new code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

import httpx

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.feishu.endpoints import (
    ALLOWLIST,
    TENANT_TOKEN,
    Endpoint,
    EndpointNotAllowed,
    OperationClass,
    full_url,
)
from personal_data_mcp.feishu.rate_limit import TokenBucket
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.feishu.token_cache import TenantTokenCache


#: Per-operation timeouts, seconds (design 6.3).
_TIMEOUTS: Final[dict[OperationClass, float]] = {
    OperationClass.TOKEN: 5.0,
    OperationClass.READ: 8.0,
    OperationClass.WRITE: 10.0,
}

#: A write timeout is unknown (the record may exist); a read or token timeout is
#: cleanly unavailable.
_TIMEOUT_CODE: Final[dict[OperationClass, ErrorCode]] = {
    OperationClass.TOKEN: ErrorCode.SOURCE_UNAVAILABLE,
    OperationClass.READ: ErrorCode.SOURCE_UNAVAILABLE,
    OperationClass.WRITE: ErrorCode.SOURCE_TIMEOUT_UNKNOWN,
}


class FeishuAdapter:
    def __init__(
        self,
        credentials: FeishuCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float],
        rate_limiter: TokenBucket | None = None,
        token_cache: TenantTokenCache | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self._credentials = credentials
        self._client = httpx.AsyncClient(transport=transport, trust_env=False)
        self._now = now
        self._sleep = sleep
        self._limiter = rate_limiter or TokenBucket(
            rate=8.0, capacity=8.0, now=now
        )
        self._token_cache = token_cache or TenantTokenCache(
            self._fetch_tenant_token, now=now
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FeishuAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- the tenant token -----------------------------------------------------

    async def _fetch_tenant_token(self) -> tuple[str, int]:
        """Mint a fresh tenant token. The only call that sends the secret."""
        envelope = await self._raw_call(
            TENANT_TOKEN,
            params={},
            json={
                "app_id": self._credentials.app_id,
                "app_secret": self._credentials.app_secret,
            },
            authenticated=False,
        )
        token = envelope.get("tenant_access_token")
        expire = envelope.get("expire")
        if not isinstance(token, str) or not isinstance(expire, int):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="tenant token response missing token or expire",
            )
        return token, expire

    async def tenant_token(self) -> str:
        return await self._token_cache.get()

    # --- the one request path -------------------------------------------------

    async def request(
        self,
        endpoint: Endpoint,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call one allowlisted endpoint, returning the Feishu `data` payload."""
        if endpoint not in ALLOWLIST:
            raise EndpointNotAllowed(f"{endpoint.name} is not allowlisted")
        return await self._raw_call(
            endpoint, params=params or {}, json=json, query=query, authenticated=True
        )

    async def _raw_call(
        self,
        endpoint: Endpoint,
        *,
        params: dict[str, str],
        json: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        authenticated: bool,
    ) -> dict[str, Any]:
        url = full_url(endpoint, params)

        wait = self._limiter.acquire()
        if wait > 0:
            await self._sleep(wait)

        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = f"Bearer {await self._token_cache.get()}"

        timeout = _TIMEOUTS[endpoint.operation]
        try:
            response = await self._client.request(
                endpoint.method,
                url,
                json=json,
                params=query,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise AppError(
                _TIMEOUT_CODE[endpoint.operation],
                internal_detail=redact_for_log(f"{endpoint.name} timed out"),
            ) from exc
        except httpx.HTTPError as exc:
            code = (
                ErrorCode.SOURCE_TIMEOUT_UNKNOWN
                if endpoint.operation is OperationClass.WRITE
                else ErrorCode.SOURCE_UNAVAILABLE
            )
            raise AppError(
                code,
                internal_detail=redact_for_log(
                    f"{endpoint.name} transport error: {type(exc).__name__}"
                ),
            ) from exc

        return self._parse_envelope(endpoint, response)

    def _parse_envelope(
        self, endpoint: Endpoint, response: httpx.Response
    ) -> dict[str, Any]:
        """Return `data` on success, or raise a stable code on any failure.

        The precise Feishu-code-to-stable-code map is refined where the write and
        read paths are exercised (DEV-018+). Here every non-zero code is a
        conservative `SOURCE_UNAVAILABLE`, and no provider message text is ever
        carried outward.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail=f"{endpoint.name} returned non-JSON",
            ) from exc

        code = body.get("code")
        if code != 0:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail=redact_for_log(
                    f"{endpoint.name} feishu code {code}"
                ),
            )
        # The tenant-token endpoint carries its result at the top level, not
        # under `data`; every Bitable endpoint nests it under `data`.
        if endpoint.operation is OperationClass.TOKEN:
            return body
        data = body.get("data")
        return data if isinstance(data, dict) else {}
