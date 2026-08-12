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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as text_clause
from sqlalchemy.exc import IntegrityError

from personal_agent.api import events
from personal_agent.api.manual_review import (
    append_resolution_event,
    resolve_manual_review,
)
from personal_agent_core.timeutil import to_rfc3339
from personal_agent.api.device_api import (
    DeviceAuthRejected,
    EnrollmentRejected,
    claim_device,
    issue_device_challenge,
    issue_device_token,
    list_devices,
    revoke_device_by_id,
    update_push_token,
)
from personal_agent.api.duplicate_flow import decide_duplicate
from personal_agent.api.operation_store import (
    chat_request_fingerprint,
    get_operation,
    open_operation,
    request_cancel,
    transition_operation,
)
from personal_agent.api.operation_state import (
    StaleOperationVersionError,
    can_cancel_pre_submit,
    is_terminal,
)
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
from personal_agent.context.builder import ContextEnvelope
from personal_agent.context.continuation import (
    ClarificationContext,
    ClarificationExchange,
    FinanceRetryContext,
)
from personal_agent.context.config import (
    ContextConfig,
    ContextConfigError,
    default_context_config,
)
from personal_agent.context.session_manager import (
    ResolvedClassification,
    SessionManager,
)
from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.runtime.bookkeeping_intent import (
    is_bookkeeping_write_request,
    is_finance_retry_request,
)
from personal_agent.storage.models import (
    REVIEW_STATUSES,
    TERMINAL_OPERATION_STATES,
    ApiRequest,
    ConversationEvent,
    Device,
    Operation,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import (
    MODEL_RETRYABLE_FAILURE_REASONS,
    AppError,
    ErrorCode,
)
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.tool_ir import TOOL_CONTRACTS


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    device_id: str
    scopes: tuple[str, ...]
    allowed_tools_version: str


class EnvelopeFactory(Protocol):
    """Assembles one turn's model context from already-persisted state.

    It takes the worker's own database session: the anchor event, the Session,
    the Checkpoints and the pending operations all have to be read inside the
    transaction that is driving this operation, not from a second connection
    that could see a different moment.
    """

    def __call__(
        self,
        session: Any,
        auth: AuthContext,
        *,
        conversation_id: str,
        session_id: str,
        current_event_id: str,
        user_text: str,
        clarification_context: ClarificationContext | None,
        finance_retry_context: FinanceRetryContext | None,
    ) -> ContextEnvelope: ...


@dataclass
class AgentApiDeps:
    session_factory: Callable[[], Any]
    token_ring: TokenKeyRing
    keyring: KeyRing
    #: `CAP-001`. The identifier/lineage HMAC resolves a legacy conversation id
    #: onto the canonical Timeline; the cursor HMAC signs a page anchor. Design
    #: 5.6 keeps them separate from the data key and from each other.
    identifier_key: HmacKey | HmacKeyRing
    cursor_key: HmacKey | HmacKeyRing
    #: Builds the per-device interpreter. Since `CAP-001` it carries no catalog
    #: of its own: the tools and the instruction travel inside the envelope.
    build_interpreter: Callable[[AuthContext], Interpreter]
    #: `CAP-001`. Assembles one turn's `ContextEnvelope` (design §9). It is the
    #: only way context reaches a model, and it is composed here rather than
    #: inside the interpreter because assembly is database work owned by the
    #: worker's own session.
    build_envelope: EnvelopeFactory
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
    #: `CAP-001` design §7.3/§8. Runs the Compactor for one Session after a turn
    #: whose input crossed the soft limit. `None` means no Compactor provider is
    #: composed, and the signal is then recorded and not acted on.
    compact_session: Callable[[Any, str], None] | None = None
    read_record: RecordReader | None = None
    #: The server's current `allowed_tools_version`, stamped onto a device at
    #: enrollment (design 4.1 step 3). `None` means enrollment is not composed:
    #: claiming a code then refuses, because a device enrolled against a version
    #: nobody supplied would be granted no tools and look revoked instead.
    enrollment_manifest_version: str | None = None
    #: HTTP waits no longer than this for an operation worker. Production uses
    #: the design's 30-second ceiling; tests may shorten it.
    sync_wait_seconds: float = 30.0
    #: `CAP-001`. The validated context budget and the Session Manager built on
    #: it. Both default to the shipped provisional configuration with no
    #: semantic classifier, which is the safe shape: every boundary decision is
    #: then deterministic and every uncertain case continues the Session.
    context_config: ContextConfig = field(default_factory=default_context_config)
    session_manager: SessionManager | None = None
    #: `DEV-031`. Where the Feishu ledger lives, named by the service and never
    #: invented by a client (Henson's 2026-07-30 decision). `None` means this
    #: composition does not know it, and the field is then absent from
    #: `/v1/capabilities` so the app hides the jump honestly rather than opening
    #: a placeholder. It is a resource identifier, not a secret: it comes from
    #: local configuration and travels only to enrolled devices.
    ledger_url: str | None = None

    def __post_init__(self) -> None:
        if self.sync_wait_seconds <= 0 or self.sync_wait_seconds > 30.0:
            raise ValueError("sync_wait_seconds must be within (0, 30]")
        if self.session_manager is None:
            self.session_manager = SessionManager(self.context_config)


_STATUS_BY_CODE = {
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.SCOPE_DENIED: 403,
    ErrorCode.TOOL_NOT_ALLOWLISTED: 403,
    ErrorCode.HOST_CONTEXT_MISMATCH: 403,
    ErrorCode.INVALID_ARGUMENT: 400,
    # `CAP-001`: an id that names no Timeline here. A `404` says so without
    # confirming whether that id exists anywhere, and without ever being read as
    # "so create it".
    ErrorCode.TIMELINE_MISMATCH: 404,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.PENDING_OPERATION_NOT_CANCELLABLE: 400,
}

_MAX_JSON_BODY_BYTES = 64 * 1024

# Only a governed write may project `safe_result` as an external record id.
# Unknown and read-only tools fail toward `answer`, never toward evidence that a
# write happened.
#
# Derived from the manifest's own risk level, never hand-listed. A hand-listed
# set drifts silently: it held exactly the three *enabled* R2 tools while
# `finance.log_expense_batch` was already R2 in the IR, so enabling that tool
# would have sent a succeeded batch write down the `answer` branch below --
# which `ChatWire.swift` then renders as a clean answer for a write it never
# proved. `test_chat_receipt_vectors` compares this set to the cross-language
# vector file, but both sides were hand-maintained, so they would have drifted
# together and stayed green. Disabled tools are included deliberately: the risk
# is precisely a tool being enabled later.
_RECORD_ID_RESULT_TOOLS = frozenset(
    contract.name for contract in TOOL_CONTRACTS if contract.risk_level == "R2"
)


class _Unauthenticated(Exception):
    """A request could not be tied to an active device."""


def build_restore_read_only_app(session_factory: Callable[[], Any]) -> FastAPI:
    """Minimal service-binary probe for an isolated restored database.

    This mode deliberately composes no model, MCP client, recovery worker or
    write handler. The database is supplied through a ``mode=ro`` engine by the
    CLI; each probe request performs a real read before returning the same 401
    an unauthenticated production capabilities request would receive.
    """
    app = FastAPI()

    @app.get("/v1/capabilities")
    async def restored_capabilities_probe() -> JSONResponse:
        with session_factory() as session:
            session.execute(text_clause("SELECT 1")).scalar_one()
        return JSONResponse(
            {"error": {"code": "UNAUTHENTICATED"}}, status_code=401
        )

    return app


def build_app(deps: AgentApiDeps) -> FastAPI:
    operation_tasks: dict[str, asyncio.Task[_ProcessedChat]] = {}
    compaction_tasks: set[asyncio.Task[None]] = set()

    async def drain_background_tasks() -> None:
        """Let accepted operations and their follow-up compactions finish.

        Production ASGI shutdown runs this before composition closes the MCP and
        control clients. Operation callbacks may enqueue compaction, so drain the
        operation set first and then the compaction set it produced.
        """

        async def bounded(tasks: tuple[asyncio.Task[Any], ...]) -> None:
            if not tasks:
                return
            _done, pending = await asyncio.wait(tasks, timeout=30.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        await bounded(tuple(operation_tasks.values()))
        # Task done callbacks enqueue compaction with call_soon semantics. Give
        # those callbacks one loop turn before snapshotting the compaction set.
        await asyncio.sleep(0)
        await bounded(tuple(compaction_tasks))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await drain_background_tasks()

    app = FastAPI(lifespan=lifespan)
    # Tests and embedded hosts that do not drive ASGI lifespan can still perform
    # a truthful, deterministic drain instead of sleeping and guessing.
    app.state.drain_background_tasks = drain_background_tasks

    def finish_compaction(done: asyncio.Task[None]) -> None:
        compaction_tasks.discard(done)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.warning(
                    "compaction after a turn failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

    def schedule_compaction(session_id: str) -> None:
        task = asyncio.create_task(
            asyncio.to_thread(_compact_session_in_background, deps, session_id)
        )
        compaction_tasks.add(task)
        task.add_done_callback(finish_compaction)

    def forget_task(operation_id: str, done: asyncio.Task[_ProcessedChat]) -> None:
        if operation_tasks.get(operation_id) is done:
            operation_tasks.pop(operation_id, None)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.error(
                    "operation worker failed before producing a safe projection",
                    exc_info=(type(error), error, error.__traceback__),
                )
            else:
                session_id = done.result().compact_session_id
                if session_id is not None:
                    schedule_compaction(session_id)

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
            # A short-lived token proves enrollment, but it intentionally does
            # not freeze the device's governed tool binding.  Rebinding tools
            # must take effect immediately and the capability projection must
            # describe the same current database row enforced by dispatch.
            allowed_tools_version=device.allowed_tools_version,
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

    # --- the device identity surface (design 5.1) ----------------------------
    #
    # The first three endpoints are the only unauthenticated ones in the
    # service. They are bodies-in, opaque-refusal-out; the handlers live in
    # `device_api` so their failure shapes can be tested without HTTP.

    @app.post("/v1/enrollments/claim")
    async def post_enrollment_claim(request: Request):
        body = await _json_body(request)
        return await asyncio.to_thread(_claim_enrollment, deps, body)

    @app.post("/v1/auth/challenges")
    async def post_auth_challenge(request: Request):
        body = await _json_body(request)
        return await asyncio.to_thread(_issue_challenge, deps, body)

    @app.post("/v1/auth/tokens")
    async def post_auth_token(request: Request):
        body = await _json_body(request)
        return await asyncio.to_thread(_issue_token, deps, body)

    @app.get("/v1/devices")
    async def get_devices(request: Request):
        with deps.session_factory() as session:
            def work():
                auth = authenticate(request, session)
                return JSONResponse(
                    list_devices(
                        session, device_id=auth.device_id, scopes=auth.scopes
                    )
                )

            return _commit(session, work)

    @app.delete("/v1/devices/{device_id}")
    async def delete_device(device_id: str, request: Request):
        with deps.session_factory() as session:
            def work():
                auth = authenticate(request, session)
                return JSONResponse(
                    revoke_device_by_id(
                        session,
                        caller_device_id=auth.device_id,
                        scopes=auth.scopes,
                        device_id=device_id,
                        now=deps.now(),
                    )
                )

            return _commit(session, work)

    @app.put("/v1/devices/{device_id}/push-token")
    async def put_push_token(device_id: str, request: Request):
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        return await asyncio.to_thread(
            _update_push_token, deps, auth, device_id, body
        )

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
        start_new_session = _optional_bool(body, "start_new_session", default=False)
        if start_new_session and clarification_of is not None:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    "start_new_session cannot be combined with clarification_of"
                ),
            )

        anchored = await asyncio.to_thread(
            _preflight_chat_replay,
            deps,
            auth,
            key,
            conversation_id,
            text,
            clarification_of,
            start_new_session,
        )
        if anchored is None:
            # This is an explicit, user-confirmed boundary. A classifier must
            # not spend a model call or be allowed to weaken that instruction.
            if start_new_session:
                resolved_classification = ResolvedClassification(
                    expected_session_id=None,
                    expected_last_event_at=None,
                    expected_timeline_sequence=0,
                    outcome=None,
                )
            else:
                resolved_classification = await asyncio.to_thread(
                    _classify_chat_boundary,
                    deps,
                    conversation_id,
                    text,
                    clarification_of,
                )
            anchored = await asyncio.to_thread(
                _anchor_chat,
                deps,
                auth,
                key,
                conversation_id,
                text,
                clarification_of,
                start_new_session,
                resolved_classification,
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
            processed = await asyncio.wait_for(
                asyncio.shield(task), timeout=deps.sync_wait_seconds
            )
            return processed.response
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
        except Exception:
            # The worker died without producing a projection. The operation is
            # already anchored and durable, so the one thing the client must not
            # lose is its id: on 2026-08-03 an unhandled `database is locked`
            # reached the client as HTTP 500 with a `null` body, leaving nothing
            # to poll for a write that had in fact succeeded in Feishu.
            #
            # This includes an AppError that escaped the worker. Business
            # refusals are supposed to become durable terminal operation states
            # inside `run_operation`; once a task-level exception reaches this
            # route, its Python type must not decide whether the client keeps the
            # id. This reports the operation's *durable* state, so it claims
            # nothing the database does not already say -- an operation still in
            # flight comes back in flight, and recovery resolves it later.
            logger.exception(
                "the chat worker failed; returning the durable operation state"
            )
            return await asyncio.to_thread(
                _load_operation_response,
                deps,
                anchored.operation_id,
                auth.device_id,
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
        # `CAP-001` design 14: the first request returns the newest page and
        # scrolling up follows `older_cursor`. Incremental sync is
        # `direction=newer` and must carry a cursor.
        cursor = request.query_params.get("cursor")
        direction = request.query_params.get("direction", "older")
        try:
            limit = deps.context_config.page_limit(
                _optional_int(request.query_params.get("limit"))
            )
        except ContextConfigError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT, internal_detail="limit is not usable"
            ) from exc
        with deps.session_factory() as session:
            def work():
                authenticate(request, session)
                timeline_id = events.resolve_timeline(
                    session,
                    deps.identifier_key,
                    client_conversation_id=conversation_id,
                    now=deps.now(),
                )
                page = events.read_page(
                    session,
                    deps.keyring,
                    deps.cursor_key,
                    conversation_id=timeline_id,
                    cursor=cursor,
                    direction=direction,
                    limit=limit,
                )
                return JSONResponse(
                    {
                        "conversation_id": timeline_id,
                        "events": [
                            # `timeline_sequence`, `session_id` and the sealed
                            # envelope stay server-side (design 14).
                            {
                                "event_id": entry.event_id,
                                "event_type": entry.event_type,
                                "operation_id": entry.operation_id,
                                "created_at": entry.created_at.isoformat(),
                                "content": entry.content,
                            }
                            for entry in page.entries
                        ],
                        "older_cursor": page.older_cursor,
                        "newer_cursor": page.newer_cursor,
                        "has_older": page.has_older,
                        "has_newer": page.has_newer,
                    }
                )

            return _commit(session, work)

    @app.get("/v1/capabilities")
    async def get_capabilities(request: Request):
        with deps.session_factory() as session:
            def work():
                auth = authenticate(request, session)
                body: dict[str, Any] = {
                    "allowed_tools_version": auth.allowed_tools_version,
                    "tools": deps.capabilities(auth),
                    # Every enrolled device resolves to this one Timeline
                    # (design 4.2.2). The field keeps its compatibility
                    # name; renaming it is an API major version.
                    "conversation_id": events.canonical_timeline_id(
                        session, now=deps.now()
                    ),
                }
                if deps.ledger_url is not None:
                    body["ledger_url"] = deps.ledger_url
                return JSONResponse(body)

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

    @app.post("/v1/operations/{operation_id}/resolution")
    async def post_manual_resolution(operation_id: str, request: Request):
        """Record what a person found in the ledger for a reviewed operation.

        Reads and writes only the Agent database -- it contacts no model and no
        external service, so it is safe to re-run against fresh state and takes
        the default retrying commit.
        """
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        resolution = _required(body, "resolution")
        return await asyncio.to_thread(
            _process_manual_resolution, deps, auth, operation_id, resolution
        )

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

    @app.exception_handler(EnrollmentRejected)
    async def _on_enrollment_rejected(request: Request, exc: EnrollmentRejected):
        # Unknown, spent and expired codes are one answer. The operator sees the
        # difference in the log line below; a caller cannot probe for it.
        logger.info("enrollment refused: %s", exc)
        return JSONResponse(
            {"error": {"code": "ENROLLMENT_REJECTED"}}, status_code=403
        )

    @app.exception_handler(DeviceAuthRejected)
    async def _on_device_auth_rejected(request: Request, exc: DeviceAuthRejected):
        logger.info("device authentication refused: %s", exc)
        return JSONResponse(
            {"error": {"code": "DEVICE_AUTH_REJECTED"}}, status_code=401
        )

    @app.exception_handler(AppError)
    async def _on_app_error(request: Request, exc: AppError):
        return _error_response(exc)

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception):
        """Nothing leaves this service as a bare 500 with a `null` body.

        Added after 2026-08-03, when an unhandled `database is locked` did
        exactly that and the client was left with no code to act on and no
        operation id to poll. The envelope is the same fixed-text one every
        other refusal uses, so this adds no leak: the exception's own message,
        which can name paths and identifiers, stays in the journal.

        It deliberately says nothing about whether anything was written. An
        unexpected failure is not evidence either way, and the caller's route to
        the truth is the durable operation id, not this response.
        """
        logger.exception("unhandled error on %s", request.url.path)
        return _error_response(AppError(ErrorCode.INTERNAL_ERROR))

    return app


@dataclass(frozen=True)
class _AnchoredChat:
    operation_id: str
    state: str


@dataclass(frozen=True)
class _ProcessedChat:
    response: JSONResponse
    compact_session_id: str | None = None


def _authenticate_once(request, deps, authenticate) -> AuthContext:
    with deps.session_factory() as session:
        return authenticate(request, session)


def _claim_enrollment(deps: AgentApiDeps, body: dict[str, Any]) -> JSONResponse:
    if deps.enrollment_manifest_version is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=(
                "enrollment is not composed: no allowed_tools_version was wired"
            ),
        )
    with deps.session_factory() as session:
        def work():
            return JSONResponse(
                claim_device(
                    session,
                    body=body,
                    allowed_tools_version=deps.enrollment_manifest_version,
                    now=deps.now(),
                ),
                status_code=201,
            )

        return _commit(session, work)


