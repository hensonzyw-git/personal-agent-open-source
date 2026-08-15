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
    CREATE_RECORD,
    GET_RECORD,
    LIST_FIELDS,
    TENANT_TOKEN,
    UPDATE_RECORD,
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
        if (
            not isinstance(token, str)
            or not token
            or type(expire) is not int
            or expire <= 0
        ):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="tenant token response missing token or expire",
            )
        return token, expire

    async def tenant_token(self) -> str:
        return await self._token_cache.get()

    # --- read: list a table's fields, following pagination -------------------

    async def list_fields(
        self, app_token: str, table_id: str, *, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Every field of one table, following `page_token` to exhaustion.

        A read must never call the first page "all of it" (engineering rule 5).
        The loop stops only when Feishu reports `has_more=false`, and a bounded
        page count guards against a provider that never sets it.
        """
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        for _ in range(1000):  # a table cannot plausibly have this many fields
            query = {"page_size": str(page_size)}
            if page_token:
                query["page_token"] = page_token
            data = await self.request(
                LIST_FIELDS,
                params={"app_token": app_token, "table_id": table_id},
                query=query,
            )
            page_items = data.get("items")
            if page_items is None:
                page_items = []
            if not isinstance(page_items, list) or not all(
                isinstance(item, dict) for item in page_items
            ):
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail="list_fields returned malformed items",
                )
            items.extend(page_items)
            has_more = data.get("has_more", False)
            if not isinstance(has_more, bool):
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail="list_fields returned malformed has_more",
                )
            if not has_more:
                return items
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail=(
                        "list_fields reported more pages without a page_token"
                    ),
                )
            if page_token in seen_page_tokens:
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    internal_detail="list_fields repeated a pagination cursor",
                )
            seen_page_tokens.add(page_token)
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="list_fields did not terminate its pagination",
        )

    # --- write: create one record, and read it back --------------------------

    async def create_record(
        self,
        app_token: str,
        table_id: str,
        *,
        fields: dict[str, Any],
        client_token: str,
    ) -> dict[str, Any]:
        """Create one record, returning the created record object.

        `client_token` is Feishu's own idempotency token and comes from the
        execution row, persisted before the first submit: a replay after a lost
        response carries the same token and must not produce a second record.
        `ignore_consistency_check=false` is sent explicitly rather than left to
        the provider default (design 9.4.2).

        This method never retries. A lost response is an *unknown* commit, and
        deciding what to do about one belongs to the execution state machine,
        which is the only thing that knows whether a record id was persisted.
        """
        data = await self.request(
            CREATE_RECORD,
            params={"app_token": app_token, "table_id": table_id},
            json={"fields": fields},
            query={
                "client_token": client_token,
                "ignore_consistency_check": "false",
            },
        )
        return self._record_of(data, "create_record")

    async def update_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        *,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Change the named fields of one existing record.

        Bitable's update is partial: fields absent from the body are untouched.
        The caller therefore sends exactly the one field it means to change, and
        every other value in the row -- 名称, 金额, 日期, 是否家庭支出 -- is not
        merely preserved but *unaddressable* by the request. That is the point:
        a correction to 分类 must not be able to rewrite the amount even if a
        caller is wrong about what the row currently holds.

        There is deliberately no `client_token`. Bitable offers idempotency for
        creates, not updates, so a lost response cannot be resolved by replaying
        this call and hoping. The update path handles that the only way that is
        actually sound: it re-reads the record and compares. Sending a token
        here would look like protection and provide none.

        Like `create_record`, this never retries. A lost response is an unknown
        outcome, and only the execution state machine may decide what an unknown
        outcome means.
        """
        data = await self.request(
            UPDATE_RECORD,
            params={
                "app_token": app_token,
                "table_id": table_id,
                "record_id": record_id,
            },
            json={"fields": fields},
        )
        return self._record_of(data, "update_record")

    async def get_record(
        self, app_token: str, table_id: str, record_id: str
    ) -> dict[str, Any]:
        """Read one record back by id, for post-write verification."""
        data = await self.request(
            GET_RECORD,
            params={
                "app_token": app_token,
                "table_id": table_id,
                "record_id": record_id,
            },
        )
        return self._record_of(data, "get_record")

    @staticmethod
    def _record_of(data: dict[str, Any], operation: str) -> dict[str, Any]:
        """Pull the `record` object out of a Bitable response, or fail.

        A missing or malformed record is never treated as an empty success: for
        a create, "no record id" is precisely the unknown-commit case, so it
        must raise rather than return something falsy that a caller might read
        as a clean failure.
        """
        record = data.get("record")
        if not isinstance(record, dict):
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail=f"{operation} returned no record object",
            )
        return record

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
