"""The Agent's HTTP client for the Finance MCP internal control plane.

Four reads live here, and none is a tool call: "what state is the execution for
this idempotency key in?" (crash recovery, design 7.6.1), "which duplicate check
is blocking this request?" (the `write anyway` flow, design 5.2), and the two the
daily review needs (design 7.7) -- "what was successfully written on this ledger
day?" and "what does the ledger hold for this record now?".

The second one is why this module exists at all. `finance.log_expense` reports a
duplicate as a bare `POSSIBLE_DUPLICATE` error, because an MCP result is the
*model-facing* channel and the model must never see or forge a
`duplicate_check_id`. The Host-to-Host channel is this one: a separate path, a
separate token audience, and a token that names the exact resource being read.

Three things are pinned rather than configured per call:

- **the base URL must be loopback.** Finance MCP binds to loopback in-process;
  a control base URL pointing anywhere else would send a bearer token off the
  host, so it is refused at construction rather than at request time;
- **the token is minted per read**, for one action and one resource, with the
  short default control TTL. Nothing here caches or reuses a token;
- **a non-200, a non-JSON body or an unexpected shape is a failure**, never an
  empty answer. `not_found` is a distinct, explicit branch the server states;
  it is not inferred from a response that could not be understood.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from personal_agent_core.control_token import (
    ControlAction,
    record_batch_resource,
    sign_control_token,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import ServiceKeyRing


CONTROL_PREFIX: Final[str] = "/internal/v1"

#: The control plane is a loopback-only sibling of the MCP endpoint.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset(
    {"127.0.0.1", "localhost", "::1", "[::1]"}
)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0


class ControlPlaneError(RuntimeError):
    """The control plane could not be read.

    Deliberately not an `AppError`: a caller decides what an unreadable control
    plane means for *its* operation. For a duplicate lookup it means the write
    was refused and no decision can be offered; for recovery it means try again
    later. Neither may be reported as a successful read of "nothing".
    """


def require_loopback_url(base_url: str) -> str:
    """Refuse any URL that would send an internal token off this host.

    Shared with the composition root, which applies the identical rule to the
    Finance MCP endpoint: one statement of the rule, so the two channels cannot
    drift apart.
    """
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname is None:
        raise ValueError("the control base URL must be an http:// loopback URL")
    if parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError(
            "refusing a control base URL that is not loopback: a control token "
            "must never leave this host"
        )
    return base_url.rstrip("/")


@dataclass(frozen=True)
class PendingDuplicateCheck:
    duplicate_check_id: str
    expires_at: str


@dataclass(frozen=True)
class SuccessfulWrite:
    """One verified write, as the review job sees it: a pointer, not values."""

    tool: str
    table_kind: str
    record_id: str
    committed_at: str


@dataclass(frozen=True)
class RecordFields:
    """A record's current values, read at the moment the card was opened."""

    table_kind: str
    record_id: str
    values: dict[str, Any]
    #: Configured fields whose cell Finance could not parse. Kept rather than
    #: dropped: a card that silently omits a field it failed to read looks the
    #: same as a card for a record that never had one.
    unreadable_fields: tuple[str, ...]


@dataclass(frozen=True)
class RecordUnavailable:
    """The receipt exists, but its current source values could not be read."""

    table_kind: str
    record_id: str