def _issue_challenge(deps: AgentApiDeps, body: dict[str, Any]) -> JSONResponse:
    with deps.session_factory() as session:
        def work():
            return JSONResponse(
                issue_device_challenge(session, body=body, now=deps.now())
            )

        return _commit(session, work)


def _issue_token(deps: AgentApiDeps, body: dict[str, Any]) -> JSONResponse:
    with deps.session_factory() as session:
        try:
            issued = issue_device_token(
                session,
                body=body,
                token_ring=deps.token_ring,
                now=deps.now(),
            )
        except DeviceAuthRejected:
            # The counted failed attempt is what makes `MAX_CHALLENGE_ATTEMPTS`
            # real, and it lives in this session. Rolling back here -- the
            # reflex on any failed request -- would hand a stolen challenge id
            # unlimited signature guesses, so a refusal commits its own
            # bookkeeping and then propagates.
            session.commit()
            raise
        except Exception:
            session.rollback()
            raise
        session.commit()
        return JSONResponse(issued.to_json())


def _update_push_token(
    deps: AgentApiDeps, auth: AuthContext, device_id: str, body: dict[str, Any]
) -> JSONResponse:
    with deps.session_factory() as session:
        def work():
            return JSONResponse(
                update_push_token(
                    session,
                    deps.keyring,
                    caller_device_id=auth.device_id,
                    device_id=device_id,
                    body=body,
                    now=deps.now(),
                )
            )

        return _commit(session, work)


