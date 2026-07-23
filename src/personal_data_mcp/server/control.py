"""The internal control API: two authenticated, read-only endpoints.

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

from collections.abc import Callable
from typing import Final

from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from personal_agent_core.control_token import (
    ControlAction,
    verify_control_token,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import ServiceKeyRing
from personal_agent_core.timeutil import parse_ledger_date
from personal_data_mcp.server.control_queries import (
    get_execution_status,
    successful_writes_on,
)


CONTROL_PREFIX: Final[str] = "/internal/v1"

SessionFactory = Callable[[], Session]

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
) -> Starlette:
    """The control ASGI app. Verification keys only; never signs."""
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

    return Starlette(
        routes=[
            Route(
                f"{CONTROL_PREFIX}/executions/{{idempotency_key}}",
                get_execution,
                methods=["GET"],
            ),
            Route(
                f"{CONTROL_PREFIX}/successful-writes",
                list_successful_writes,
                methods=["GET"],
            ),
        ]
    )
