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

import asyncio
import json
import logging
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
    transition_operation,
)
from personal_agent.api.operation_state import StaleOperationVersionError
from personal_agent.api.orchestrator import (
    Authorizer,
    Dispatcher,
    Interpreter,
    run_operation,
)
from personal_agent.api.review_view import (
    RecordReader,
    acknowledge,
    defer,
    list_reviews,
    review_detail,
)
from personal_agent.api.request_payload import (
    ChatRequestPayload,
    continuation_context,
    open_chat_request,
    seal_chat_request,
    with_clarification_question,
)
from personal_agent.auth.tokens import TokenError, TokenKeyRing, verify_access_token
from personal_agent.storage.models import REVIEW_STATUSES, Device, Operation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


logger = logging.getLogger(__name__)


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
    #: Builds the per-device interpreter; it is bound to that device's visible
    #: tools, so it is constructed per request like the authorizer.
    build_interpreter: Callable[[AuthContext], Interpreter]
    #: Builds the per-operation Finance dispatcher. Like the interpreter it is
    #: bound to the calling device -- it signs a Host Context naming that device
    #: -- so it is constructed per request and never shared between them.
    build_dispatcher: Callable[[AuthContext, str], Dispatcher]
    #: Builds the per-device tool authorizer used by the orchestrator.
    build_authorizer: Callable[[AuthContext], Authorizer]
    #: The tools genuinely available to this device (design 5.3 /capabilities).
    capabilities: Callable[[AuthContext], list[dict[str, Any]]]
    now: Callable[[], datetime]
    #: Reads a reviewed record's *current* ledger values (design 7.7 step 5).
    #: `None` means the review endpoints are not composed, and opening a card
    #: says so rather than rendering one with no values.
    read_record: RecordReader | None = None
    #: HTTP waits no longer than this for an operation worker. Production uses
    #: the design's 30-second ceiling; tests may shorten it.
    sync_wait_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.sync_wait_seconds <= 0 or self.sync_wait_seconds > 30.0:
            raise ValueError("sync_wait_seconds must be within (0, 30]")


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
    operation_tasks: dict[str, asyncio.Task[JSONResponse]] = {}

    def forget_task(operation_id: str, done: asyncio.Task[JSONResponse]) -> None:
        if operation_tasks.get(operation_id) is done:
            operation_tasks.pop(operation_id, None)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.error(
                    "operation worker failed before producing a safe projection",
                    exc_info=(type(error), error, error.__traceback__),
                )

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
        # Authentication stays ahead of body parsing, while SQLite and the
        # model/dispatcher run outside the event loop.
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        key = idempotency_key(request)
        conversation_id = _required(body, "conversation_id")
        text = _required(body, "text")
        clarification_of = _optional_operation_id(body, "clarification_of")

        anchored = await asyncio.to_thread(
            _anchor_chat,
            deps,
            auth,
            key,
            conversation_id,
            text,
            clarification_of,
        )
        if anchored.state != "accepted":
            return await asyncio.to_thread(
                _load_operation_response,
                deps,
                anchored.operation_id,
                auth.device_id,
            )

        task = operation_tasks.get(anchored.operation_id)
        if task is None or task.done():
            task = asyncio.create_task(
                asyncio.to_thread(
                    _process_chat,
                    deps,
                    auth,
                    anchored.operation_id,
                )
            )
            operation_tasks[anchored.operation_id] = task
            task.add_done_callback(
                lambda done, operation_id=anchored.operation_id: forget_task(
                    operation_id, done
                )
            )
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=deps.sync_wait_seconds
            )
        except TimeoutError:
            # The worker owns its session and continues. The client polls this
            # durable operation id; timeout never means the write was cancelled.
            return JSONResponse(
                {
                    "operation_id": anchored.operation_id,
                    "state": "accepted",
                    "cancel_requested": False,
                    "client_detached": False,
                    "tool": None,
                    "record_id": None,
                    "failure_reason": None,
                    "duplicate_check_id": None,
                },
                status_code=202,
            )

    @app.get("/v1/operations/{operation_id}")
    async def get_operation_status(operation_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                auth = authenticate(request, session)
                operation = _owned_operation(
                    session, operation_id, device_id=auth.device_id
                )
                return _operation_response(operation)

            return _commit(session, work)

    @app.delete("/v1/operations/{operation_id}")
    async def cancel_operation(operation_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                auth = authenticate(request, session)
                _owned_operation(session, operation_id, device_id=auth.device_id)
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

    @app.get("/v1/daily-reviews")
    async def get_daily_reviews(request: Request):
        status = request.query_params.get("status")
        if status is not None and status not in REVIEW_STATUSES:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"unknown review status {status!r}",
            )
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                return JSONResponse(
                    {
                        "reviews": [
                            summary.to_json()
                            for summary in list_reviews(session, status=status)
                        ]
                    }
                )

            return _commit(session, work)

    @app.get("/v1/daily-reviews/{review_id}")
    async def get_daily_review(review_id: str, request: Request):
        # Opening a card reads the ledger once per item, so the whole handler
        # runs off the event loop rather than only the SQLite part.
        return await asyncio.to_thread(_read_daily_review, deps, request, review_id, authenticate)

    @app.post("/v1/daily-reviews/{review_id}/ack")
    async def post_review_ack(review_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                summary = acknowledge(session, review_id, now=deps.now())
                return JSONResponse(summary.to_json())

            return _commit(session, work)

    @app.post("/v1/daily-reviews/{review_id}/defer")
    async def post_review_defer(review_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                summary = defer(session, review_id, now=deps.now())
                return JSONResponse(summary.to_json())

            return _commit(session, work)

    @app.post("/v1/duplicate-checks/{duplicate_check_id}/decision")
    async def post_decision(duplicate_check_id: str, request: Request):
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        key = idempotency_key(request)
        decision = _required(body, "decision")
        return await asyncio.to_thread(
            _process_duplicate_decision,
            deps,
            auth,
            duplicate_check_id,
            decision,
            key,
        )

    @app.exception_handler(_Unauthenticated)
    async def _on_unauth(request: Request, exc: _Unauthenticated):
        return JSONResponse(
            {"error": {"code": "UNAUTHENTICATED"}}, status_code=401
        )

    @app.exception_handler(AppError)
    async def _on_app_error(request: Request, exc: AppError):
        return _error_response(exc)

    return app


@dataclass(frozen=True)
class _AnchoredChat:
    operation_id: str
    state: str


def _authenticate_once(request, deps, authenticate) -> AuthContext:
    with deps.session_factory() as session:
        return authenticate(request, session)


def _anchor_chat(
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    clarification_of: str | None,
) -> _AnchoredChat:
    """Persist request, encrypted payload and user event before model work."""

    with deps.session_factory() as session:
        try:
            fingerprint = chat_request_fingerprint(
                conversation_id=conversation_id,
                text=text,
                clarification_of=clarification_of,
            )
            opened = open_operation(
                session,
                device_id=auth.device_id,
                client_request_id=key,
                request_fingerprint=fingerprint,
                now=deps.now(),
            )
            operation = opened.operation
            if not opened.created:
                return _AnchoredChat(operation.operation_id, operation.state)

            context = None
            if clarification_of is not None:
                source = _owned_operation(
                    session,
                    clarification_of,
                    device_id=auth.device_id,
                )
                if source.state != "waiting_for_clarification":
                    raise AppError(
                        ErrorCode.INVALID_ARGUMENT,
                        internal_detail=(
                            "clarification_of must name an operation waiting "
                            "for clarification"
                        ),
                    )
                source_payload = open_chat_request(
                    deps.keyring,
                    request_id=source.request_id,
                    envelope=source.api_request.encrypted_request_payload,
                )
                if source_payload.conversation_id != conversation_id:
                    raise AppError(
                        ErrorCode.INVALID_ARGUMENT,
                        internal_detail=(
                            "clarification must stay in the source conversation"
                        ),
                    )
                context = continuation_context(source_payload)
                transition_operation(
                    session,
                    operation_id=source.operation_id,
                    current_state=source.state,
                    current_version=source.state_version,
                    target_state="cancelled_pre_submit",
                    now=deps.now(),
                )

            payload = ChatRequestPayload(
                conversation_id=conversation_id,
                text=text,
                clarification_of=clarification_of,
                clarification_context=context,
            )
            operation.api_request.encrypted_request_payload = seal_chat_request(
                deps.keyring,
                request_id=operation.request_id,
                payload=payload,
            )
            events.append_event(
                session,
                deps.keyring,
                conversation_id=conversation_id,
                event_type=events.USER_MESSAGE,
                content={
                    "text": text,
                    **(
                        {"clarification_of": clarification_of}
                        if clarification_of is not None
                        else {}
                    ),
                },
                operation_id=operation.operation_id,
                now=deps.now(),
            )
            session.commit()
            return _AnchoredChat(operation.operation_id, operation.state)
        except Exception:
            session.rollback()
            raise


def _process_chat(
    deps: AgentApiDeps,
    auth: AuthContext,
    operation_id: str,
) -> JSONResponse:
    """Run one accepted operation in a worker-owned database session."""

    with deps.session_factory() as session:
        try:
            operation = _owned_operation(
                session, operation_id, device_id=auth.device_id
            )
            if operation.state != "accepted":
                return _operation_response(operation)
            payload = open_chat_request(
                deps.keyring,
                request_id=operation.request_id,
                envelope=operation.api_request.encrypted_request_payload,
            )
            result = run_operation(
                session,
                operation,
                text=payload.text,
                conversation_id=payload.conversation_id,
                clarification_context=payload.clarification_context,
                interpreter=deps.build_interpreter(auth),
                dispatcher=deps.build_dispatcher(auth, operation.trace_id),
                authorize=deps.build_authorizer(auth),
                keyring=deps.keyring,
                now=deps.now,
            )
            if result.state == "waiting_for_clarification":
                question = result.clarification
                if not isinstance(question, str) or not question.strip():
                    raise AppError(
                        ErrorCode.INTERNAL_ERROR,
                        internal_detail="parked clarification has no question",
                    )
                operation.api_request.encrypted_request_payload = seal_chat_request(
                    deps.keyring,
                    request_id=operation.request_id,
                    payload=with_clarification_question(payload, question),
                )
            events.append_event(
                session,
                deps.keyring,
                conversation_id=payload.conversation_id,
                event_type=events.OPERATION_RESULT,
                content=_result_content(result),
                operation_id=operation.operation_id,
                now=deps.now(),
            )
            session.commit()
            session.refresh(operation)
            return _operation_response(operation, extra=_transient(result))
        except StaleOperationVersionError:
            session.rollback()
            operation = _owned_operation(
                session, operation_id, device_id=auth.device_id
            )
            return _operation_response(operation)
        except Exception:
            session.rollback()
            raise


def _load_operation_response(
    deps: AgentApiDeps, operation_id: str, device_id: str
) -> JSONResponse:
    with deps.session_factory() as session:
        operation = _owned_operation(session, operation_id, device_id=device_id)
        return _operation_response(operation)


def _process_duplicate_decision(
    deps: AgentApiDeps,
    auth: AuthContext,
    duplicate_check_id: str,
    decision: str,
    key: str,
) -> JSONResponse:
    with deps.session_factory() as session:
        def work():
            outcome = decide_duplicate(
                session,
                deps.keyring,
                duplicate_check_id=duplicate_check_id,
                decision=decision,
                device_id=auth.device_id,
                new_client_request_id=key,
                now=deps.now(),
            )
            new_op = outcome.new_operation
            if new_op is not None and new_op.state == "accepted":
                run_operation(
                    session,
                    new_op,
                    text="",
                    conversation_id="",
                    interpreter=deps.build_interpreter(auth),
                    dispatcher=deps.build_dispatcher(auth, new_op.trace_id),
                    authorize=deps.build_authorizer(auth),
                    keyring=deps.keyring,
                    now=deps.now,
                )
            target = new_op if new_op is not None else _find_by_check(
                session, duplicate_check_id, auth.device_id
            )
            return _operation_response(target)

        return _commit(session, work)


def _read_daily_review(deps: AgentApiDeps, request, review_id: str, authenticate):
    """One card with live ledger values, entirely off the event loop."""
    if deps.read_record is None:
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail=(
                "the review surface is not composed: no ledger reader is wired"
            ),
        )
    with deps.session_factory() as session:
        def work():
            authenticate(request, session)
            return JSONResponse(
                review_detail(session, review_id, deps.read_record)
            )

        return _commit(session, work)


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


def _owned_operation(
    session, operation_id: str, *, device_id: str
) -> Operation:
    operation = (
        session.query(Operation)
        .filter(Operation.operation_id == operation_id)
        .filter(Operation.api_request.has(device_id=device_id))
        .one_or_none()
    )
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


def _optional_operation_id(body: dict[str, Any], field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.startswith("op_")
        or len(value) != 35
    ):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} must be an operation id",
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
        if operation.state == "waiting_for_clarification":
            projection["clarification"] = operation.safe_result
        elif operation.state == "waiting_for_duplicate_decision":
            projection["duplicate_existing"] = operation.safe_result
        elif (
            operation.state == "succeeded"
            and operation.tool in _RECORD_ID_RESULT_TOOLS
        ):
            projection["record_id"] = operation.safe_result
        elif operation.state == "succeeded":
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