def _preflight_chat_replay(
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    clarification_of: str | None,
    start_new_session: bool,
) -> _AnchoredChat | None:
    """Return an existing idempotent chat before spending a classifier call."""

    with deps.session_factory() as session:
        try:
            timeline_id = events.resolve_timeline(
                session,
                deps.identifier_key,
                client_conversation_id=conversation_id,
                now=deps.now(),
            )
            fingerprint = chat_request_fingerprint(
                conversation_id=timeline_id,
                text=text,
                clarification_of=clarification_of,
                start_new_session=start_new_session,
            )
            request_row = (
                session.query(ApiRequest)
                .filter(
                    ApiRequest.device_id == auth.device_id,
                    ApiRequest.client_request_id == key,
                )
                .one_or_none()
            )
            if request_row is None:
                session.commit()
                return None
            if request_row.request_fingerprint != fingerprint:
                raise AppError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    internal_detail=(
                        f"client_request_id {key} is already bound to a "
                        "different request"
                    ),
                )
            operation = (
                session.query(Operation)
                .filter(Operation.request_id == request_row.request_id)
                .one_or_none()
            )
            if operation is None:
                raise AppError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    internal_detail=(
                        f"client_request_id {key} is already bound "
                        "to a non-chat request"
                    ),
                )
            result = _AnchoredChat(operation.operation_id, operation.state)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise


def _classify_chat_boundary(
    deps: AgentApiDeps,
    conversation_id: str,
    text: str,
    clarification_of: str | None,
) -> ResolvedClassification:
    """Prepare under SQLite, then call the classifier with no transaction open.

    The write transaction has not started yet: a crash or timeout here leaves no
    durable accepted operation without its anchoring event. A concurrent request
    may change the Session while the model runs; `select_session` compares the
    prepared Session snapshot in the fresh write transaction and fails closed.
    """

    with deps.session_factory() as session:
        try:
            timeline_id = events.resolve_timeline(
                session,
                deps.identifier_key,
                client_conversation_id=conversation_id,
                now=deps.now(),
            )
            prepared = deps.session_manager.prepare_classification(
                session,
                conversation_id=timeline_id,
                user_text=text,
                now=deps.now(),
                # Clarification answers are pinned by their source operation in
                # the write phase. They never need a semantic classifier.
                pinned_session_id=(
                    "clarification-pinned" if clarification_of is not None else None
                ),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    return deps.session_manager.resolve_classification(prepared)


def _anchor_chat(
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    clarification_of: str | None,
    start_new_session: bool,
    resolved_classification: ResolvedClassification,
) -> _AnchoredChat:
    """Persist request, encrypted payload and user event before model work."""

    with deps.session_factory() as session:
        try:
            for claim_attempt in range(2):
                try:
                    return run_write_transaction(
                        session,
                        lambda: _anchor_chat_in_transaction(
                            session,
                            deps,
                            auth,
                            key,
                            conversation_id,
                            text,
                            clarification_of,
                            start_new_session,
                            resolved_classification,
                        ),
                        attempts=8,
                    )
                except IntegrityError as exc:
                    # Usually SQLite serialises this as a snapshot conflict. If
                    # both consumers instead reach the unique retry-lineage
                    # constraint, the loser gets one fresh-state pass and then
                    # anchors an unbound retry. Do not mask other constraints.
                    retry_claim_conflict = (
                        is_finance_retry_request(text)
                        and "operations.retry_of_operation_id" in str(exc.orig)
                    )
                    if claim_attempt == 0 and retry_claim_conflict:
                        continue
                    raise
            raise AssertionError("unreachable")  # pragma: no cover
        except StaleOperationVersionError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    "clarification source was already answered by another request"
                ),
            ) from exc
        except Exception:
            session.rollback()
            raise