class FinanceControlClient:
    """Read-only access to the Finance MCP control plane."""

    def __init__(
        self,
        *,
        base_url: str,
        signing_ring: ServiceKeyRing,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = require_loopback_url(base_url)
        self._ring = signing_ring
        # An injected client belongs to the caller's event loop; tests supply
        # one. Production injects nothing, because this client is constructed
        # once at composition and then used from operation worker threads, each
        # driving its own loop. An `httpx.AsyncClient` binds its pool to the loop
        # that first uses it, so a shared instance would be a cross-loop bug that
        # no in-loop test can see. One short-lived client per read instead: the
        # control plane is loopback and read rarely.
        self._client = client
        self._timeout = timeout_seconds

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _get(
        self, path: str, *, action: ControlAction, resource: str
    ) -> dict[str, Any]:
        token = sign_control_token(self._ring, action=action, resource=resource)
        try:
            if self._client is not None:
                response = await self._client.get(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(
                        f"{self._base_url}{path}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=self._timeout,
                    )
        except httpx.HTTPError as exc:
            raise ControlPlaneError(
                f"control read {action} failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            # The body may carry a stable error code, but it is not this
            # client's job to re-raise the server's business error: any non-200
            # means the read did not happen.
            raise ControlPlaneError(
                f"control read {action} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ControlPlaneError(
                f"control read {action} returned a non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise ControlPlaneError(
                f"control read {action} returned a non-object body"
            )
        return payload

    async def _post(
        self,
        path: str,
        *,
        action: ControlAction,
        resource: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        token = sign_control_token(self._ring, action=action, resource=resource)
        try:
            if self._client is not None:
                response = await self._client.post(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(
                        f"{self._base_url}{path}",
                        headers={"Authorization": f"Bearer {token}"},
                        json=body,
                        timeout=self._timeout,
                    )
        except httpx.HTTPError as exc:
            raise ControlPlaneError(
                f"control read {action} failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise ControlPlaneError(
                f"control read {action} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ControlPlaneError(
                f"control read {action} returned a non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise ControlPlaneError(
                f"control read {action} returned a non-object body"
            )
        return payload

    async def get_execution(self, idempotency_key: str) -> dict[str, Any] | None:
        """The Finance execution for this key, or None when it never existed."""
        payload = await self._get(
            f"{CONTROL_PREFIX}/executions/{idempotency_key}",
            action=ControlAction.GET_EXECUTION,
            resource=idempotency_key,
        )
        status = payload.get("status")
        if status == "not_found":
            return None
        execution = payload.get("execution")
        if status != "found" or not isinstance(execution, dict):
            raise ControlPlaneError("execution status body was not understood")
        return execution

    async def get_pending_duplicate_check(
        self, idempotency_key: str
    ) -> PendingDuplicateCheck | None:
        """The undecided duplicate check for this request, or None."""
        payload = await self._get(
            f"{CONTROL_PREFIX}/duplicate-checks/{idempotency_key}",
            action=ControlAction.GET_PENDING_DUPLICATE_CHECK,
            resource=idempotency_key,
        )
        status = payload.get("status")
        if status == "not_found":
            return None
        check = payload.get("duplicate_check")
        if status != "found" or not isinstance(check, dict):
            raise ControlPlaneError("duplicate check body was not understood")
        check_id = check.get("duplicate_check_id")
        expires_at = check.get("expires_at")
        if not isinstance(check_id, str) or not check_id:
            raise ControlPlaneError("duplicate check body carried no id")
        if not isinstance(expires_at, str) or not expires_at:
            raise ControlPlaneError("duplicate check body carried no expiry")
        return PendingDuplicateCheck(
            duplicate_check_id=check_id, expires_at=expires_at
        )

    async def list_successful_writes(self, write_date: str) -> list[SuccessfulWrite]:
        """The verified writes committed on one Asia/Shanghai ledger day.

        An empty list is a real answer -- "nothing was written that day" is
        precisely the case where design 7.7 says to create no review and send no
        push. Anything that cannot be understood raises instead, so a broken
        control plane can never be mistaken for a quiet day.
        """
        payload = await self._get(
            f"{CONTROL_PREFIX}/successful-writes?write_date={write_date}",
            action=ControlAction.LIST_SUCCESSFUL_WRITES,
            resource=write_date,
        )
        writes = payload.get("writes")
        if not isinstance(writes, list):
            raise ControlPlaneError("successful-writes body carried no list")
        if payload.get("write_date") != write_date:
            raise ControlPlaneError(
                "successful-writes body answered for a different day"
            )
        return [_successful_write(item) for item in writes]

    async def get_record_fields(
        self, *, table_kind: str, record_id: str
    ) -> RecordFields | None:
        """The record's current values, or None when Finance has no receipt."""
        payload = await self._get(
            f"{CONTROL_PREFIX}/records/{table_kind}/{record_id}",
            action=ControlAction.GET_RECORD_FIELDS,
            resource=f"{table_kind}:{record_id}",
        )
        status = payload.get("status")
        if status == "not_found":
            return None
        record = payload.get("record")
        if status != "found" or not isinstance(record, dict):
            raise ControlPlaneError("record body was not understood")
        return _record_fields(
            record, table_kind=table_kind, record_id=record_id
        )

    async def get_record_fields_batch(
        self, records: list[tuple[str, str]]
    ) -> list[RecordFields | RecordUnavailable | None]:
        """Read one card's current values with one control request."""
        body = {
            "records": [
                {"table_kind": table_kind, "record_id": record_id}
                for table_kind, record_id in records
            ]
        }
        payload = await self._post(
            f"{CONTROL_PREFIX}/records:batch",
            action=ControlAction.GET_RECORD_FIELDS_BATCH,
            resource=record_batch_resource(records),
            body=body,
        )
        raw_results = payload.get("records")
        if not isinstance(raw_results, list) or len(raw_results) != len(records):
            raise ControlPlaneError("record batch returned the wrong result count")

        parsed: list[RecordFields | RecordUnavailable | None] = []
        for raw, (table_kind, record_id) in zip(
            raw_results, records, strict=True
        ):
            if not isinstance(raw, dict):
                raise ControlPlaneError("record batch entry was not an object")
            if (
                raw.get("table_kind", table_kind) != table_kind
                or raw.get("record_id", record_id) != record_id
            ):
                raise ControlPlaneError("record batch entry changed its pointer")
            status = raw.get("status")
            if status == "not_found":
                parsed.append(None)
            elif status == "unavailable":
                parsed.append(RecordUnavailable(table_kind, record_id))
            elif status == "found" and isinstance(raw.get("record"), dict):
                parsed.append(
                    _record_fields(
                        raw["record"],
                        table_kind=table_kind,
                        record_id=record_id,
                    )
                )
            else:
                raise ControlPlaneError("record batch entry was not understood")
        return parsed


def _successful_write(item: Any) -> SuccessfulWrite:
    if not isinstance(item, dict):
        raise ControlPlaneError("a successful-writes entry was not an object")
    fields = {}
    for key in ("tool", "table_kind", "record_id", "committed_at"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise ControlPlaneError(
                f"a successful-writes entry carried no {key}"
            )
        fields[key] = value
    return SuccessfulWrite(**fields)


def _record_fields(
    record: dict[str, Any], *, table_kind: str, record_id: str
) -> RecordFields:
    values = record.get("values")
    unreadable = record.get("unreadable_fields")
    if not isinstance(values, dict) or not isinstance(unreadable, list):
        raise ControlPlaneError("record body carried no projected values")
    if (
        record.get("table_kind") != table_kind
        or record.get("record_id") != record_id
    ):
        raise ControlPlaneError("record body described a different record")
    if not all(isinstance(name, str) for name in unreadable):
        raise ControlPlaneError("record body carried non-string unreadable fields")
    return RecordFields(
        table_kind=table_kind,
        record_id=record_id,
        values=values,
        unreadable_fields=tuple(unreadable),
    )


def unavailable() -> AppError:
    """The stable code a caller may surface when the control plane is down."""
    return AppError(
        ErrorCode.SOURCE_UNAVAILABLE,
        internal_detail="the Finance control plane could not be read",
    )
