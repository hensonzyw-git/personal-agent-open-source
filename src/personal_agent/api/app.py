"""The Agent Client API (FastAPI), per technical design 5.

This is the only public surface. It is built from injected collaborators -- a
session factory, the access-token ring, the Agent key ring, the model
interpreter, the Finance dispatcher, a per-device authorizer and a capability
provider -- so the whole HTTP contract is exercised offline with fakes, and the
real model and fact source are wired only at composition time.

Four invariants live here rather than in a handler's good intentions:

- **Every request is authenticated to an active device.** A token proves minting
  and freshness; the device's current status is read separately, so a revoked
  device is refused even with a still-valid token.
- **One operation per `Idempotency-Key`.** The chat and decision endpoints anchor
  a request before any work; a replay returns the same operation, and the same
  key with a different body is a `409`.
- **Cancellation never lies.** `DELETE` records the intent and only reports a
  clean cancellation when the operation is provably pre-submit.
- **The model cannot authorise a duplicate override.** The decision endpoint is
  the only path that mints one, and only on the user's explicit `write_anyway`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from personal_agent.api import events
from personal_agent.api.duplicate_flow import decide_duplicate
from personal_agent.api.operation_store import (
    chat_request_fingerprint,
    get_operation,
    open_operation,
    request_cancel,
)
from personal_agent.api.orchestrator import (
    Authorizer,
    Dispatcher,
    Interpreter,
    run_operation,
)
from personal_agent.auth.tokens import TokenError, TokenKeyRing, verify_access_token
from personal_agent.storage.models import Device, Operation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class AuthContext:
    device_id: str
    scopes: tuple[str, ...]
    allowed_tools_version: str


@dataclass
class AgentApiDeps:
    session_factory: Callable[[], Any]
    token_ring: TokenKeyRing
    keyring: KeyRing
    interpreter: Interpreter
    dispatcher: Dispatcher
    #: Builds the per-device tool authorizer used by the orchestrator.
    build_authorizer: Callable[[AuthContext], Authorizer]
    #: The tools genuinely available to this device (design 5.3 /capabilities).
    capabilities: Callable[[AuthContext], list[dict[str, Any]]]
    now: Callable[[], datetime]


_STATUS_BY_CODE = {
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.SCOPE_DENIED: 403,
    ErrorCode.TOOL_NOT_ALLOWLISTED: 403,
    ErrorCode.HOST_CONTEXT_MISMATCH: 403,
    ErrorCode.INVALID_ARGUMENT: 400,
}

_MAX_JSON_BODY_BYTES = 64 * 1024

# Only an explicitly enumerated governed write may project `safe_result` as an
# external record id. Unknown and read-only tools fail toward `answer`, never
# toward evidence that a write happened.
_RECORD_ID_RESULT_TOOLS = frozenset(
    {
        "finance.log_expense",
        "finance.log_income",
        "finance.update_family_fund",
    }
)


class _Unauthenticated(Exception):
    """A request could not be tied to an active device."""


def build_app(deps: AgentApiDeps) -> FastAPI:
    app = FastAPI()

    def authenticate(request: Request, session) -> AuthContext:
        raw = request.headers.get("authorization", "")
        if not raw.startswith("Bearer "):
            raise _Unauthenticated("missing bearer token")
        token = raw[len("Bearer ") :].strip()
        try:
            claims = verify_access_token(deps.token_ring, token, now=deps.now())
        except TokenError as exc:
            raise _Unauthenticated(str(exc)) from exc
        device = session.get(Device, claims["device_id"])
        if device is None or device.status != "active":
            # A revoked device is refused even with a still-valid token.
            raise _Unauthenticated("device is not active")
        return AuthContext(
            device_id=device.device_id,
            scopes=tuple(claims.get("scopes", [])),
            allowed_tools_version=claims["allowed_tools_version"],
        )

    def idempotency_key(request: Request) -> str:
        key = request.headers.get("idempotency-key", "").strip()
        if not key:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="an Idempotency-Key header is required",
            )
        try:
            parsed = uuid.UUID(key)
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="Idempotency-Key must be a canonical UUIDv4",
            ) from exc
        if (
            parsed.version != 4
            or parsed.variant != uuid.RFC_4122
            or str(parsed) != key
        ):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="Idempotency-Key must be a canonical UUIDv4",
            )
        return key

    @app.post("/v1/chat/messages")
    async def post_message(request: Request):
        with deps.session_factory() as session:
            auth = authenticate(request, session)
            body = await _json_body(request)

            def work():
                key = idempotency_key(request)
                conversation_id = _required(body, "conversation_id")
                text = _required(body, "text")
                fingerprint = chat_request_fingerprint(
                    conversation_id=conversation_id, text=text
                )
                opened = open_operation(
                    session,
                    device_id=auth.device_id,
                    client_request_id=key,
                    request_fingerprint=fingerprint,
                    now=deps.now(),
                )
                if not opened.created and opened.operation.state != "accepted":
                    # A replay returns an already-started operation without
                    # invoking the model or Finance again.
                    return _operation_response(opened.operation)
                if opened.created:
                    # The request/operation anchor must survive any later model,
                    # resolver, process, or network failure. An accepted replay is
                    # safe to resume because no source submit can have happened.
                    session.commit()
                    session.refresh(opened.operation)

                events.append_event(
                    session, deps.keyring,
                    conversation_id=conversation_id, event_type=events.USER_MESSAGE,
                    content={"text": text}, operation_id=opened.operation.operation_id,
                    now=deps.now(),
                )
                result = run_operation(
                    session, opened.operation,
                    text=text, conversation_id=conversation_id,
                    interpreter=deps.interpreter, dispatcher=deps.dispatcher,
                    authorize=deps.build_authorizer(auth), keyring=deps.keyring,
                    now=deps.now(),
                )
                events.append_event(
                    session, deps.keyring,
                    conversation_id=conversation_id, event_type=events.OPERATION_RESULT,
                    content=_result_content(result),
                    operation_id=opened.operation.operation_id, now=deps.now(),
                )
                return _operation_response(
                    opened.operation, extra=_transient(result)
                )

            return _commit(session, work)

    @app.get("/v1/operations/{operation_id}")
    async def get_operation_status(operation_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                operation = _owned_operation(session, operation_id)
                return _operation_response(operation)

            return _commit(session, work)

    @app.delete("/v1/operations/{operation_id}")
    async def cancel_operation(operation_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                _owned_operation(session, operation_id)
                request_cancel(session, operation_id=operation_id, now=deps.now())
                return _operation_response(get_operation(session, operation_id))

            return _commit(session, work)

    @app.get("/v1/conversations/{conversation_id}/events")
    async def get_events(conversation_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                timeline = events.list_timeline(
                    session, deps.keyring, conversation_id=conversation_id
                )
                return JSONResponse(
                    {
                        "conversation_id": conversation_id,
                        "events": [
                            {
                                "event_id": entry.event_id,
                                "event_type": entry.event_type,
                                "operation_id": entry.operation_id,
                                "created_at": entry.created_at.isoformat(),
                                "content": entry.content,
                            }
                            for entry in timeline
                        ],
                    }
                )

            return _commit(session, work)

    @app.get("/v1/capabilities")
    async def get_capabilities(request: Request):
        with deps.session_factory() as session:
            def work():
                auth = authenticate(request, session)
                return JSONResponse(
                    {
                        "allowed_tools_version": auth.allowed_tools_version,
                        "tools": deps.capabilities(auth),
                    }
                )

            return _commit(session, work)

    @app.post("/v1/duplicate-checks/{duplicate_check_id}/decision")
    async def post_decision(duplicate_check_id: str, request: Request):
        with deps.session_factory() as session:
            auth = authenticate(request, session)
            body = await _json_body(request)

            def work():
                key = idempotency_key(request)
                decision = _required(body, "decision")
                outcome = decide_duplicate(
                    session, deps.keyring,
                    duplicate_check_id=duplicate_check_id, decision=decision,
                    device_id=auth.device_id, new_client_request_id=key,
                    now=deps.now(),
                )
                new_op = outcome.new_operation
                if new_op is not None and new_op.state == "accepted":
                    run_operation(
                        session, new_op,
                        text="", conversation_id="",
                        interpreter=deps.interpreter, dispatcher=deps.dispatcher,
                        authorize=deps.build_authorizer(auth), keyring=deps.keyring,
                        now=deps.now(),
                    )
                target = new_op if new_op is not None else _find_by_check(
                    session, duplicate_check_id, auth.device_id
                )
                return _operation_response(target)

            return _commit(session, work)

    @app.exception_handler(_Unauthenticated)
    async def _on_unauth(request: Request, exc: _Unauthenticated):
        return JSONResponse(
            {"error": {"code": "UNAUTHENTICATED"}}, status_code=401
        )

    @app.exception_handler(AppError)
    async def _on_app_error(request: Request, exc: AppError):
        return _error_response(exc)

    return app


def _commit(session, work: Callable[[], Any]):
    try:
        response = work()
        session.commit()
        return response
    except (_Unauthenticated, AppError):
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise


def _owned_operation(session, operation_id: str) -> Operation:
    operation = get_operation(session, operation_id)
    if operation is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"no such operation {operation_id}",
        )
    return operation


async def _json_body(request: Request) -> dict[str, Any]:
    """Read one bounded JSON object after authentication."""
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="Content-Type must be application/json",
        )

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="Content-Length must be an integer",
            ) from exc
        if declared_length < 0 or declared_length > _MAX_JSON_BODY_BYTES:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"JSON body exceeds {_MAX_JSON_BODY_BYTES} bytes",
            )

    raw = await request.body()
    if len(raw) > _MAX_JSON_BODY_BYTES:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"JSON body exceeds {_MAX_JSON_BODY_BYTES} bytes",
        )
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="request body must be valid UTF-8 JSON",
        ) from exc
    if not isinstance(body, dict):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="request body must be a JSON object",
        )
    return body


def _find_by_check(
    session, duplicate_check_id: str, device_id: str
) -> Operation:
    operation = (
        session.query(Operation)
        .filter(Operation.duplicate_check_id == duplicate_check_id)
        .join(Operation.api_request)
        .filter(Operation.api_request.has(device_id=device_id))
        .order_by(Operation.created_at.desc())
        .first()
    )
    if operation is None:  # pragma: no cover - decide_duplicate already validated
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="no operation for this duplicate check",
        )
    return operation


def _required(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} is required",
        )
    return value


def _operation_response(
    operation: Operation, *, extra: dict[str, Any] | None = None
) -> JSONResponse:
    # A parked or in-flight operation is 202; a resolved one is 200. The client
    # polls the same projection either way. `extra` carries transient fields that
    # are not persisted on the operation (a clarification question, the existing
    # duplicate record), returned on the immediate reply only.
    from personal_agent.api.operation_state import is_terminal

    projection = _operation_projection(operation)
    if extra:
        projection.update(extra)
    return JSONResponse(
        projection,
        status_code=200 if is_terminal(operation.state) else 202,
    )


def _transient(result) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for field in ("answer", "clarification", "duplicate_existing"):
        value = getattr(result, field, None)
        if value is not None:
            fields[field] = value
    return fields


def _operation_projection(operation: Operation) -> dict[str, Any]:
    projection = {
        "operation_id": operation.operation_id,
        "state": operation.state,
        "cancel_requested": operation.cancel_requested,
        "client_detached": operation.client_detached,
        "tool": operation.tool,
        "record_id": None,
        "failure_reason": operation.failure_reason,
        "duplicate_check_id": operation.duplicate_check_id,
    }
    if operation.safe_result is not None:
        if operation.tool in _RECORD_ID_RESULT_TOOLS:
            projection["record_id"] = operation.safe_result
        else:
            projection["answer"] = operation.safe_result
    return projection


def _result_content(result) -> dict[str, Any]:
    content: dict[str, Any] = {"state": result.state}
    for field in (
        "record_id", "answer", "clarification", "duplicate_check_id",
        "duplicate_existing", "failure_reason",
    ):
        value = getattr(result, field, None)
        if value is not None:
            content[field] = value
    return content


def _error_response(error: AppError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(error.code, 500)
    envelope = error.to_envelope().model_dump(mode="json")
    return JSONResponse({"error": envelope}, status_code=status)