def _anchor_chat_in_transaction(
    session,
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    clarification_of: str | None,
    start_new_session: bool,
    resolved_classification: ResolvedClassification,
) -> _AnchoredChat:
    """Anchor one chat against the transaction's current database snapshot."""
    timeline_id = events.resolve_timeline(
        session,
        deps.identifier_key,
        client_conversation_id=conversation_id,
        now=deps.now(),
    )
    fingerprint = chat_request_fingerprint(
        conversation_id=timeline_id,
        text=text,
        clarification_of=clarification_of,
        start_new_session=start_new_session,
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

    if start_new_session:
        _abandon_pre_submit_operations_in_open_session(
            session,
            deps,
            conversation_id=timeline_id,
        )

    context = None
    retry_context = None
    retry_source = None
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
                    "clarification_of must name an operation waiting for clarification"
                ),
            )
        source_payload = open_chat_request(
            deps.keyring,
            request_id=source.request_id,
            envelope=source.api_request.encrypted_request_payload,
        )
        if source_payload.conversation_id != timeline_id:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="clarification must stay in the source conversation",
            )
        context = continuation_context(
            source_payload,
            source_operation_id=source.operation_id,
        )
        transition_operation(
            session,
            operation_id=source.operation_id,
            current_state=source.state,
            current_version=source.state_version,
            target_state="cancelled_pre_submit",
            now=deps.now(),
        )
    elif is_finance_retry_request(text):
        retry_source, retry_context = _eligible_finance_retry(
            session,
            deps,
            device_id=auth.device_id,
            conversation_id=timeline_id,
            now=deps.now(),
        )
        if retry_source is not None:
            operation.retry_of_operation_id = retry_source.operation_id

    payload = ChatRequestPayload(
        conversation_id=timeline_id,
        text=text,
        clarification_of=clarification_of,
        clarification_context=context,
        finance_retry_context=retry_context,
        start_new_session=start_new_session,
    )
    operation.api_request.encrypted_request_payload = seal_chat_request(
        deps.keyring,
        request_id=operation.request_id,
        payload=payload,
    )
    pinned = None
    if clarification_of is not None:
        pinned = _session_of_operation(session, clarification_of)
    elif retry_source is not None:
        pinned = _session_of_operation(session, retry_source.operation_id)
    decision = deps.session_manager.select_session(
        session,
        conversation_id=timeline_id,
        user_text=text,
        now=deps.now(),
        pinned_session_id=pinned,
        resolved_classification=resolved_classification,
        force_new_session=start_new_session,
    )
    turn_id = events.new_turn_id()
    if decision.is_boundary:
        events.append_event(
            session,
            deps.keyring,
            conversation_id=timeline_id,
            session_id=decision.session_id,
            turn_id=turn_id,
            event_type=(
                events.SESSION_BOUNDARY_CORRECTED
                if decision.relation_kind == "corrects_boundary"
                else events.SESSION_DIVIDER
            ),
            content={"reason": decision.reason},
            operation_id=None,
            now=deps.now(),
        )
    events.append_event(
        session,
        deps.keyring,
        conversation_id=timeline_id,
        session_id=decision.session_id,
        turn_id=turn_id,
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
    logger.info(
        "session boundary %s",
        json.dumps(decision.audit_record(), sort_keys=True),
    )
    return _AnchoredChat(operation.operation_id, operation.state)


def _abandon_pre_submit_operations_in_open_session(
    session,
    deps: AgentApiDeps,
    *,
    conversation_id: str,
) -> None:
    """Cancel every safely cancellable pending operation before an explicit reset.

    The caller is inside the same write transaction that later closes the old
    Session and anchors the new user event.  Any operation that might already
    have reached its source refuses the whole request, which prevents a divider
    from claiming that the user safely abandoned work whose outcome is unknown.
    """
    current = deps.session_manager.open_session(
        session, conversation_id=conversation_id
    )
    if current is None:
        return
    pending = (
        session.query(Operation)
        .join(
            ConversationEvent,
            ConversationEvent.operation_id == Operation.operation_id,
        )
        .filter(
            ConversationEvent.conversation_id == conversation_id,
            ConversationEvent.session_id == current.session_id,
            ~Operation.state.in_(TERMINAL_OPERATION_STATES),
        )
        .order_by(ConversationEvent.timeline_sequence)
        .all()
    )
    for pending_operation in pending:
        if not can_cancel_pre_submit(pending_operation.state):
            raise AppError(
                ErrorCode.PENDING_OPERATION_NOT_CANCELLABLE,
                internal_detail=(
                    "explicit session reset found an operation that may have "
                    f"reached its source: {pending_operation.operation_id}"
                ),
            )
    for pending_operation in pending:
        outcome = request_cancel(
            session,
            operation_id=pending_operation.operation_id,
            now=deps.now(),
        )
        if not outcome.cancelled:
            # A concurrent worker advanced the operation after the first pass.
            # The surrounding transaction rolls back any earlier cancellation.
            raise AppError(
                ErrorCode.PENDING_OPERATION_NOT_CANCELLABLE,
                internal_detail=(
                    "operation changed while opening a new Session: "
                    f"{pending_operation.operation_id}"
                ),
            )


def _process_chat(
    deps: AgentApiDeps,
    auth: AuthContext,
    operation_id: str,
) -> _ProcessedChat:
    """Run one accepted operation in a worker-owned database session."""

    with deps.session_factory() as session:
        try:
            operation = _owned_operation(
                session, operation_id, device_id=auth.device_id
            )
            if operation.state != "accepted":
                return _ProcessedChat(_operation_response(operation))
            payload = open_chat_request(
                deps.keyring,
                request_id=operation.request_id,
                envelope=operation.api_request.encrypted_request_payload,
            )
            anchor = _anchor_event(session, operation.operation_id)
            turn_context = _context_factory(
                deps,
                auth,
                session,
                payload=payload,
                anchor=anchor,
            )
            result = run_operation(
                session,
                operation,
                build_context=turn_context,
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
            # The result joins the user message's own turn and Session; a
            # Session decision is made once per message, at anchoring time.
            events.append_event(
                session,
                deps.keyring,
                conversation_id=payload.conversation_id,
                session_id=anchor.session_id,
                turn_id=anchor.turn_id,
                event_type=events.OPERATION_RESULT,
                content=_result_content(result),
                operation_id=operation.operation_id,
                now=deps.now(),
            )
            session.commit()
            session.refresh(operation)
            compact_session_id = (
                anchor.session_id
                if (
                    deps.compact_session is not None
                    and turn_context.envelope is not None
                    and turn_context.envelope.compaction_requested
                    and (
                        not turn_context.envelope.checkpoint_rebuild_required
                        or is_terminal(operation.state)
                    )
                )
                else None
            )
            return _ProcessedChat(
                _operation_response(operation, extra=_transient(result)),
                compact_session_id=compact_session_id,
            )
        except StaleOperationVersionError:
            session.rollback()
            operation = _owned_operation(
                session, operation_id, device_id=auth.device_id
            )
            return _ProcessedChat(_operation_response(operation))
        except Exception:
            session.rollback()
            raise


@dataclass(frozen=True)
class _Anchor:
    """The persisted user message this operation belongs to."""

    conversation_id: str
    session_id: str
    turn_id: str
    event_id: str


def _anchor_event(session, operation_id: str) -> _Anchor:
    """The Session, turn and event the operation's user message was written into."""
    row = session.execute(
        text_clause(
            "SELECT conversation_id, session_id, turn_id, event_id "
            "FROM conversation_events "
            "WHERE operation_id = :oid ORDER BY timeline_sequence LIMIT 1"
        ),
        {"oid": operation_id},
    ).one_or_none()
    if row is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="operation has no anchoring timeline event",
        )
    return _Anchor(
        conversation_id=row[0],
        session_id=row[1],
        turn_id=row[2],
        event_id=row[3],
    )


class _TurnContext:
    """Assembles this turn's envelope and remembers what it decided.

    The Compactor's trigger is a property of the assembled input (§7.3: crossing
    the soft limit triggers compaction, never a Session split), so the caller
    needs the envelope after the turn -- and only the envelope, not a second
    measurement that could disagree with it.
    """

    def __init__(self, build: Callable[[], ContextEnvelope]) -> None:
        self._build = build
        self.envelope: ContextEnvelope | None = None

    def __call__(self) -> ContextEnvelope:
        self.envelope = self._build()
        return self.envelope


def _compact_session_in_background(
    deps: AgentApiDeps,
    session_id: str,
) -> None:
    """Run post-response compaction in a worker-owned database session."""

    if deps.compact_session is None:
        return
    with deps.session_factory() as session:
        try:
            deps.compact_session(session, session_id)
        except Exception:
            session.rollback()
            raise


def _context_factory(
    deps: AgentApiDeps,
    auth: AuthContext,
    session,
    *,
    payload: ChatRequestPayload,
    anchor: _Anchor,
) -> _TurnContext:
    """Bind this turn's assembly, to be run when the model is about to be asked.

    The envelope is built from the persisted anchor event rather than from the
    request body: the archived message is the one the model must answer, and a
    caller-supplied string that disagrees with it is refused by the builder.
    """

    def build() -> ContextEnvelope:
        return deps.build_envelope(
            session,
            auth,
            conversation_id=payload.conversation_id,
            session_id=anchor.session_id,
            current_event_id=anchor.event_id,
            user_text=payload.text,
            clarification_context=payload.clarification_context,
            finance_retry_context=payload.finance_retry_context,
        )

    return _TurnContext(build)


_SAFE_FINANCE_RETRY_FAILURES = frozenset(
    {*MODEL_RETRYABLE_FAILURE_REASONS, ErrorCode.BOOKKEEPING_TOOL_REQUIRED.value}
)
_FINANCE_RETRY_LOOKBACK = timedelta(hours=24)


def _eligible_finance_retry(
    session,
    deps: AgentApiDeps,
    *,
    device_id: str,
    conversation_id: str,
    now: datetime,
) -> tuple[Operation | None, FinanceRetryContext | None]:
    """Return the newest unconsumed Finance failure known to have made no call.

    `failed_safe` alone is insufficient: a policy refusal, a dispatched write,
    or a manual-review outcome must never be replayed. The closed reason set,
    `tool IS NULL`, sealed request inspection and one-shot lineage jointly make
    the retry narrower than the user's short phrase.
    """
    candidates = (
        session.query(Operation)
        .join(ApiRequest, Operation.request_id == ApiRequest.request_id)
        .join(
            ConversationEvent,
            ConversationEvent.operation_id == Operation.operation_id,
        )
        .filter(
            ApiRequest.device_id == device_id,
            ConversationEvent.conversation_id == conversation_id,
            ConversationEvent.event_type == events.USER_MESSAGE,
            Operation.updated_at >= now - _FINANCE_RETRY_LOOKBACK,
        )
        .order_by(ConversationEvent.timeline_sequence.desc())
        .yield_per(100)
    )
    for source in candidates:
        payload = open_chat_request(
            deps.keyring,
            request_id=source.request_id,
            envelope=source.api_request.encrypted_request_payload,
        )
        if payload.conversation_id != conversation_id:
            continue
        retry = payload.finance_retry_context
        if retry is not None:
            original = retry.original_user_text
            exchanges = retry.completed_exchanges
        elif payload.clarification_context is not None:
            clarification = payload.clarification_context
            original = clarification.original_user_text
            exchanges = (
                *clarification.completed_exchanges,
                ClarificationExchange(
                    question=clarification.question,
                    answer=payload.text,
                ),
            )
        else:
            original = payload.text
            exchanges = ()
        finance_related = (
            is_bookkeeping_write_request(original)
            or payload.finance_retry_context is not None
            or payload.clarification_context is not None
            or is_finance_retry_request(payload.text)
            or (source.tool is not None and source.tool.startswith("finance."))
        )
        if not finance_related:
            continue
        # Stop at the newest Finance-related operation. A later successful,
        # dispatched, parked or outcome-unknown turn is a hard barrier: looking
        # behind it for an older failure could duplicate a write. Ordinary chat
        # between the failure and the explicit retry is intentionally ignored.
        already_consumed = (
            session.query(Operation.operation_id)
            .filter(Operation.retry_of_operation_id == source.operation_id)
            .first()
        )
        if (
            source.state != "failed_safe"
            or source.tool is not None
            or source.failure_reason not in _SAFE_FINANCE_RETRY_FAILURES
            or already_consumed is not None
            or not is_bookkeeping_write_request(original)
        ):
            return None, None
        return source, FinanceRetryContext(
            original_user_text=original,
            completed_exchanges=tuple(exchanges),
            source_operation_id=source.operation_id,
            source_failure_reason=source.failure_reason or "",
        )
    return None, None


def _session_of_operation(session, operation_id: str) -> str | None:
    row = session.execute(
        text_clause(
            "SELECT session_id FROM conversation_events "
            "WHERE operation_id = :oid ORDER BY timeline_sequence LIMIT 1"
        ),
        {"oid": operation_id},
    ).scalar_one_or_none()
    return row


def _load_operation_response(
    deps: AgentApiDeps, operation_id: str, device_id: str
) -> JSONResponse:
    with deps.session_factory() as session:
        operation = _owned_operation(session, operation_id, device_id=device_id)
        return _operation_response(operation)


def _process_manual_resolution(
    deps: AgentApiDeps,
    auth: AuthContext,
    operation_id: str,
    resolution: str,
) -> JSONResponse:
    with deps.session_factory() as session:
        def work():
            outcome = resolve_manual_review(
                session,
                deps.keyring,
                operation_id=operation_id,
                device_id=auth.device_id,
                resolution=resolution,
                now=deps.now(),
            )
            if outcome.recorded:
                anchor = _anchor_event(session, outcome.operation.operation_id)
                append_resolution_event(
                    session,
                    deps.keyring,
                    operation=outcome.operation,
                    conversation_id=anchor.conversation_id,
                    session_id=anchor.session_id,
                    turn_id=anchor.turn_id,
                    resolution=resolution,
                    now=deps.now(),
                )
            # Deliberately NOT `_operation_response`. That body is the frozen
            # `chat_receipt_projection_v2` contract the iOS client reads from a
            # shared vector file, and a manual resolution is not a chat receipt:
            # widening the receipt would make every existing case carry a field
            # about a surface that does not display it yet, and would drag a
            # cross-language contract bump into a server-only change. This
            # endpoint answers about the resolution it just recorded.
            operation = outcome.operation
            return JSONResponse(
                {
                    "operation_id": operation.operation_id,
                    "state": operation.state,
                    "manual_resolution": operation.manual_resolution,
                    "manual_resolved_at": to_rfc3339(operation.manual_resolved_at),
                    "recorded": outcome.recorded,
                }
            )

        return _commit(session, work)


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
            # Resolve Timeline ownership only after the decision contract has
            # accepted or replayed the request. In particular, a reused key for
            # another check must remain the idempotency layer's 409 rather than
            # being pre-empted by a "no source" lookup.
            source = _duplicate_source_operation(
                session, duplicate_check_id, auth.device_id
            )
            anchor = _anchor_event(session, source.operation_id)
            new_op = outcome.new_operation
            if new_op is not None and new_op.state == "accepted":
                run_operation(
                    session,
                    new_op,
                    # A `write anyway` operation carries its resolved intent and
                    # never reaches the model, so it assembles no context.
                    build_context=None,
                    interpreter=deps.build_interpreter(auth),
                    dispatcher=deps.build_dispatcher(auth, new_op.trace_id),
                    authorize=deps.build_authorizer(auth),
                    keyring=deps.keyring,
                    now=deps.now,
                )
            target = new_op if new_op is not None else _find_by_check(
                session, duplicate_check_id, auth.device_id
            )
            _append_duplicate_decision_events(
                session,
                deps.keyring,
                source=source,
                target=target,
                anchor=anchor,
                duplicate_check_id=duplicate_check_id,
                decision=decision,
                now=deps.now(),
            )
            return _operation_response(target)

        # `write anyway` dispatches a real Finance write inside `work`.
        return _commit(session, work, retry=False)


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

        # Opening a card reads live ledger values through the control plane.
        return _commit(session, work, retry=False)


def _commit(session, work: Callable[[], Any], *, retry: bool = True):
    """Run one endpoint's database work and commit it.

    `retry` is on by default because an endpoint that reads and then writes
    cannot keep its snapshot once anyone else has committed; SQLite refuses the
    upgrade rather than waiting, and re-running the handler against fresh state
    is the only correct answer. Endpoints that reach an external service inside
    `work` pass `retry=False`: repeating them would repeat that call.
    """
    if not retry:
        try:
            response = work()
            session.commit()
            return response
        except Exception:
            session.rollback()
            raise
    return run_write_transaction(session, work)


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


def _duplicate_source_operation(
    session, duplicate_check_id: str, device_id: str
) -> Operation:
    """Return the chat operation whose Timeline turn produced this check.

    A successful `write_anyway` creates a second operation under the same check.
    Only the original has a `user_message` anchor, so joining that fact avoids
    choosing the override on an idempotent replay.
    """
    operation = (
        session.query(Operation)
        .join(Operation.api_request)
        .join(
            ConversationEvent,
            ConversationEvent.operation_id == Operation.operation_id,
        )
        .filter(
            Operation.duplicate_check_id == duplicate_check_id,
            Operation.api_request.has(device_id=device_id),
            ConversationEvent.event_type == events.USER_MESSAGE,
        )
        .one_or_none()
    )
    if operation is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="no anchored operation for this duplicate check",
        )
    return operation


