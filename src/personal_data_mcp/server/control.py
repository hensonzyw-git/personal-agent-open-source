"""The internal control API: authenticated, read-only endpoints.

It is deliberately not an MCP tool and not on the `/mcp` path. Technical design
7.6.1 is explicit that the execution-status endpoint "is not registered as an
MCP tool", and the same holds for the review query: the model must never be able
to reach execution state or record ids through its tool surface. Keeping the
control plane a separate ASGI app on a separate path is what makes that
structural rather than a naming convention.

Authentication is the control token (`personal_agent_core.control_token`), whose
audience and bound resource keep it from being interchangeable with a tool-call
Host Context. The token names the exact read, so the endpoint verifies against
the path parameter it actually received.

Every failure leaves as a small JSON body with a stable code; no provider text,
no execution payload and no exception string reaches the wire.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from personal_agent_core.control_token import (
    MAX_RECORD_BATCH,
    ControlAction,
    calendar_lookup_resource,
    record_batch_resource,
    verify_control_token,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import ServiceKeyRing
from personal_agent_core.timeutil import parse_ledger_date, utc_now
from personal_data_mcp.server.control_queries import (
    get_execution_status,
    get_pending_duplicate_check,
    resolve_calendar_target,
    successful_writes_on,
    verified_receipt_for,
)


CONTROL_PREFIX: Final[str] = "/internal/v1"

SessionFactory = Callable[[], Session]

#: `(table_kind, record_id) -> the record's current values`. Supplied only by
#: Finance composition, because reading Feishu needs the credential, the bound
#: source and the protected config -- none of which a control route may load.
RecordReader = Callable[
    [list[tuple[str, str]]], Awaitable[list[dict[str, Any]]]
]

_BEARER_PREFIX = "Bearer "


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    if not raw.startswith(_BEARER_PREFIX):
        raise AppError(
            ErrorCode.HOST_CONTEXT_MISMATCH,
            internal_detail="missing or malformed control Authorization header",
        )
    token = raw[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise AppError(
            ErrorCode.HOST_CONTEXT_MISMATCH, internal_detail="empty control token"
        )
    return token


def _error_response(error: AppError) -> JSONResponse:
    # HOST_CONTEXT_MISMATCH and SCOPE_DENIED are auth failures -> 403; a bad
    # date is a client error -> 400; anything else is 500. The body is the same
    # stable envelope the tool surface uses.
    status = {
        ErrorCode.HOST_CONTEXT_MISMATCH: 403,
        ErrorCode.SCOPE_DENIED: 403,
        ErrorCode.INVALID_ARGUMENT: 400,
    }.get(error.code, 500)
    envelope = error.to_envelope().model_dump(mode="json")
    return JSONResponse({"error": envelope}, status_code=status)


def build_control_app(
    *,
    verification_ring: ServiceKeyRing,
    session_factory: SessionFactory,
    record_reader: RecordReader | None = None,
    data_keyring: KeyRing | None = None,
) -> Starlette:
    """The control ASGI app. The service ring verifies and never signs.

    Without a `record_reader` the current-value route is still mounted but
    refuses: a service started without a ledger config has no credential and no
    protected config, and answering "not found" there would let a caller read a
    missing capability as a missing record. `data_keyring` decrypts only the
    pending duplicate's display projection; it is never used for auth and its
    plaintext never reaches the MCP/model channel.
    """
    ring = verification_ring.public_only()

    async def get_execution(request: Request) -> JSONResponse:
        key = request.path_params["idempotency_key"]
        try:
            token = _bearer(request)
            verify_control_token(
                ring,
                token,
                action=ControlAction.GET_EXECUTION,
                resource=key,
            )
        except AppError as error:
            return _error_response(error)

        with session_factory() as session:
            status = get_execution_status(session, key)
        if status is None:
            # A distinct, non-error shape: recovery treats "not_found" as a
            # branch, not a failure.
            return JSONResponse({"status": "not_found"})
        return JSONResponse({"status": "found", "execution": status})

    async def get_duplicate_check(request: Request) -> JSONResponse:
        key = request.path_params["idempotency_key"]
        try:
            token = _bearer(request)
            verify_control_token(
                ring,
                token,
                action=ControlAction.GET_PENDING_DUPLICATE_CHECK,
                resource=key,
            )
        except AppError as error:
            return _error_response(error)

        try:
            with session_factory() as session:
                pending = get_pending_duplicate_check(
                    session, key, now=utc_now(), keyring=data_keyring
                )
        except AppError as error:
            return _error_response(error)
        if pending is None:
            # "Nothing is pending" is a branch, not a failure: a write can be
            # refused for reasons that are not a duplicate at all.
            return JSONResponse({"status": "not_found"})
        return JSONResponse({"status": "found", "duplicate_check": pending})

    async def list_successful_writes(request: Request) -> JSONResponse:
        raw_date = request.query_params.get("write_date", "")
        try:
            token = _bearer(request)
            verify_control_token(
                ring,
                token,
                action=ControlAction.LIST_SUCCESSFUL_WRITES,
                resource=raw_date,
            )
            try:
                day = parse_ledger_date(raw_date)
            except ValueError as exc:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail="write_date must be YYYY-MM-DD",
                ) from exc
        except AppError as error:
            return _error_response(error)

        with session_factory() as session:
            writes = successful_writes_on(session, day)
        return JSONResponse({"write_date": raw_date, "writes": writes})

    async def get_record_fields_batch(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail="record batch body must be JSON",
                )
            )
        try:
            pointers = _record_pointers(body)
            token = _bearer(request)
            verify_control_token(
                ring,
                token,
                action=ControlAction.GET_RECORD_FIELDS_BATCH,
                resource=record_batch_resource(pointers),
            )
        except AppError as error:
            return _error_response(error)

        permitted: list[tuple[str, str]] = []
        permitted_indexes: list[int] = []
        response_items: list[dict[str, Any] | None] = [None] * len(pointers)
        with session_factory() as session:
            for index, (table_kind, record_id) in enumerate(pointers):
                receipt = verified_receipt_for(
                    session, table_kind=table_kind, record_id=record_id
                )
                if receipt is None:
                    response_items[index] = {
                        "status": "not_found",
                        "table_kind": table_kind,
                        "record_id": record_id,
                    }
                else:
                    permitted.append((table_kind, record_id))
                    permitted_indexes.append(index)

        if permitted:
            if record_reader is None:
                read_results = [
                    {
                        "status": "unavailable",
                        "table_kind": table_kind,
                        "record_id": record_id,
                    }
                    for table_kind, record_id in permitted
                ]
            else:
                try:
                    read_results = await record_reader(permitted)
                except AppError as error:
                    return _error_response(error)
                if len(read_results) != len(permitted):
                    return _error_response(
                        AppError(
                            ErrorCode.SOURCE_UNAVAILABLE,
                            internal_detail=(
                                "record reader returned the wrong result count"
                            ),
                        )
                    )
            for index, result in zip(permitted_indexes, read_results, strict=True):
                response_items[index] = result

        return JSONResponse({"records": response_items})

    async def resolve_calendar(request: Request) -> JSONResponse:
        device_id = request.path_params["device_id"]
        title = request.query_params.get("title", "")
        try:
            token = _bearer(request)
            verify_control_token(
                ring,
                token,
                action=ControlAction.RESOLVE_CALENDAR,
                resource=calendar_lookup_resource(device_id, title),
            )
        except AppError as error:
            return _error_response(error)

        with session_factory() as session:
            resolution = resolve_calendar_target(
                session, device_id=device_id, title=title
            )
        # Every outcome is a `status`, including the misses: "no such calendar"
        # is the answer to the question asked, not a failure of the read. Only
        # an unreadable plane is an error, and that leaves via `_error_response`.
        return JSONResponse(resolution)

    return Starlette(
        routes=[
            Route(
                f"{CONTROL_PREFIX}/executions/{{idempotency_key}}",
                get_execution,
                methods=["GET"],
            ),
            Route(
                f"{CONTROL_PREFIX}/calendars/{{device_id}}/resolve",
                resolve_calendar,
                methods=["GET"],
            ),
            Route(
                f"{CONTROL_PREFIX}/duplicate-checks/{{idempotency_key}}",
                get_duplicate_check,
                methods=["GET"],
            ),
            Route(
                f"{CONTROL_PREFIX}/successful-writes",
                list_successful_writes,
                methods=["GET"],
            ),
            Route(
                f"{CONTROL_PREFIX}/records:batch",
                get_record_fields_batch,
                methods=["POST"],
            ),
        ]
    )


def _record_pointers(body: Any) -> list[tuple[str, str]]:
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="record batch must carry a records list",
        )
    raw = body["records"]
    if not raw or len(raw) > MAX_RECORD_BATCH:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"record batch size must be 1..{MAX_RECORD_BATCH}",
        )
    pointers: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="record pointer must be an object",
            )
        table_kind = item.get("table_kind")
        record_id = item.get("record_id")
        if (
            not isinstance(table_kind, str)
            or not table_kind
            or not isinstance(record_id, str)
            or not record_id
        ):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="record pointer fields must be non-empty strings",
            )
        pointers.append((table_kind, record_id))
    return pointers