def _append_duplicate_decision_events(
    session,
    keyring: KeyRing,
    *,
    source: Operation,
    target: Operation,
    anchor: _Anchor,
    duplicate_check_id: str,
    decision: str,
    now: datetime,
) -> None:
    """Persist one decision marker and the operation outcome it produced.

    The marker is the idempotency witness for Timeline projection too: if the
    HTTP decision is replayed, both events already exist and neither is appended
    twice. They share one final transaction, so the marker can never claim a
    result event was persisted when it was not.
    """
    already_recorded = (
        session.query(ConversationEvent)
        .filter(
            ConversationEvent.operation_id == source.operation_id,
            ConversationEvent.event_type == events.DUPLICATE_DECISION,
        )
        .first()
        is not None
    )
    if already_recorded:
        return

    session.refresh(target)
    events.append_event(
        session,
        keyring,
        conversation_id=anchor.conversation_id,
        session_id=anchor.session_id,
        turn_id=anchor.turn_id,
        event_type=events.DUPLICATE_DECISION,
        content={
            "duplicate_check_id": duplicate_check_id,
            "decision": decision,
        },
        operation_id=source.operation_id,
        now=now,
    )
    events.append_event(
        session,
        keyring,
        conversation_id=anchor.conversation_id,
        session_id=anchor.session_id,
        turn_id=anchor.turn_id,
        event_type=events.OPERATION_RESULT,
        content=_operation_event_content(target),
        operation_id=target.operation_id,
        now=now,
    )


def _operation_event_content(operation: Operation) -> dict[str, Any]:
    projection = _operation_projection(operation)
    return {
        name: projection[name]
        for name in (
            "state",
            "record_id",
            "answer",
            "clarification",
            "duplicate_check_id",
            "duplicate_existing",
            "failure_reason",
        )
        if projection.get(name) is not None
    }


def _required(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} is required",
        )
    return value


def _optional_bool(body: dict[str, Any], field: str, *, default: bool) -> bool:
    value = body.get(field)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} must be a boolean",
        )
    return value


def _optional_int(raw: str | None) -> int | None:
    """Parse a query integer strictly; a malformed one is not a default."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT, internal_detail="limit must be an integer"
        ) from exc


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
    for name in ("answer", "clarification", "duplicate_existing"):
        value = getattr(result, name, None)
        if value is not None:
            fields[name] = value
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
            operation.state in {"succeeded", "needs_manual_review"}
            and operation.tool in _RECORD_ID_RESULT_TOOLS
        ):
            projection["record_id"] = operation.safe_result
        elif operation.state == "succeeded":
            projection["answer"] = operation.safe_result
    return projection


def _result_content(result) -> dict[str, Any]:
    content: dict[str, Any] = {"state": result.state}
    for name in (
        "record_id", "answer", "clarification", "duplicate_check_id",
        "duplicate_existing", "failure_reason",
    ):
        value = getattr(result, name, None)
        if value is not None:
            content[name] = value
    return content


def _error_response(error: AppError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(error.code, 500)
    envelope = error.to_envelope().model_dump(mode="json")
    return JSONResponse({"error": envelope}, status_code=status)
