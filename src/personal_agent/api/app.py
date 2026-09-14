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
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from fastapi import FastAPI, Request
from fastapi import Response
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from sqlalchemy import text as text_clause
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from personal_agent.api import events
from personal_agent.api.chat_anchor import (
    bind_chat_images,
    resolve_chat_images,
    resolved_parts,
)
from personal_agent.api.chat_parts import (
    ImageRefPart,
    Parts,
    parse_chat_parts,
    parts_text,
)
from personal_agent.api.calendar_issue import (
    may_override,
    override_fingerprint,
    override_key,
)
from personal_agent.api.calendar_query_projection import (
    CalendarQueryProjectionError,
    decode_calendar_query_projection,
    summarise_calendar_projection,
)
from personal_agent.api.device_action_projection import open_device_action
from personal_agent.api.finance_query_projection import (
    FinanceQueryProjectionError,
    decode_finance_query_projection,
    summarise_query_projection,
)
from personal_agent.api.finance_record_projection import open_expense_record
from personal_agent.api.manual_review import (
    append_resolution_event,
    resolve_manual_review,
)
from personal_agent.api.operation_request import (
    OperationRequestError,
    open_operation_request,
    seal_operation_request,
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
    plan_operations,
    request_cancel,
    transition_operation,
)
from personal_agent.api.operation_state import (
    StaleOperationVersionError,
    can_cancel_pre_submit,
    is_terminal,
)
from personal_agent.api.intent import WriteIntent, seal_intent
from personal_agent.api.orchestrator import (
    Authorizer,
    Dispatcher,
    Interpreter,
    resume_action_plan,
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
    describes_request,
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
    PreparedClassification,
    ResolvedClassification,
    SessionManager,
)
from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.media.locking import MediaLockError, media_locks
from personal_agent.media.store import MediaStore
from personal_agent.media.uploads import (
    CHAT_IMAGE_PURPOSE,
    CompleteOutcome,
    CompletedUpload,
    CreatedUpload,
    FetchedMedia,
    MediaBusyError,
    MediaError,
    MediaGoneError,
    MediaIncompleteUploadError,
    MediaLimits,
    MediaNotReadyError,
    MediaNotFoundError,
    MediaRejectedError,
    UploadDeclaration,
    UploadReceipt,
    complete_upload,
    delete_media as decide_media_deletion,
    read_media,
    receive_upload,
    start_upload,
)
from personal_agent.api.media_read import read_authorized_images
from personal_agent.runtime.bookkeeping_intent import (
    is_bookkeeping_write_request,
    is_finance_retry_request,
)
from personal_agent.runtime.modality import ImageCapability, image_capability
from personal_agent.runtime.model_input import InputPart, TextInputPart
from personal_agent.diagnostics import transcript
from personal_agent.diagnostics.transcript import NullRecorder, Recorder, TurnIdentity
from personal_agent.storage.models import (
    DEVICE_REPORT_WRITES,
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
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.tool_ir import (
    CLIENT_WIRE_VERSION_HEADER,
    DEFAULT_CLIENT_WIRE_VERSION,
    DEVICE_EXECUTED_TOOL_NAMES,
    TOOL_CONTRACTS,
    SCOPE_CALENDAR_READ,
    client_supports_wire_version,
    domain_of_tool,
    parse_client_wire_version,
)


logger = logging.getLogger(__name__)

#: The closed sync body, taken from the IR's `calendar.ingest_events` input
#: schema. Validated at the route edge with the same validator the bridge
#: applies inside — deliberately twice-gated, because the route must refuse a
#: whole malformed batch before it reaches any governed machinery, and because
#: the bridge is not composed in offline API tests.
_SYNC_INGEST_CONTRACT: Final[dict[str, Any]] = next(
    contract.model_input_schema
    for contract in TOOL_CONTRACTS
    if contract.name == "calendar.ingest_events"
)


@dataclass(frozen=True)
class AuthContext:
    device_id: str
    scopes: tuple[str, ...]
    allowed_tools_version: str
    #: The action semantics this caller says it implements (design 2.5.1).
    #: Read from the request header and never persisted: reading it per request
    #: is what covers all three delivery doors at once, because the 200 reply,
    #: the by-id poll and a replay are each a device-authenticated request.
    client_wire_version: int

    @property
    def chat_runtime_v2(self):
        return self.client_wire_version >= 4


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
        input_parts: tuple[InputPart, ...] = (),
    ) -> ContextEnvelope: ...


def _images_never_enabled() -> ImageCapability:
    """The capability of a service that composed no model or no media.

    Not a stand-in for the real composition -- `composition.py` always supplies
    one -- but the honest default for the many `AgentApiDeps` built by hand in
    tests, and for any future caller that wires the dataclass directly. It
    names no model and asserts no approval, so it can only ever answer "off".
    """
    return image_capability(
        master=False,
        provider="",
        model_id="",
        media_ready=False,
    )


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
    v2_device_ids: frozenset[str] = frozenset()
    v2_execution_enabled: bool = True
    v2_model_factory: Callable | None = None
    v2_search_adapter: Any = None
    v2_search_allowed: Callable[[AuthContext, str], bool] = lambda auth, tool: False
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
    #: The full-fidelity turn transcript. The default records nothing, so a
    #: composition that does not configure a transcript directory -- and every
    #: offline test -- behaves exactly as before.
    recorder: Recorder = field(default_factory=NullRecorder)
    #: `#18`. The encrypted object store and §4.3's ceilings. Both are `None`
    #: together or neither: a store with no limits has no bound to enforce
    #: (§4.3 makes the ceilings configuration, and a defaulted ceiling is a
    #: policy nobody chose), and limits with no store cannot store anything.
    #: `None` means the media surface is not composed, and every route below
    #: refuses rather than half-running -- the same shape `CAP-001`'s
    #: `enrollment_manifest_version = None` uses.
    media_store: MediaStore | None = None
    media_limits: MediaLimits | None = None
    #: `#13`. §8's composed switch, **called** rather than held as a value.
    #: §8 requires the server to re-validate rather than let a client's stale
    #: cached capability through, and a verdict computed once at boot would
    #: make that a statement about a number nothing can move. It is one
    #: callable so that the entry guard, the anchor that binds an image to a
    #: message, and `/v1/capabilities` are three reads of one source instead of
    #: three sources -- §8's "客户端 capability 和服务端入口同源计算".
    #:
    #: The default is closed and says so. A deployment that wired nothing has
    #: not enabled images, and an `AgentApiDeps` built by a test must not
    #: accidentally advertise a capability the test never composed.
    image_capability: Callable[[], ImageCapability] = _images_never_enabled
    #: The governed calendar mirror ingest, composed once over the real bridge
    #: in production. It receives the authenticated device's `AuthContext` and
    #: the closed sync body, signs a Host Context naming *that* device, and
    #: merges the batch into the mirror. `None` means calendar sync is not
    #: composed and the route refuses rather than serving an unbound mirror.
    sync_ingest: Callable[[AuthContext, dict[str, Any]], dict[str, Any]] | None = None
    #: The keyring that seals an issued device action onto its operation row
    #: (review R6, 2026-09-08). Production composes the data keyring; the
    #: separate field keeps the seal at a deliberate, visible seam rather than
    #: letting every `keyring` call site implicitly gain write access to the
    #: device-action column. `None` means device actions may not be issued,
    #: and `run_operation` refuses one loudly instead of parking an
    #: operation whose action nobody could ever deliver.
    action_keyring: KeyRing | None = None

    def __post_init__(self) -> None:
        if self.sync_wait_seconds <= 0 or self.sync_wait_seconds > 30.0:
            raise ValueError("sync_wait_seconds must be within (0, 30]")
        if self.session_manager is None:
            self.session_manager = SessionManager(self.context_config)


_STATUS_BY_CODE = {
    ErrorCode.CLIENT_UPGRADE_REQUIRED: 409,
    ErrorCode.RUNTIME_UNAVAILABLE: 503,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.SCOPE_DENIED: 403,
    ErrorCode.TOOL_NOT_ALLOWLISTED: 403,
    ErrorCode.HOST_CONTEXT_MISMATCH: 403,
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.CALENDAR_SYNC_RESET_REQUIRED: 400,
    # `CAP-001`: an id that names no Timeline here. A `404` says so without
    # confirming whether that id exists anywhere, and without ever being read as
    # "so create it".
    ErrorCode.TIMELINE_MISMATCH: 404,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.PENDING_OPERATION_NOT_CANCELLABLE: 400,
    # The progress trail distinguishes "this key names no operation *yet*" from
    # every other refusal so the client keeps polling instead of concluding
    # anything about the write.
    ErrorCode.OPERATION_NOT_ANCHORED: 400,
    # `#18`. Three different retry instructions, so three different statuses:
    # wait and re-ask (`MEDIA_NOT_READY`), wait and re-send the same bytes
    # (`MEDIA_BUSY`), or stop (`MEDIA_GONE`). Collapsing them into one 4xx is
    # what makes a client poll a tombstone or give up on a busy lock.
    ErrorCode.MEDIA_NOT_READY: 409,
    ErrorCode.MEDIA_BUSY: 409,
    ErrorCode.MEDIA_NOT_FOUND: 404,
    ErrorCode.MEDIA_GONE: 410,
}

_MAX_JSON_BODY_BYTES = 64 * 1024
#: The calendar sync route carries a whole window of events, and its own
#: batching budget is 128 KiB (design 6): the global 64 KiB cap would refuse a
#: batch the device is *required* to send. Raised only for this route -- every
#: other body stays on the global limit.
_MAX_SYNC_BODY_BYTES = 512 * 1024

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

#: The governed read tools whose `safe_result` is a structured query projection
#: rather than a prose answer. Like the write set above, this is derived from
#: the IR, never hand-listed: a hand-listed query tool would drift silently the
#: day a second governed read ships, and the projection would then emit its raw
#: canonical JSON as `answer` -- exactly the bug this change exists to fix.
#:
#: It is narrowed to the tools whose output contract actually *is* the expense
#: query projection: a governed read like `meta.capabilities` is a read but not
#: a query, and must keep the plain `answer` path. The discriminant is the
#: output contract's `metric` const, so a tool that merely happens to be a read
#: cannot fall into the strict decoder and fail closed on a valid result.
_QUERY_RESULT_TOOLS = frozenset(
    contract.name
    for contract in TOOL_CONTRACTS
    if contract.effect == "read"
    and contract.enabled
    and contract.output_schema.get("properties", {}).get("metric", {}).get("const")
    == "personal_spend_total_cny"
)

#: Same discipline for the calendar mirror read (review R4): a governed read
#: whose `safe_result` is the calendar projection, keyed on the output
#: contract's `source_system` const the way the Finance set keys on `metric`.
_CALENDAR_QUERY_RESULT_TOOLS = frozenset(
    contract.name
    for contract in TOOL_CONTRACTS
    if contract.effect == "read"
    and contract.enabled
    and contract.output_schema.get("properties", {}).get("source_system", {}).get(
        "const"
    )
    == "apple_calendar_mirror"
)

#: The device-executed tools whose issued action the operation projection
#: hands to the phone while the operation is parked (review R6). Derived from
#: the IR's `executor` field like every other executor split, never
#: hand-listed, so a second device tool is delivered by the same branch
#: automatically.
_DEVICE_EXECUTED_TOOLS = DEVICE_EXECUTED_TOOL_NAMES

#: Which domain each tool belongs to is the IR's answer, not this file's
#: (`domain_of_tool`). A client picks the manual-review card by domain — a
#: calendar write cannot be checked in the ledger — so the domain has to be a
#: fact the server states, derived from the tool's own contract like every
#: other executor and risk split here. A tool with no contract has no domain
#: and projects as null rather than falling into a default. The Timeline marker
#: asks the same question when it freezes a resolution, and two expressions of
#: it would be two things to keep in step.


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
    boundary_tasks: set[threading.Thread] = set()
    boundary_operation_ids: set[str] = set()
    boundary_prepared: dict[str, PreparedClassification] = {}
    boundary_lock = threading.Lock()

    async def drain_background_tasks() -> None:
        """Let accepted operations and their follow-up compactions finish.

        Production ASGI shutdown runs this before composition closes the MCP and
        control clients. Operation callbacks may enqueue compaction, so drain the
        operation set first, then the asynchronous boundary work and finally the
        compaction set it produced.  A split has to settle before compaction can
        bind a checkpoint to the Session membership it observed.
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
        with boundary_lock:
            boundary_workers = tuple(boundary_tasks)
        await asyncio.to_thread(_join_boundary_workers, boundary_workers)
        # Task done callbacks enqueue compaction with call_soon semantics. Give
        # those callbacks one loop turn before snapshotting the compaction set.
        await asyncio.sleep(0)
        await bounded(tuple(compaction_tasks))

    async def resume_v2(stop):
        from personal_agent.api.runtime_v2 import resumable
        while not stop.is_set():
            try:
                for operation_id, auth in await asyncio.to_thread(resumable,deps):
                    if operation_id in operation_tasks:continue
                    task=asyncio.create_task(asyncio.to_thread(_process_chat,deps,auth,operation_id))
                    operation_tasks[operation_id]=task
                    task.add_done_callback(lambda done, op=operation_id: forget_task(op,done))
            except Exception:
                # Do not serialize provider exceptions or private snapshots.
                logger.warning("v2 recovery discovery failed; retrying")
            try:await asyncio.wait_for(stop.wait(),timeout=5)
            except TimeoutError:pass

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop=asyncio.Event()
        from personal_agent.api.runtime_v2 import recovery_needed
        recovery=asyncio.create_task(resume_v2(stop)) if await asyncio.to_thread(recovery_needed,deps) else None
        try:yield
        finally:
            stop.set()
            if recovery is not None: await recovery
            await drain_background_tasks()

    app = FastAPI(lifespan=lifespan)
    # The service composition runs one API process. Hold admission across both
    # receive and the disk worker, including cancellation of the HTTP request.
    upload_slots = threading.BoundedSemaphore(
        (deps.media_limits.max_concurrent_uploads or 1) if deps.media_limits else 1
    )
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

    def schedule_compaction(session_id: str, operation_id: str) -> None:
        task = asyncio.create_task(
            asyncio.to_thread(
                _compact_session_in_background,
                deps,
                session_id,
                operation_id,
            )
        )
        compaction_tasks.add(task)
        task.add_done_callback(finish_compaction)

    def schedule_boundary(
        prepared: PreparedClassification,
        operation_id: str,
        processed: _ProcessedChat,
    ) -> None:
        worker: threading.Thread

        def classify_then_apply() -> None:
            target_session_id: str | None = None
            try:
                resolved = _resolve_chat_boundary(deps, prepared, operation_id)
                target_session_id = _apply_chat_boundary(
                    deps, operation_id, resolved
                )
                if target_session_id is not None:
                    logger.info(
                        "asynchronous Session boundary applied operation_id=%s",
                        operation_id,
                    )
            except Exception:
                logger.exception(
                    "asynchronous Session boundary classification failed"
                )
            finally:
                try:
                    if processed.compact_session_id is not None:
                        _compact_session_in_background(
                            deps,
                            target_session_id or processed.compact_session_id,
                            operation_id,
                        )
                except Exception:
                    logger.exception(
                        "compaction after asynchronous Session boundary failed"
                    )
                finally:
                    with boundary_lock:
                        boundary_operation_ids.discard(operation_id)
                        boundary_tasks.discard(worker)

        worker = threading.Thread(
            target=classify_then_apply,
            name=f"session-boundary-{operation_id}",
            daemon=True,
        )
        with boundary_lock:
            boundary_tasks.add(worker)
        worker.start()

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
                prepared = boundary_prepared.pop(operation_id, None)
                if prepared is not None:
                    schedule_boundary(prepared, operation_id, done.result())
                elif session_id is not None:
                    with boundary_lock:
                        boundary_pending = operation_id in boundary_operation_ids
                    if not boundary_pending:
                        schedule_compaction(session_id, operation_id)

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
            client_wire_version=parse_client_wire_version(
                request.headers.get(CLIENT_WIRE_VERSION_HEADER)
            ),
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

    @app.post("/v1/media/uploads")
    async def post_media_upload(request: Request):
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        key = idempotency_key(request)
        return await asyncio.to_thread(
            _start_media_upload, deps, auth, key, body
        )

    @app.put("/v1/media/content/{media_id}")
    async def put_media_content(media_id: str, request: Request):
        # Authentication first, then the body -- the same order every other
        # route uses, and the reason the binary reader below can assume it is
        # reading for a caller that is entitled to upload at all.
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        store, limits = _media_ready(deps)
        if not upload_slots.acquire(blocking=False):
            raise AppError(ErrorCode.MEDIA_BUSY, internal_detail="upload concurrency limit")
        handed_off = False
        try:
            raw = await asyncio.wait_for(
                _binary_body(request, ceiling=limits.max_content_bytes),
                timeout=limits.claim_ttl.total_seconds(),
            )
            def receive():
                try:
                    return _receive_media_content(deps, auth, media_id, raw)
                finally:
                    upload_slots.release()
            worker = asyncio.create_task(asyncio.to_thread(receive))
            handed_off = True
            return await asyncio.shield(worker)
        finally:
            if not handed_off:
                upload_slots.release()

    @app.post("/v1/media/uploads/{media_id}/complete")
    async def post_media_complete(media_id: str, request: Request):
        # No body, and so no `_json_body`: §5.2's complete carries nothing the
        # server needs -- everything it verifies is already sealed -- and
        # requiring an empty JSON object would refuse the natural shape of the
        # request for no gain.
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        return await asyncio.to_thread(_complete_media_upload, deps, auth, media_id)

    @app.get("/v1/media/{media_id}")
    async def get_media(media_id: str, request: Request):
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        return await asyncio.to_thread(_fetch_media, deps, auth, media_id)

    @app.delete("/v1/media/{media_id}")
    async def delete_media(media_id: str, request: Request):
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        return await asyncio.to_thread(_delete_media, deps, auth, media_id)

    @app.post("/v1/chat/messages")
    async def post_message(request: Request):
        # Authentication stays ahead of body parsing, while SQLite and the
        # model/dispatcher run outside the event loop.
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        key = idempotency_key(request)
        conversation_id = _required(body, "conversation_id")
        parts, text = _chat_request(body, deps)
        clarification_of = _optional_operation_id(body, "clarification_of")
        start_new_session = _optional_bool(body, "start_new_session", default=False)
        if start_new_session and clarification_of is not None:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    "start_new_session cannot be combined with clarification_of"
                ),
            )

        prepared_classification: PreparedClassification | None = None
        anchored = await asyncio.to_thread(
            _preflight_chat_replay,
            deps,
            auth,
            key,
            conversation_id,
            text,
            parts,
            clarification_of,
            start_new_session,
        )
        if anchored is None:
            # This is an explicit, user-confirmed boundary. A classifier must
            # not spend a model call or be allowed to weaken that instruction.
            if start_new_session or (auth.chat_runtime_v2 and auth.device_id in deps.v2_device_ids and deps.v2_execution_enabled):
                resolved_classification = ResolvedClassification(
                    expected_session_id=None,
                    expected_last_event_at=None,
                    expected_timeline_sequence=0,
                    outcome=None,
                )
            else:
                prepared_classification = await asyncio.to_thread(
                    _prepare_chat_boundary,
                    deps,
                    conversation_id,
                    text,
                    clarification_of,
                )
                resolved_classification = ResolvedClassification(
                    expected_session_id=prepared_classification.expected_session_id,
                    expected_last_event_at=prepared_classification.expected_last_event_at,
                    expected_timeline_sequence=(
                        prepared_classification.expected_timeline_sequence
                    ),
                    outcome=None,
                )
            anchored = await asyncio.to_thread(
                _anchor_chat,
                deps,
                auth,
                key,
                conversation_id,
                text,
                parts,
                clarification_of,
                start_new_session,
                resolved_classification,
            )
        if anchored.state != "accepted":
            response = await asyncio.to_thread(
                _load_operation_response,
                deps,
                auth,
                anchored.operation_id,
            )
            return await asyncio.to_thread(
                _record_operation_http_response,
                deps,
                anchored.operation_id,
                auth.device_id,
                response,
                delivery="chat_replay",
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
            if (
                not start_new_session
                and clarification_of is None
                and prepared_classification is not None
                and prepared_classification.request is not None
            ):
                boundary_prepared[anchored.operation_id] = prepared_classification
                with boundary_lock:
                    boundary_operation_ids.add(anchored.operation_id)
            task.add_done_callback(
                lambda done, operation_id=anchored.operation_id: forget_task(
                    operation_id, done
                )
            )
        try:
            processed = await asyncio.wait_for(
                asyncio.shield(task), timeout=deps.sync_wait_seconds
            )
            return await asyncio.to_thread(
                _record_operation_http_response,
                deps,
                anchored.operation_id,
                auth.device_id,
                processed.response,
                delivery="chat_sync",
            )
        except TimeoutError:
            # The worker owns its session and continues. The client polls this
            # durable operation id; timeout never means the write was cancelled.
            response = JSONResponse(
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
            return await asyncio.to_thread(
                _record_operation_http_response,
                deps,
                anchored.operation_id,
                auth.device_id,
                response,
                delivery="chat_detached",
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
            response = await asyncio.to_thread(
                _load_operation_response,
                deps,
                auth,
                anchored.operation_id,
            )
            return await asyncio.to_thread(
                _record_operation_http_response,
                deps,
                anchored.operation_id,
                auth.device_id,
                response,
                delivery="chat_worker_failed",
            )

    @app.get("/v1/operations/{operation_id}")
    async def get_operation_status(operation_id: str, request: Request):
        authenticated_device_id: str | None = None
        with deps.session_factory() as session:
            def work():
                nonlocal authenticated_device_id
                auth = authenticate(request, session)
                authenticated_device_id = auth.device_id
                operation = _owned_operation(
                    session, operation_id, device_id=auth.device_id
                )
                return _operation_response(
                    deps.keyring,
                    operation,
                    client_wire_version=auth.client_wire_version,
                )

            response = _commit(session, work)
        return _record_operation_http_response(
            deps,
            operation_id,
            _authenticated_device(authenticated_device_id),
            response,
            delivery="operation_poll",
        )

    @app.get("/v1/operations/by-key/{path_key}")
    async def get_operation_by_key(path_key: str, request: Request):
        # The progress trail's poll: the client holds its own idempotency key
        # through the whole 30-second chat POST, so it can ask about the
        # operation *before* the POST answers with an operation_id. Read-only,
        # and the ownership rule is exactly `_owned_operation`'s: the operation
        # must belong to an api_request this device made. A key nothing has
        # anchored yet is a distinguishable `OPERATION_NOT_ANCHORED`, which the
        # client treats as "keep polling", never as a conclusion about a write.
        #
        # The path variable is `path_key`, not `idempotency_key`: a parameter
        # with the latter name would shadow the `idempotency_key` header parser
        # for the whole function body.
        authenticated_device_id: str | None = None
        anchored_operation_id: str | None = None
        with deps.session_factory() as session:
            def work():
                nonlocal authenticated_device_id, anchored_operation_id
                auth = authenticate(request, session)
                authenticated_device_id = auth.device_id
                # The key arrives in the path, not in a header, so the same
                # canonical-UUIDv4 rule is applied to the path value directly.
                try:
                    parsed = uuid.UUID(path_key)
                except ValueError as exc:
                    raise AppError(
                        ErrorCode.INVALID_ARGUMENT,
                        internal_detail="path key must be a canonical UUIDv4",
                    ) from exc
                if (
                    parsed.version != 4
                    or parsed.variant != uuid.RFC_4122
                    or str(parsed) != path_key
                ):
                    raise AppError(
                        ErrorCode.INVALID_ARGUMENT,
                        internal_detail="path key must be a canonical UUIDv4",
                    )
                operation = (
                    session.query(Operation)
                    .filter(Operation.idempotency_key == path_key)
                    .filter(Operation.api_request.has(device_id=auth.device_id))
                    .one_or_none()
                )
                if operation is None:
                    raise AppError(
                        ErrorCode.OPERATION_NOT_ANCHORED,
                        internal_detail=(
                            f"no operation anchored for key {path_key} on this device"
                        ),
                    )
                anchored_operation_id = operation.operation_id
                return _operation_response(
                    deps.keyring,
                    operation,
                    client_wire_version=auth.client_wire_version,
                )

            response = _commit(session, work)
        if anchored_operation_id is not None:
            return _record_operation_http_response(
                deps,
                anchored_operation_id,
                _authenticated_device(authenticated_device_id),
                response,
                delivery="operation_poll_by_key",
            )
        return response

    @app.delete("/v1/operations/{operation_id}")
    async def cancel_operation(operation_id: str, request: Request):
        authenticated_device_id: str | None = None
        with deps.session_factory() as session:
            def work():
                nonlocal authenticated_device_id
                auth = authenticate(request, session)
                authenticated_device_id = auth.device_id
                _owned_operation(session, operation_id, device_id=auth.device_id)
                request_cancel(session, operation_id=operation_id, now=deps.now())
                from personal_agent.runtime.run_repository import RunRepository
                RunRepository(deps.session_factory,deps.keyring).cancel_pre_submit(session,operation_id,now_ms=round(deps.now().timestamp()*1000))
                operation = get_operation(session, operation_id)
                return _operation_response(
                    deps.keyring,
                    operation,
                    client_wire_version=auth.client_wire_version,
                )

            response = _commit(session, work)
        return _record_operation_http_response(
            deps,
            operation_id,
            _authenticated_device(authenticated_device_id),
            response,
            delivery="operation_cancel",
        )

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
                auth=authenticate(request, session)
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
                if auth.client_wire_version<4 and any(isinstance(entry.content,dict) and entry.content.get('result_envelope',{}).get('version')==2 for entry in page.entries):
                    from personal_agent.api.runtime_v2 import unavailable
                    raise unavailable('client_upgrade_required')
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
                    "chat_runtime_v2": auth.client_wire_version >= 4 and auth.device_id in deps.v2_device_ids and deps.v2_execution_enabled,
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
                # `#13`. §8's "客户端 capability 和服务端入口同源计算": this is
                # the same callable the entry guard and the anchor read, so a
                # client cannot be told one thing while the server does another.
                # Only the verdict travels -- the closed terms name internal
                # controls, and a client needs to know whether it may send an
                # image, not which of the deployment's approvals is missing.
                capability = deps.image_capability()
                # The verdict remains the only approval information a device
                # receives.  These bounds are not approval terms: they are the
                # public limits a client needs to resize and encode before it
                # makes an upload request.  Omitting them would force iOS to
                # invent local ceilings, which is exactly the configuration
                # drift §4.3 prohibits.
                images: dict[str, Any] = {"enabled": capability.enabled}
                if deps.media_limits is not None:
                    images.update(
                        {
                            "max_content_bytes": deps.media_limits.max_content_bytes,
                            "max_dimension": deps.media_limits.max_dimension,
                            "allowed_mimes": sorted(deps.media_limits.allowed_mimes),
                        }
                    )
                body["images"] = images
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

    @app.post("/v1/expense-records/{record_id}/category")
    async def post_category_correction(record_id: str, request: Request):
        """Correct one already-recorded expense's 分类 from the receipt card.

        Keyed by `record_id` rather than by operation: Henson's 2026-08-15
        decision is that any expense row is correctable, including one found by
        scrolling back through history, so the address is the ledger row and not
        the conversation turn that happened to create it.

        This route is deliberately **off the model channel**. The picker's tap is
        the decision; there is nothing to interpret and nothing to infer, so the
        operation is created with its intent already resolved and never reaches
        a model. `finance.update_expense_category` is `model_callable=False` in
        the IR for the same reason, which means this is not merely the path the
        model does not take -- it is the only path there is.

        It still goes through a full `Operation`: device identity, the client's
        idempotency key, policy, the audit envelope, an external receipt and a
        Timeline event. An edit to a committed ledger row is a governed write,
        and the fact that a person pressed a button does not make it less so.
        """
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        key = idempotency_key(request)
        category = _required(body, "category")
        # Absent and null are different: null states "I believe this row has no
        # category", which is a real state for a refund. `_required` would
        # collapse them, so this one is read directly.
        expected = body.get("expected_current_category")
        if expected is not None and not isinstance(expected, str):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="expected_current_category must be a string or null",
            )
        if "expected_current_category" not in body:
            # The compare-and-swap is the whole safety story of this route. A
            # caller that omits it is not requesting a blind overwrite -- it is
            # a caller that has not been updated, and treating the omission as
            # "expect nothing" would silently turn every stale card into a
            # successful overwrite of someone else's edit.
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="expected_current_category is required",
            )
        return await asyncio.to_thread(
            _process_category_correction,
            deps,
            auth,
            record_id,
            category,
            expected,
            key,
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

    @app.post("/v1/device-actions/{action_id}/result")
    async def post_device_action_result(action_id: str, request: Request):
        """Settle one device-executed write with the phone's own report.

        The iPhone is the executor of `calendar.create_event` and the fact
        source for what it did, so its report is the evidence the operation
        settles on -- not a claim to be re-verified against anything. The
        report vocabulary is closed:

        - `created` / `duplicate` -- the event exists on the phone. The
          EventKit identifier is the receipt's record id (`safe_result`), and
          both are success: the device's local dedup finding the event already
          there is a created calendar from the user's point of view.
        - `denied` / `failed` -- EventKit refused the save. The device refused
          before any write could exist, which is the strongest zero-write
          evidence this domain can hold, so the operation settles `failed_safe`
          -- the one place a device report may claim it.

        A report naming a success without an `event_id` proves nothing and is
        refused before it can settle anything. The operation is located by its
        own idempotency key (which *is* the action id) under the same ownership
        rule every operation endpoint applies, so a valid token for another
        device cannot settle this one's action. The CAS transition makes the
        settlement exactly-once: a replay answers the settled projection and
        never re-migrates.
        """
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        return await asyncio.to_thread(
            _process_device_action_result, deps, auth, action_id, body
        )

    @app.post("/v1/device-actions/{action_id}/override")
    async def post_device_action_override(action_id: str, request: Request):
        """Write the calendar event again, because the user said 「仍要创建」.

        The device reported `duplicate`, so the phone's own lookup found an
        event it will not write over. The user answered the card and asked for
        it anyway; that answer is what this endpoint carries, and nothing else.

        There is no decision to submit, which is why the body is empty and
        closed: the meaning of the call is entirely in *which* action it names
        and in whether that action is allowed to be overridden (a device-
        executed calendar create that settled `succeeded` on a `duplicate`
        report). An override is a write, so it is issued afresh through the
        normal dispatch path -- policy, scopes and kill switch are all
        re-evaluated rather than inherited from the operation being overridden.

        It is idempotent by derivation, not by a key the caller sends: the one
        derived operation for an action is fixed by the action's own id, so a
        double tap, a retry whose response was lost, and two concurrent taps
        all reach the same operation and the same projection (design 3.3).
        """
        # Authentication precedes any body check, as it does on every other
        # device endpoint: an unauthenticated caller learns nothing about this
        # action, not even whether its own envelope would have been accepted.
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        body = await _json_body(request)
        if body:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="an override carries no body",
            )
        return await asyncio.to_thread(
            _process_device_action_override, deps, auth, action_id
        )

    @app.post("/v1/calendar/sync")
    async def post_calendar_sync(request: Request):
        """Merge one device calendar snapshot into the server's mirror.

        The mirror is what `calendar.query_events` reads, so a device that
        could write it through any other path could fabricate the calendar the
        model would then be asked to summarise. The upload is therefore
        governed like any tool call: device auth, the `calendar.event.read`
        scope (an upload exists to be read back), and the real bridge inside
        `deps.sync_ingest`, which signs a Host Context naming the calling
        device -- the payload never carries an identity the server trusts.

        One malformed batch refuses whole (§5.1: no silent triage), and the
        bridge call runs without an API-side transaction under it (§5.2).
        """
        if deps.sync_ingest is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="calendar sync is not composed",
            )
        auth = await asyncio.to_thread(_authenticate_once, request, deps, authenticate)
        if SCOPE_CALENDAR_READ not in auth.scopes:
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail="calendar sync requires calendar.event.read",
            )
        body = await _json_body(request, max_bytes=_MAX_SYNC_BODY_BYTES)
        _validate_sync_body(body)
        result = await asyncio.to_thread(deps.sync_ingest, auth, body)
        return JSONResponse(result)

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


def _join_boundary_workers(workers: tuple[threading.Thread, ...]) -> None:
    """Join the bounded background classifier workers during orderly shutdown."""
    for worker in tuple(workers):
        worker.join(timeout=30.0)


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


# --- media (`#18`) ---------------------------------------------------------
#
# §5.2's five endpoints. Every one of them is device-authenticated (`GET`
# included -- an image is personal data and §6's read protection is what
# replaces the long-lived URL the design refuses), and every one of them is
# thin: the state machine, the locks and the store live in
# `personal_agent.media`, and this layer only turns one wire body into a
# declaration and one domain refusal into a status.


def _media_ready(deps: AgentApiDeps) -> tuple[MediaStore, MediaLimits]:
    """The composed media surface, or a refusal saying it is not composed.

    `INTERNAL_ERROR` rather than `UNSUPPORTED_OPERATION`: an uncomposed store
    is a deployment that forgot to wire one, not a feature this phase declines
    to offer, and the two must not look alike to whoever reads the log.
    """
    if deps.media_store is None or deps.media_limits is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="media is not composed: no store or no limits wired",
        )
    return deps.media_store, deps.media_limits


def _media_refusal(error: MediaError) -> AppError:
    """Map one media-domain refusal onto its outward code, **by type**.

    Never by message. A mapper that switched on text would keep passing after a
    message was reworded and would quietly start answering `INVALID_ARGUMENT`
    for everything -- and these codes exist precisely because the client's next
    action differs between them: wait (`MEDIA_NOT_READY`), re-send the same
    bytes (`MEDIA_BUSY`), or stop (`MEDIA_GONE`).
    """
    if isinstance(error, MediaBusyError):
        return AppError(ErrorCode.MEDIA_BUSY, internal_detail=str(error))
    if isinstance(error, MediaNotFoundError):
        return AppError(ErrorCode.MEDIA_NOT_FOUND, internal_detail=str(error))
    if isinstance(error, MediaGoneError):
        return AppError(ErrorCode.MEDIA_GONE, internal_detail=str(error))
    if isinstance(error, (MediaNotReadyError, MediaIncompleteUploadError)):
        return AppError(ErrorCode.MEDIA_NOT_READY, internal_detail=str(error))
    # `MediaRejectedError` and every other lifecycle refusal: the request and
    # the object's state disagree, and repeating the request changes nothing.
    # `INVALID_ARGUMENT` is the honest answer -- the client's request was
    # wrong, not early. A rejected object is a tombstone (`reject_upload`
    # writes no manifest entry, because nothing was ever published), so a
    # client that ignores this gets `MEDIA_GONE` on its next read, which is the
    # truthful follow-up rather than a contradiction.
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=str(error))


def _upload_declaration(body: dict[str, Any]) -> UploadDeclaration:
    """Read §5.2's create row -- purpose/MIME/size/hash -- off the wire.

    Every field is required and none is defaulted. A defaulted digest would
    make the server compare its measurement against a value nobody sent, and
    §5.4 keeps the declared hash for exactly one purpose: deciding that what
    arrived is not what was announced.

    `purpose` is checked here rather than in the domain because it is a wire
    field with no counterpart in `UploadDeclaration` -- the state machine has a
    single purpose, and `create_upload` would refuse a second one by raising.
    Checking it at the door turns that into a 400 that names the field.
    """
    purpose = _required(body, "purpose")
    if purpose != CHAT_IMAGE_PURPOSE:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"unsupported media purpose {purpose!r}",
        )
    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="size must be a positive integer",
        )
    width = _optional_dimension(body, "width")
    height = _optional_dimension(body, "height")
    if (width is None) != (height is None):
        # §5.2 keeps both as the client's declared values. One without the
        # other describes no rectangle, and a later reuse (aspect-ratio
        # planning, §6) would have to guess the missing half.
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="width and height must be declared together",
        )
    return UploadDeclaration(
        mime=_required(body, "mime"),
        size=size,
        sha256=_required(body, "sha256"),
        width=width,
        height=height,
    )


def _optional_dimension(body: dict[str, Any], field: str) -> int | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} must be a positive integer when present",
        )
    return value


async def _binary_body(request: Request, *, ceiling: int) -> bytes:
    """Read one bounded binary body for `PUT /v1/media/content/{id}`.

    §5.2: "仅二进制 content 前缀使用独立 body 上限/限速；JSON 的两条 64 KiB 不
    变". The ceiling is §4.3's object ceiling rather than the JSON one, because
    the two bodies are different kinds of thing: the JSON body is parsed and
    copied several times over, while this one is copied once into the staging
    writer. A 30 MiB image is an ordinary request here and a fatal one there.

    The ceiling is enforced *while* reading, not after: a client that streams
    past it is cut off at the ceiling instead of being buffered whole and then
    refused, which is the difference between a bounded service and a
    memory-exhaustion one. The `Content-Length` pre-check is only an early
    exit -- a lying header still meets the read loop.

    `Content-Type` is deliberately unchecked. The materiality test for these
    bytes is the magic probe against the sealed declaration (§5.4's
    "验证不能仅信文件扩展名"), and a header the server is forbidden to trust
    cannot be the gate that decides.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="Content-Length must be an integer",
            ) from exc
        if declared_length < 0 or declared_length > ceiling:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"content body exceeds {ceiling} bytes",
            )

    buffered = bytearray()
    async for chunk in request.stream():
        if len(buffered) + len(chunk) > ceiling:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"content body exceeds {ceiling} bytes",
            )
        buffered.extend(chunk)
    return bytes(buffered)


def _start_media_upload(
    deps: AgentApiDeps, auth: AuthContext, key: str, body: dict[str, Any]
) -> JSONResponse:
    """`POST /v1/media/uploads`: create a target, or return the one it made.

    The idempotency key is required here even though `start_upload` accepts
    `None`. A request must not be able to create an object no key names: the
    response carries the media id, so a client that lost it has nothing to
    retry with, and the alternative -- a second `pending` object -- is a row
    the user is never told about.
    """
    _, limits = _media_ready(deps)
    declaration = _upload_declaration(body)
    with deps.session_factory() as session:
        def work():
            try:
                created = start_upload(
                    session,
                    keyring=deps.keyring,
                    device_id=auth.device_id,
                    client_request_id=key,
                    declaration=declaration,
                    limits=limits,
                    now=deps.now(),
                )
            except MediaError as error:
                raise _media_refusal(error) from error
            return JSONResponse(
                {
                    "media_id": created.media_id,
                    "state": created.state,
                    "expires_at": to_rfc3339(created.expires_at),
                    "replayed": created.replayed,
                },
                # A replay is the same object, so it is not a creation. `201`
                # for both would claim the client now has two of something.
                status_code=200 if created.replayed else 201,
            )

        return _commit(session, work)


def _receive_media_content(
    deps: AgentApiDeps, auth: AuthContext, media_id: str, body: bytes
) -> JSONResponse:
    """`PUT /v1/media/content/{id}`: consume the target and seal the bytes."""
    store, limits = _media_ready(deps)
    with deps.session_factory() as session:
        def work():
            try:
                receipt = receive_upload(
                    session,
                    store=store,
                    keyring=deps.keyring,
                    media_id=media_id,
                    device_id=auth.device_id,
                    body=body,
                    limits=limits,
                    now=deps.now(),
                )
            except MediaError as error:
                raise _media_refusal(error) from error
            return JSONResponse(_receipt_json(receipt))

        return _commit(session, work)


def _receipt_json(receipt: UploadReceipt) -> dict[str, Any]:
    """The measured half of one upload, as §5.1 requires it to be labelled.

    `size` here is the server's count of what arrived, not the declaration --
    the field is only named `size` because this object's response has no
    declared value to be confused with. `complete` returns both and names them
    apart.
    """
    return {
        "media_id": receipt.media_id,
        "state": receipt.state,
        "mime": receipt.mime,
        "size": receipt.size,
    }


def _complete_media_upload(
    deps: AgentApiDeps, auth: AuthContext, media_id: str
) -> JSONResponse:
    """`POST /v1/media/uploads/{id}/complete`: verify the seal, then publish.

    Reached `uploaded`, this publishes. Reached while an attempt is still
    running it answers `202` with `outcome: in_progress` and the claim's
    deadline, because §5.2 makes the poll the documented recovery for a lost
    `PUT` response -- "不把尚未完成当坏图，处理中返回可轮询状态与期限".

    `202` rather than a `409` carrying the same body: every other refusal on
    this route is an `{"error": {...}}` envelope, and one status code with two
    body shapes is how a client ends up parsing an error as a status or the
    reverse. Here the status is never in doubt -- the request was accepted and
    the answer is a state -- so the body shape is the same on `200` and `202`,
    and only the status says which it is.

    A tombstone is a `200` with the outcome named, not a `410`: re-asking after
    a deletion is a legitimate way to find out what happened, and turning it
    into a refusal would make the client guess which of the two it got.
    """
    store, limits = _media_ready(deps)
    with deps.session_factory() as session:
        def work():
            try:
                completed = complete_upload(
                    session,
                    store=store,
                    keyring=deps.keyring,
                    media_id=media_id,
                    device_id=auth.device_id,
                    limits=limits,
                    now=deps.now(),
                )
            except MediaError as error:
                raise _media_refusal(error) from error
            body = _completed_json(completed)
            if completed.outcome is CompleteOutcome.IN_PROGRESS:
                return JSONResponse(body, status_code=202)
            return JSONResponse(body)

        return _commit(session, work)


def _completed_json(completed: CompletedUpload) -> dict[str, Any]:
    return {
        "media_id": completed.media_id,
        "state": completed.state,
        "outcome": completed.outcome.value,
        "retry_at": (
            None if completed.retry_at is None else to_rfc3339(completed.retry_at)
        ),
        # The server's measurement (§5.1). Null on a tombstone or while an
        # attempt is still in flight -- the object has no published bytes yet,
        # and reporting the declaration as if it were one is what §5.1 forbids.
        "content_sha256": completed.content_sha256,
        "mime": completed.mime,
        "size": completed.size,
        "declared_width": completed.declared_width,
        "declared_height": completed.declared_height,
    }


def _fetch_media(deps: AgentApiDeps, auth: AuthContext, media_id: str) -> Response:
    """`GET /v1/media/{id}`: the persisted image under §6's read protection."""
    store, _ = _media_ready(deps)
    with deps.session_factory() as session:
        def work():
            try:
                fetched = read_media(
                    session,
                    store=store,
                    keyring=deps.keyring,
                    media_id=media_id,
                    device_id=auth.device_id,
                )
            except MediaError as error:
                raise _media_refusal(error) from error
            return Response(
                content=fetched.body,
                media_type=fetched.mime,
                headers={
                    # §5.2: "不发长期 URL，不写公共缓存". `no-store` is the
                    # stronger half of that pair -- the bytes never reach a
                    # shared cache at all, which a bearer-token URL's `private`
                    # would still permit on the device.
                    "Cache-Control": "no-store",
                    # The digest of exactly these bytes, so the client's
                    # round-trip self-check (§FR-PHOTO) compares against a
                    # server measurement and not against its own echo.
                    "ETag": f'"{hashlib.sha256(fetched.body).hexdigest()}"',
                },
            )

        return _commit(session, work, retry=False)


def _delete_media(deps: AgentApiDeps, auth: AuthContext, media_id: str) -> JSONResponse:
    """`DELETE /v1/media/{id}`: mark the deletion and answer for the decision.

    The response deliberately separates "已受理" from "物理完成" (§5.2): the
    mark and its manifest entry commit together, and the reaper removes the
    bytes later. A second delete is a 200 with `already_decided`, never a
    conflict -- the state the caller asked for already holds.
    """
    with deps.session_factory() as session:
        def work():
            try:
                decided = decide_media_deletion(
                    session,
                    keyring=deps.keyring,
                    media_id=media_id,
                    device_id=auth.device_id,
                    now=deps.now(),
                )
            except MediaError as error:
                raise _media_refusal(error) from error
            return JSONResponse(
                {
                    "media_id": media_id,
                    "state": "deleting",
                    "decided": decided,
                }
            )

        return _commit(session, work, retry=False)


def _preflight_chat_replay(
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    parts: Parts,
    clarification_of: str | None,
    start_new_session: bool,
) -> _AnchoredChat | None:
    """Return an existing idempotent chat before spending a classifier call.

    Two ways to recognise the request, because a parts request cannot be
    fingerprinted here: §3.2's fingerprint input carries the measured digest of
    every image, and reading one back would be a read of live media -- which the
    same sentence forbids on the replay path ("不访问活媒体"). So a parts request is
    compared against the sealed structure instead, which is the same comparison
    with an immutable value standing in for a re-readable one.
    """

    with deps.session_factory() as session:
        try:
            timeline_id = events.resolve_timeline(
                session,
                deps.identifier_key,
                client_conversation_id=conversation_id,
                now=deps.now(),
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
            if parts:
                sealed = open_chat_request(
                    deps.keyring,
                    request_id=request_row.request_id,
                    envelope=request_row.encrypted_request_payload,
                )
                matches = describes_request(
                    sealed,
                    conversation_id=timeline_id,
                    text=text,
                    parts=parts,
                    clarification_of=clarification_of,
                    start_new_session=start_new_session,
                )
            else:
                fingerprint = chat_request_fingerprint(
                    conversation_id=timeline_id,
                    text=text,
                    clarification_of=clarification_of,
                    start_new_session=start_new_session,
                )
                matches = request_row.request_fingerprint == fingerprint
            if not matches:
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


def _prepare_chat_boundary(
    deps: AgentApiDeps,
    conversation_id: str,
    text: str,
    clarification_of: str | None,
) -> PreparedClassification:
    """Prepare a possible asynchronous boundary judgement before anchoring.

    This is only a bounded local read.  The potentially slow model call is made
    after the turn has been accepted, so an unavailable classifier cannot turn
    into user-visible chat latency.
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
    return prepared


def _resolve_chat_boundary(
    deps: AgentApiDeps,
    prepared: PreparedClassification,
    operation_id: str,
) -> ResolvedClassification:
    """Call the classifier after anchoring, with the durable turn identity."""
    with deps.session_factory() as session:
        try:
            operation = get_operation(session, operation_id)
            anchor = _anchor_event(session, operation_id)
            identity = _turn_identity(operation, anchor)
            session.commit()
        except Exception:
            session.rollback()
            raise
    with deps.recorder.turn(identity):
        return deps.session_manager.resolve_classification(prepared)


def _apply_chat_boundary(
    deps: AgentApiDeps,
    operation_id: str,
    resolved: ResolvedClassification,
) -> str | None:
    """Apply a ready retrospective boundary in a fresh retryable transaction."""
    with deps.session_factory() as session:
        try:
            def work() -> str | None:
                anchor = _anchor_event(session, operation_id)
                decision = deps.session_manager.apply_retroactive_boundary(
                    session,
                    conversation_id=anchor.conversation_id,
                    operation_id=operation_id,
                    resolved=resolved,
                    now=deps.now(),
                )
                return decision.session_id if decision is not None else None

            applied = run_write_transaction(session, work)
            session.commit()
            return applied
        except Exception:
            session.rollback()
            raise


def _anchor_chat(
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    parts: Parts,
    clarification_of: str | None,
    start_new_session: bool,
    resolved_classification: ResolvedClassification,
) -> _AnchoredChat:
    """Persist request, encrypted payload and user event before model work."""

    with deps.session_factory() as session:
        try:
            with _chat_media_locks(deps, parts):
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
                                parts,
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


@contextmanager
def _chat_media_locks(deps: AgentApiDeps, parts: Parts) -> Iterator[None]:
    """§4.1's lock set for the images this message names, or nothing at all.

    Entered **outside** the write transaction and released after it, because
    that is the order §4.1 gives: the lock is what makes the transaction's
    read-and-compare-and-swap atomic against a concurrent deleter, and CLAUDE.md
    §5.2 forbids holding a database transaction while waiting on a file lock.
    Holding it across the retry loop is deliberate -- the retries re-read state
    under the same protection.

    Non-blocking, like the upload path's use of the same lock: a message that
    would have to queue behind a slow reader is refused and retried by the
    client, and §6 gives the reader that holds the stripe a bounded grace rather
    than a queue.
    """
    media_ids = _chat_image_ids(parts)
    if not media_ids:
        yield
        return
    store, _ = _media_ready(deps)
    try:
        with media_locks(store.roots.root, media_ids, blocking=False):
            yield
    except MediaLockError as exc:
        raise MediaBusyError("the media store is busy; retry shortly") from exc


def _anchor_chat_in_transaction(
    session,
    deps: AgentApiDeps,
    auth: AuthContext,
    key: str,
    conversation_id: str,
    text: str,
    parts: Parts,
    clarification_of: str | None,
    start_new_session: bool,
    resolved_classification: ResolvedClassification,
) -> _AnchoredChat:
    """Anchor one chat against the transaction's current database snapshot.

    §3.2's first anchoring runs here, in the order the design gives: resolve the
    request's images, fingerprint them with the measured digests, create the
    request/operation under one key, and only then -- once the user event exists
    to hang them on -- record the bindings and move each object to `bound`.
    """
    timeline_id = events.resolve_timeline(
        session,
        deps.identifier_key,
        client_conversation_id=conversation_id,
        now=deps.now(),
    )
    # Resolution precedes the fingerprint because the fingerprint's input *is*
    # the resolved image (§3.2: ordered (media_id, content_sha256), digest
    # server-measured). It also precedes `open_operation`, which costs a reader
    # one extra live read in the rare race where this request turns out to be a
    # duplicate -- the loser of that race returns the winner's operation without
    # ever binding anything, and a client whose retry lands in that window is
    # answered by the preflight above, which never reads live media.
    images = _resolve_chat_images(session, deps, auth, parts)
    fingerprint = chat_request_fingerprint(
        conversation_id=timeline_id,
        text=text,
        clarification_of=clarification_of,
        start_new_session=start_new_session,
        parts=resolved_parts(parts, images),
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

    from personal_agent.api import runtime_v2
    runtime_version, source_version = runtime_v2.choose(session, deps, auth, clarification_of, timeline_id)

    if start_new_session and runtime_version == 1:
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
        if runtime_version == 1:
            transition_operation(
                session,
                operation_id=source.operation_id,
            current_state=source.state,
            current_version=source.state_version,
            target_state="cancelled_pre_submit",
            now=deps.now(),
        )
    elif runtime_version == 1 and is_finance_retry_request(text):
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
        # The *unresolved* parts: §3.2 seals the original structure, and the
        # measured digest belongs to the fingerprint and the media table, not to
        # the sealed copy of what the client sent.
        parts=parts,
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
    event_id = events.append_event(
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
    if images:
        bind_chat_images(
            session,
            images=images,
            event_id=event_id,
            operation_id=operation.operation_id,
            reuse_lineage=_reuse_lineage(clarification_of, retry_source),
            now=deps.now(),
        )
    if runtime_version == 2:
        runtime_v2.anchor(session, deps, operation, payload, source_version)
    logger.info(
        "session boundary %s",
        json.dumps(decision.audit_record(), sort_keys=True),
    )
    return _AnchoredChat(operation.operation_id, operation.state)


def _resolve_chat_images(
    session,
    deps: AgentApiDeps,
    auth: AuthContext,
    parts: Parts,
) -> tuple:
    """Resolve every image this message names, or refuse the request.

    A text-only request resolves nothing and does not need a media store at all,
    which matters: §5.4's "缺配置不启用图片" means a deployment with images off is
    still a working deployment, and only the requests that actually name an
    image may require the media configuration to exist.
    """
    if not _chat_image_ids(parts):
        return ()
    # §3.2 checks "扫描/豁免状态 ... 与能力" inside the anchoring transaction,
    # which is here, and §8's reason is worth restating: a client that uploaded
    # while the switch was open, and then re-sends after it closed, must not be
    # let through because its own cached capability still says yes. The entry
    # guard has already asked the same question; this asks it again at the point
    # where an object would acquire a use, because that is the point a refusal
    # still costs nothing.
    capability = deps.image_capability()
    if not capability.enabled:
        raise AppError(
            ErrorCode.UNSUPPORTED_OPERATION,
            internal_detail=capability.refusal(),
        )
    _, limits = _media_ready(deps)
    return resolve_chat_images(
        session,
        keyring=deps.keyring,
        device_id=auth.device_id,
        parts=parts,
        limits=limits,
    )


def _reuse_lineage(
    clarification_of: str | None, retry_source: Operation | None
) -> tuple[str, ...]:
    """The operations whose recorded use of an image this message may re-establish.

    §5.1 allows exactly two, and both are server-side facts by the time this
    runs: the clarification source has been checked to be waiting for
    clarification in this conversation, and the retry source has been checked to
    be the newest unconsumed failure that provably made no call. Neither is
    taken from the request's own shape, which is what §3.2's "服务端验证 source
    lineage" rules out.
    """
    if clarification_of is not None:
        return (clarification_of,)
    if retry_source is not None:
        return (retry_source.operation_id,)
    return ()


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

    from personal_agent.api import runtime_v2
    with deps.session_factory() as check_session:
        is_v2 = runtime_v2.row(check_session, operation_id) is not None
    if is_v2:
        return runtime_v2.process(deps, auth, operation_id)

    with deps.session_factory() as session:
        try:
            operation = _owned_operation(
                session, operation_id, device_id=auth.device_id
            )
            if operation.state != "accepted":
                # A replay of a frozen action plan finishes issuing it (design
                # 4.1). This is the one non-`accepted` state that still has work
                # to do: the freeze wrote the list down, and a crash between two
                # items left the rest waiting for an action that only this
                # re-entry can give them. Every other shape is answered by its
                # projection, exactly as before -- `resume_action_plan` returns
                # `None` unless there is something to finish.
                resumed = _resume_action_plan(deps, auth, session, operation)
                if resumed is not None:
                    return resumed
                return _ProcessedChat(
                    _operation_response(
                        deps.keyring,
                        operation,
                        client_wire_version=auth.client_wire_version,
                    )
                )
            payload = open_chat_request(
                deps.keyring,
                request_id=operation.request_id,
                envelope=operation.api_request.encrypted_request_payload,
            )
            anchor = _anchor_event(session, operation.operation_id)
            # Every record this worker writes from here on belongs to this one
            # message. The scope is opened after the anchor because the Session
            # and turn ids are part of the correlation keys.
            with deps.recorder.turn(_turn_identity(operation, anchor)):
                return _run_chat_turn(
                    deps, auth, session, operation, payload=payload, anchor=anchor
                )
        except StaleOperationVersionError:
            session.rollback()
            operation = _owned_operation(
                session, operation_id, device_id=auth.device_id
            )
            return _ProcessedChat(
                _operation_response(
                    deps.keyring,
                    operation,
                    client_wire_version=auth.client_wire_version,
                )
            )
        except Exception:
            session.rollback()
            raise


def _resume_action_plan(
    deps: AgentApiDeps,
    auth: AuthContext,
    session,
    operation: Operation,
) -> _ProcessedChat | None:
    """Finish a frozen plan this session's replay found unfinished.

    The turn is recorded under the same correlation identity as the message that
    created the plan -- the anchor already exists, because the plan's first item
    is that very operation -- so the resumed issuing reads as the same
    conversation turn rather than as a second one.
    """
    if operation.plan_key is None:
        # Not a member of any frozen list, so there is nothing to finish and no
        # reason to build a dispatcher for it.
        return None
    identity = _turn_identity(
        operation, _anchor_event_or_none(session, operation.operation_id)
    )
    with deps.recorder.turn(identity):
        result = resume_action_plan(
            session,
            operation,
            dispatcher=deps.build_dispatcher(auth, operation.trace_id),
            authorize=deps.build_authorizer(auth),
            keyring=deps.keyring,
            action_keyring=deps.action_keyring,
            now=deps.now,
        )
        if result is None:
            return None
        deps.recorder.record(
            transcript.TURN_RESULT, {"state": result.state, "result": result}
        )
    session.commit()
    session.refresh(operation)
    return _ProcessedChat(
        _operation_response(
            deps.keyring,
            operation,
            client_wire_version=auth.client_wire_version,
            extra=_transient(result),
        )
    )


def _run_chat_turn(
    deps: AgentApiDeps,
    auth: AuthContext,
    session,
    operation: Operation,
    *,
    payload: ChatRequestPayload,
    anchor: _Anchor,
) -> _ProcessedChat:
    """One accepted operation, inside its transcript scope."""

    deps.recorder.record(
        transcript.USER_MESSAGE,
        {
            "text": payload.text,
            "clarification_of": payload.clarification_of,
            "clarification_question": payload.clarification_question,
            "clarification_context": payload.clarification_context,
            "finance_retry_context": payload.finance_retry_context,
            "start_new_session": payload.start_new_session,
        },
    )
    turn_context = _context_factory(
        deps,
        auth,
        session,
        payload=payload,
        anchor=anchor,
        operation_id=operation.operation_id,
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
        prior_clarification_question=(
            payload.clarification_context.question
            if payload.clarification_context is not None
            else None
        ),
        recorder=deps.recorder,
        action_keyring=deps.action_keyring,
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
        content=_operation_event_content(deps.keyring, operation),
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
    processed = _ProcessedChat(
        _operation_response(
            deps.keyring,
            operation,
            client_wire_version=auth.client_wire_version,
            extra=_transient(result),
        ),
        compact_session_id=compact_session_id,
    )
    return processed


def _authenticated_device(device_id: str | None) -> str:
    """The device the handler authenticated, as a proven non-`None` value.

    `_commit` raises before returning when authentication fails, so this cannot
    trigger -- but an `assert` would vanish under `python -O` and leave a `None`
    travelling into an ownership check as a device id.
    """
    if device_id is None:  # pragma: no cover - _commit raises first
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="a committed handler left no authenticated device",
        )
    return device_id


def _record_operation_http_response(
    deps: AgentApiDeps,
    operation_id: str,
    device_id: str,
    response: JSONResponse,
    *,
    delivery: str,
) -> JSONResponse:
    """Record the body chosen by the route, without changing that response."""
    if not deps.recorder.enabled:
        # A disabled transcript costs nothing. Assembling the identity means a
        # second database session and two reads per polled response, and every
        # default deployment would pay them to feed a sink that discards them.
        return response
    try:
        with deps.session_factory() as session:
            operation = _owned_operation(session, operation_id, device_id=device_id)
            _record_http_response(
                deps, session, operation, response, delivery=delivery
            )
    except Exception:  # noqa: BLE001 - diagnostics never change API behaviour
        logger.warning(
            "operation HTTP response transcript dropped operation_id=%s",
            operation_id,
            exc_info=True,
        )
    return response


def _record_http_response(
    deps: AgentApiDeps,
    session,
    operation: Operation,
    response: JSONResponse,
    *,
    delivery: str,
) -> None:
    """Record one actual HTTP response under its durable turn identity.

    The anchor is optional here. Polling a category correction hit
    `_anchor_event`'s hard failure on every request during the 2026-08-16
    acceptance run: that operation has no user message, so it has no anchoring
    event, and the recorder was treating a legitimate shape as a wiring error.
    Nothing broke — the `except` below keeps diagnostics from changing API
    behaviour — but every poll logged a traceback and dropped its transcript,
    which is the opposite of what a transcript is for. `_turn_identity` already
    accepts `None` and records the operation, trace and device ids, which
    identify the record completely.
    """
    try:
        identity = _turn_identity(
            operation, _anchor_event_or_none(session, operation.operation_id)
        )
        with deps.recorder.turn(identity):
            deps.recorder.record(
                transcript.API_RESPONSE,
                {
                    "delivery": delivery,
                    "status_code": response.status_code,
                    "body": _decoded_body(response),
                },
            )
    except Exception:  # noqa: BLE001 - diagnostics never change API behaviour
        logger.warning(
            "operation HTTP response transcript dropped operation_id=%s",
            operation.operation_id,
            exc_info=True,
        )


def _decoded_body(response: JSONResponse) -> Any:
    """The rendered response body as data, for the transcript only.

    Decoding what was actually serialised -- rather than re-projecting the
    operation -- is the point: a field lost during rendering is invisible to any
    record built from the inputs.
    """
    try:
        return json.loads(response.body)
    except (TypeError, ValueError):
        return {"__undecodable_body__": len(response.body)}


@dataclass(frozen=True)
class _Anchor:
    """The persisted user message this operation belongs to."""

    conversation_id: str
    session_id: str
    turn_id: str
    event_id: str


def _turn_identity(operation: Operation, anchor: _Anchor | None) -> TurnIdentity:
    """Correlation identity for the transcript.

    `anchor` is `None` only when the caller genuinely has no Timeline owner. The
    operation, trace and device ids still identify that record completely;
    inventing a Session or turn id to fill the shape would put a fabricated
    correlation key into the transcript, which is worse than an absent one.
    """
    return TurnIdentity(
        operation_id=operation.operation_id,
        client_request_id=operation.api_request.client_request_id,
        trace_id=operation.trace_id,
        turn_id=anchor.turn_id if anchor is not None else None,
        session_id=anchor.session_id if anchor is not None else None,
        conversation_id=anchor.conversation_id if anchor is not None else None,
        device_id=operation.api_request.device_id,
    )


def _anchor_event_or_none(session, operation_id: str) -> _Anchor | None:
    """The operation's own Timeline anchor, or `None` if it has none.

    Not every operation is a conversation turn. A receipt-card category
    correction is a direct action on a ledger row: it has no user message, so
    it has no anchoring event, and that is a legitimate shape rather than a
    defect. Callers that merely want to *label* a record use this; callers for
    which a missing anchor really is a wiring error use `_anchor_event`.
    """
    row = session.execute(
        text_clause(
            "SELECT conversation_id, session_id, turn_id, event_id "
            "FROM conversation_events "
            "WHERE operation_id = :oid ORDER BY timeline_sequence LIMIT 1"
        ),
        {"oid": operation_id},
    ).one_or_none()
    if row is None:
        return None
    return _Anchor(
        conversation_id=row[0],
        session_id=row[1],
        turn_id=row[2],
        event_id=row[3],
    )


def _message_operation(session, operation: Operation) -> Operation:
    """The operation the user's message became, for any operation downstream of it.

    `_plan_rows` answers the *delivery* question -- which actions may this reply
    hand over -- and so returns the whole plan for item 0 and the row alone for
    a sibling. This answers the *authorship* question, and it has one answer for
    every row the same sentence produced:

    - an item of a frozen plan belongs to item 0, the message itself;
    - a row an override derived belongs to the operation it names as its parent
      (`_process_device_action_override` states exactly that when it labels the
      derived run with the source's turn), so a chain of deliberate re-issues
      still points at the one message that asked for the event;
    - anything else is its own message.

    The walk is bounded rather than trusting the lineage to be acyclic: a cycle
    would otherwise be an unbounded loop inside a request, and the honest answer
    for a lineage this code cannot read is the row itself.
    """
    seen: set[str] = set()
    while True:
        if operation.plan_key is not None:
            return plan_operations(session, operation.plan_key)[0]
        parent_id = operation.parent_operation_id
        if parent_id is None or operation.operation_id in seen:
            return operation
        seen.add(operation.operation_id)
        parent = session.get(Operation, parent_id)
        if parent is None:
            return operation
        operation = parent


def _append_plan_item_result_event(
    session, keyring: KeyRing, *, operation: Operation, now: datetime
) -> None:
    """Append a changed result projection once, preserving earlier history.

    Both the message's own operation and plan siblings need durable terminal
    receipts. An earlier running projection must not suppress settlement.
    Replayed reports compare the latest event with the current stored state.
    Operations without a conversation anchor cannot invent a history turn.
    """
    # An initial source_in_progress event is not the final receipt. Keep it
    # in the archive and append the settled projection exactly once, including
    # on a replay that repairs a previously missing terminal event.
    session.refresh(operation)
    existing = (
        session.query(ConversationEvent)
        .filter(
            ConversationEvent.operation_id == operation.operation_id,
            ConversationEvent.event_type == events.OPERATION_RESULT,
        )
        .order_by(ConversationEvent.timeline_sequence.desc())
        .first()
    )
    if existing is not None:
        previous = events._entry(keyring, existing).content
        if previous.get("state") == operation.state:
            return
    anchor = _anchor_event_or_none(
        session, _message_operation(session, operation).operation_id
    )
    if anchor is None:
        return
    # The CAS above wrote the row directly, so the identity map still holds the
    # state this request read; the projection has to describe what was stored.
    session.refresh(operation)
    events.append_event(
        session,
        keyring,
        conversation_id=anchor.conversation_id,
        session_id=anchor.session_id,
        turn_id=anchor.turn_id,
        event_type=events.OPERATION_RESULT,
        content=_operation_event_content(keyring, operation),
        operation_id=operation.operation_id,
        now=now,
    )


def _anchor_event(session, operation_id: str) -> _Anchor:
    """The Session, turn and event the operation's user message was written into."""
    anchor = _anchor_event_or_none(session, operation_id)
    if anchor is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="operation has no anchoring timeline event",
        )
    return anchor


def _expense_record_anchor(session, record_id: str) -> _Anchor:
    """Return the original Timeline turn whose expense receipt names a row.

    The correction route is addressed by ledger row, not operation id, so the
    server resolves the presentation owner from its own durable facts. Only an
    anchored ``finance.log_expense`` result qualifies: another R2 tool can have
    an equal-looking external id in a different table, and a prior correction
    may itself name this row after the first edit.
    """
    row = session.execute(
        text_clause(
            "SELECT ce.conversation_id, ce.session_id, ce.turn_id, ce.event_id "
            "FROM conversation_events AS ce "
            "JOIN operations AS o ON o.operation_id = ce.operation_id "
            "WHERE o.tool = 'finance.log_expense' "
            "AND o.safe_result = :record_id "
            "ORDER BY ce.timeline_sequence LIMIT 1"
        ),
        {"record_id": record_id},
    ).one_or_none()
    if row is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="expense record has no anchoring Timeline receipt",
        )
    return _Anchor(
        conversation_id=row[0],
        session_id=row[1],
        turn_id=row[2],
        event_id=row[3],
    )


def _append_expense_category_corrected(
    session,
    keyring: KeyRing,
    *,
    operation: Operation,
    anchor: _Anchor,
    record_id: str,
    now: datetime,
) -> str | None:
    """Append one durable current-value marker after a verified correction.

    The original operation remains sealed and immutable. The new correction
    operation owns this marker, and replaying its idempotency key sees the
    existing marker instead of appending a duplicate.
    """
    if (
        operation.state != "succeeded"
        or operation.tool != "finance.update_expense_category"
        or operation.safe_result != record_id
        or operation.encrypted_result_record is None
    ):
        return None
    existing = (
        session.query(ConversationEvent)
        .filter(
            ConversationEvent.operation_id == operation.operation_id,
            ConversationEvent.event_type == events.EXPENSE_CATEGORY_CORRECTED,
        )
        .first()
    )
    if existing is not None:
        return None
    record = open_expense_record(
        keyring,
        operation_id=operation.operation_id,
        envelope=operation.encrypted_result_record,
    )
    if record is None or record.category_updated_at is None:
        # The operation still proves the governed write with its record id, but
        # an unreadable presentation payload cannot repaint an older card.
        return None
    return events.append_event(
        session,
        keyring,
        conversation_id=anchor.conversation_id,
        session_id=anchor.session_id,
        turn_id=anchor.turn_id,
        event_type=events.EXPENSE_CATEGORY_CORRECTED,
        content={"record_id": record_id, "record": record.to_dict()},
        operation_id=operation.operation_id,
        now=now,
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
    operation_id: str,
) -> None:
    """Run post-response compaction in a worker-owned database session."""

    if deps.compact_session is None:
        return
    with deps.session_factory() as session:
        try:
            operation = get_operation(session, operation_id)
            anchor = _anchor_event(session, operation_id)
            with deps.recorder.turn(_turn_identity(operation, anchor)):
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
    operation_id: str,
) -> _TurnContext:
    """Bind this turn's assembly, to be run when the model is about to be asked.

    The envelope is built from the persisted anchor event rather than from the
    request body: the archived message is the one the model must answer, and a
    caller-supplied string that disagrees with it is refused by the builder.

    §6's read happens here and nowhere else, because "here" is the moment the
    design names: the last point before the model sees anything, where the
    stripe lock can be held for one read and released before the call. A turn
    that names no image never enters the media store at all.
    """

    def build() -> ContextEnvelope:
        input_parts = _authorized_images(
            deps, session, payload=payload, anchor=anchor, operation_id=operation_id
        )
        return deps.build_envelope(
            session,
            auth,
            conversation_id=payload.conversation_id,
            session_id=anchor.session_id,
            current_event_id=anchor.event_id,
            user_text=payload.text,
            clarification_context=payload.clarification_context,
            finance_retry_context=payload.finance_retry_context,
            input_parts=input_parts,
        )

    return _TurnContext(build)


def _authorized_images(
    deps: AgentApiDeps,
    session,
    *,
    payload: ChatRequestPayload,
    anchor: _Anchor,
    operation_id: str,
) -> tuple[InputPart, ...]:
    """§6's read for this turn, or a refusal the orchestrator can classify.

    §8's switch is asked once more here. The entry guard and the anchor have
    both asked it already, and asking a third time is the point rather than
    duplication: this is the last moment at which refusing costs nothing -- no
    bytes have left the store, no model has been called -- and §8 requires the
    server to re-validate rather than honour a client's cached capability.
    """
    media_ids = _chat_image_ids(payload.parts)
    if not media_ids:
        return ()
    capability = deps.image_capability()
    if not capability.enabled:
        # The same refusal the entry guard gives, reached by a different route:
        # a switch that closed between the message being accepted and the model
        # being asked. `UNSUPPORTED_OPERATION` is what the client saw then, so
        # it is what the client sees now.
        raise AppError(
            ErrorCode.UNSUPPORTED_OPERATION,
            internal_detail=capability.refusal(),
        )
    store, limits = _media_ready(deps)
    try:
        images = read_authorized_images(
            session,
            store=store,
            keyring=deps.keyring,
            limits=limits,
            event_id=anchor.event_id,
            operation_id=operation_id,
            media_ids=media_ids,
        )
        return ((TextInputPart(payload.text),) if payload.text else ()) + images
    except MediaError as error:
        raise _media_read_refusal(error) from error


def _media_read_refusal(error: MediaError) -> AppError:
    """Map one §6 read refusal onto the code the orchestrator acts on.

    The four classified refusals keep the door's own mapping
    (`_media_refusal`), because they mean the same thing at both ends and a
    client should read the same answer either way -- including `MEDIA_BUSY`,
    which stays *out* of `_CONTEXT_FAILURES` so the idempotency key remains
    re-runnable (§4.1's "每次等待有界，任务可重试").

    What differs is the unclassified rest. At the door those refusals are the
    client's own mistake and `INVALID_ARGUMENT` is honest; here, after §3.2 has
    already accepted the message, the same refusal can only be a server-side
    integrity failure -- a `ready` object with no seal record, or one whose
    measured type is no longer allowed. Blaming the request for that would send
    the client looking in the wrong place, so it becomes the loud code instead:
    the operation stays re-runnable and the failure is a 500 the operator sees.
    """
    mapped = _media_refusal(error)
    if mapped.code is ErrorCode.INVALID_ARGUMENT:
        return AppError(ErrorCode.INTERNAL_ERROR, internal_detail=str(error))
    return mapped


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
    deps: AgentApiDeps, auth: AuthContext, operation_id: str
) -> JSONResponse:
    with deps.session_factory() as session:
        operation = _owned_operation(
            session, operation_id, device_id=auth.device_id
        )
        return _operation_response(
            deps.keyring,
            operation,
            client_wire_version=auth.client_wire_version,
        )


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
            # `chat_receipt_projection_v4` contract the iOS client reads from a
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


def _process_device_action_result(
    deps: AgentApiDeps,
    auth: AuthContext,
    action_id: str,
    body: dict[str, Any],
) -> JSONResponse:
    """Settle the operation a device report names, exactly once.

    Reads and writes only the Agent database -- the phone's report *is* the
    external evidence, so no fact source is contacted and the retrying commit
    is safe. The CAS in `transition_operation` is the one-shot guarantee: a
    retry that arrives after settlement re-projects the winner's state rather
    than moving anything.
    """
    _closed_device_result_body(body)
    result = body["result"]
    event_id = body.get("event_id")
    detail = body.get("detail")
    if result in DEVICE_REPORT_WRITES and (
        not isinstance(event_id, str) or not event_id.strip()
    ):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="a created or duplicate report must carry an event_id",
        )
    if detail is not None and not isinstance(detail, str):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="detail must be a string or null",
        )
    with deps.session_factory() as session:
        def work():
            operation = _owned_action_operation(
                session, action_id, device_id=auth.device_id
            )
            # This endpoint settles *device-executed* actions. Ownership alone
            # is not enough: a Finance operation parked by its own execution
            # path must not be settleable by a phone POST claiming a calendar
            # write it never held. Derived from the IR, so a second device
            # tool is admitted automatically.
            if not any(
                contract.name == operation.tool and contract.executor == "device"
                for contract in TOOL_CONTRACTS
            ):
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail=(
                        f"action {action_id} is not a device-executed tool"
                    ),
                )
            session.refresh(operation)
            if not is_terminal(operation.state):
                now = deps.now()
                if result in DEVICE_REPORT_WRITES:
                    # Success evidence: the phone holds the event. Two hops,
                    # both audited, so the receipt carries the EventKit id.
                    transition_operation(
                        session,
                        operation_id=operation.operation_id,
                        current_state=operation.state,
                        current_version=operation.state_version,
                        target_state="verifying",
                        now=now,
                    )
                    session.refresh(operation)
                    transition_operation(
                        session,
                        operation_id=operation.operation_id,
                        current_state=operation.state,
                        current_version=operation.state_version,
                        target_state="succeeded",
                        now=now,
                        safe_result=event_id,
                        # Kept verbatim, and kept at all, because `created` and
                        # `duplicate` are not interchangeable downstream: both
                        # settle here with the EventKit id above, and only the
                        # report itself says whether the phone *found* the event
                        # or *made* it. 「仍要创建」 may be offered for exactly
                        # one of those (design 3.3).
                        device_result=result,
                    )
                else:
                    # The device refused before any write could exist: the
                    # fact source's own zero-write testimony.
                    reason = (
                        _DEVICE_REPORT_REASONS[result]
                        if detail is None
                        else f"{_DEVICE_REPORT_REASONS[result]}: {detail}"
                    )
                    transition_operation(
                        session,
                        operation_id=operation.operation_id,
                        current_state=operation.state,
                        current_version=operation.state_version,
                        target_state="failed_safe",
                        now=now,
                        failure_reason=reason,
                        device_result=result,
                    )
                session.refresh(operation)
            # Every item of a plan gets its own receipt line (design 4.2). Item
            # 0's is the one the issuing turn wrote; a sibling's is written here
            # or nowhere, and nowhere is a settled write nobody is ever shown.
            _append_plan_item_result_event(
                session, deps.keyring, operation=operation, now=deps.now()
            )
            # A settled operation (including one this request did not move --
            # the loser of a CAS race, or a replay) answers its current state.
            return _operation_response(
                deps.keyring,
                operation,
                client_wire_version=auth.client_wire_version,
            )

        return _commit(session, work)


def _process_device_action_override(
    deps: AgentApiDeps,
    auth: AuthContext,
    action_id: str,
) -> JSONResponse:
    """Re-issue a calendar write the user answered 「仍要创建」 to (design 3.3).

    The decision is a server-side binding, not a new conversation turn. A turn
    would ask the model to produce the call again -- and a turn is exactly what
    a double tap, a retry whose response was lost, or two concurrent taps each
    produce, giving two independently valid actions and two real events. So the
    endpoint derives *one* key from the original operation
    (`uuid5(namespace, "<operation_id>:calendar-override")`) and calls the same
    INSERT-or-get the rest of the API uses: the first tap creates the derived
    operation, and every later one reads it back and answers its current
    projection. The client never supplies this key and cannot compute it for
    another device's action, which is what keeps a predictable key the
    mechanism here rather than a hazard.

    The new operation carries `parent_operation_id`, so the audit shows two
    operations -- the write that found the duplicate and the deliberate second
    one -- rather than one silent pass. It resumes the *sealed* arguments the
    original turn was authorised to make, never a re-derivation, and the device
    is told to skip its own lookup, which is the whole point of the decision.

    Only a `succeeded` operation whose report was `duplicate` qualifies; every
    other state, including a settled one, is refused with `INVALID_ARGUMENT`.
    """
    action_keyring = deps.action_keyring
    if action_keyring is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="the override endpoint is not composed",
        )
    with deps.session_factory() as session:
        def work():
            source = _owned_action_operation(
                session, action_id, device_id=auth.device_id
            )
            if not may_override(
                tool=source.tool,
                state=source.state,
                device_result=source.device_result,
            ):
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail=(
                        f"action {action_id} is not a duplicate a user may override"
                    ),
                )
            if source.encrypted_request is None:
                # The row says it is overridable and carries nothing to resume.
                # Only a row that predates the retained request can be in this
                # state, and guessing its arguments is the one thing an override
                # must never do.
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail=(
                        f"action {action_id} kept no request to resume"
                    ),
                )
            try:
                intent = open_operation_request(
                    action_keyring,
                    operation_id=source.operation_id,
                    envelope=source.encrypted_request,
                )
            except OperationRequestError as unreadable:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail=str(unreadable),
                ) from unreadable

            opened = open_operation(
                session,
                device_id=auth.device_id,
                client_request_id=override_key(source.operation_id),
                request_fingerprint=override_fingerprint(source.operation_id),
                now=deps.now(),
                parent_operation_id=source.operation_id,
            )
            derived = opened.operation
            if derived.state == "accepted":
                # Sealed *before* the run, because this is what makes the
                # derived operation pre-resolved: the orchestrator recognises
                # an override by the parent lineage together with this seal, and
                # runs it without a model. The seal is rewritten in the same
                # transition that issues the new action, where the freshly
                # attested arguments are in hand.
                derived.encrypted_request = seal_operation_request(
                    action_keyring,
                    operation_id=derived.operation_id,
                    intent=intent,
                )
                session.flush()
                # The override joins the source message's turn when the source
                # has one: it is the same user request, continued, and its own
                # record is appended under that turn's identity rather than a
                # fabricated one. A press is still not a chat turn, though, so a
                # source with no anchor is a legitimate shape here (as it is for
                # the other card-driven actions) and the record is labelled by
                # the operation, trace and device instead of a guessed turn.
                anchor = _anchor_event_or_none(session, source.operation_id)
                with deps.recorder.turn(_turn_identity(derived, anchor)):
                    run_operation(
                        session,
                        derived,
                        # No model: the write is the one already authorised.
                        build_context=None,
                        interpreter=deps.build_interpreter(auth),
                        dispatcher=deps.build_dispatcher(auth, derived.trace_id),
                        authorize=deps.build_authorizer(auth),
                        keyring=deps.keyring,
                        now=deps.now,
                        recorder=deps.recorder,
                        action_keyring=action_keyring,
                    )
            session.refresh(derived)
            # The derived operation's own projection is the answer, on both the
            # first tap and every replay: a parked operation hands over its
            # action through the same delivery door every other response uses
            # (and under the same capability gate), and a settled one reports
            # what the phone did with it.
            return _operation_response(
                deps.keyring,
                derived,
                client_wire_version=auth.client_wire_version,
            )

        # Retrying is safe here, and required: the device fork's `resolve` only
        # *reads* our control plane (the calendar directory) and the write
        # itself happens on the phone, after this response is delivered -- so a
        # re-run repeats an idempotent read and nothing else. It is also what
        # makes a double tap converge: two taps are a read-then-write each, and
        # the loser of that shape cannot upgrade its snapshot once the winner
        # commits (SQLite refuses immediately; `busy_timeout` cannot help), so
        # without the retry the second tap would be a 500 rather than the same
        # projection.
        return _commit(session, work)


#: The stable failure reasons a refused report records. The device's report is
#: the evidence; the reason only names which closed value carried it. Which
#: results are *writes* is not restated here: `DEVICE_REPORT_WRITES` is the
#: same constant the operations CHECK constraint is built from, so a value the
#: database would accept can never be one this endpoint refuses, or the reverse.
_DEVICE_REPORT_REASONS: Final[dict[str, str]] = {
    "denied": "DEVICE_ACTION_DENIED",
    "failed": "DEVICE_EXECUTION_FAILED",
}
_DEVICE_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"result", "event_id", "detail"}
)


def _closed_device_result_body(body: dict[str, Any]) -> None:
    """Validate the closed report body before anything can settle."""
    unexpected = sorted(set(body) - _DEVICE_RESULT_FIELDS)
    if unexpected:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"unexpected fields in request body: {unexpected}",
        )
    result = body.get("result")
    if result not in _DEVICE_REPORT_REASONS and result not in DEVICE_REPORT_WRITES:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=(
                "result must be one of created, duplicate, denied, failed"
            ),
        )


def _validate_sync_body(body: dict[str, Any]) -> None:
    """Refuse a whole sync batch that does not match the ingest contract.

    One malformed event refuses the batch entire (§5.1: no silent triage) --
    the device is told to resend a coherent snapshot, never to have the server
    guess which half it meant. The schema is the IR's own, so a contract
    change moves both gates together.
    """
    try:
        Draft202012Validator(
            _SYNC_INGEST_CONTRACT, format_checker=FormatChecker()
        ).validate(body)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=(
                "calendar sync body failed schema validation at "
                f"{list(exc.absolute_path)}"
            ),
        ) from exc


def _category_correction_fingerprint(
    record_id: str, category: str, expected: str | None
) -> str:
    """Bind an idempotency key to this exact correction.

    All three values are in it, `expected_current_category` included. Two
    corrections of the same row to the same category from *different* believed
    starting points are different requests: one of them is working from a stale
    view, and letting them share a key would let the stale one replay as the
    fresh one's success.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "route": "expense_category_correction",
                "record_id": record_id,
                "category": category,
                "expected_current_category": expected,
            }
        ).encode("utf-8")
    ).hexdigest()


def _process_category_correction(
    deps: AgentApiDeps,
    auth: AuthContext,
    record_id: str,
    category: str,
    expected: str | None,
    key: str,
) -> JSONResponse:
    """Run one category correction as a pre-resolved operation.

    `retry=False`, like the duplicate-decision route beside it and for the same
    reason (§5.2): this unit dispatches outside the database. Re-running it
    against fresh state would re-send the correction, and a governed write is
    never something to replay because a local transaction lost a race.
    """
    intent = WriteIntent(
        tool="finance.update_expense_category",
        model_args={
            "record_id": record_id,
            "category": category,
            "expected_current_category": expected,
        },
    )
    with deps.session_factory() as session:
        def work():
            # Resolve the receipt's Timeline ownership before opening an
            # operation or contacting Finance. Otherwise a guessed record id
            # could mutate the ledger and leave no durable correction fact for
            # an app restart to replay.
            anchor = _expense_record_anchor(session, record_id)
            opened = open_operation(
                session,
                device_id=auth.device_id,
                client_request_id=key,
                request_fingerprint=_category_correction_fingerprint(
                    record_id, category, expected
                ),
                now=deps.now(),
            )
            operation = opened.operation
            if opened.created:
                operation.api_request.encrypted_request_payload = seal_intent(
                    deps.keyring,
                    request_id=operation.request_id,
                    intent=intent,
                )
                session.flush()
            if operation.state == "accepted":
                with deps.recorder.turn(_turn_identity(operation, anchor)):
                    run_operation(
                        session,
                        operation,
                        # Nothing to assemble: the picker's tap is the whole
                        # instruction, and no model is consulted.
                        build_context=None,
                        interpreter=deps.build_interpreter(auth),
                        dispatcher=deps.build_dispatcher(auth, operation.trace_id),
                        authorize=deps.build_authorizer(auth),
                        keyring=deps.keyring,
                        now=deps.now,
                        pre_resolved=True,
                        recorder=deps.recorder,
                    )
            try:
                _append_expense_category_corrected(
                    session,
                    deps.keyring,
                    operation=operation,
                    anchor=anchor,
                    record_id=record_id,
                    now=deps.now(),
                )
            except Exception:  # noqa: BLE001 - see below
                # The marker is what lets an older receipt resolve to the new
                # category after a restart; it is not what makes the correction
                # true. By this point the ledger row is changed and verified and
                # the operation is `succeeded`, so letting an append failure
                # raise would report a completed governed write as a 500 — and
                # invite a retry for something that already happened.
                #
                # The degradation is visible and bounded: the card shows the new
                # category now and reverts to the stored one after a relaunch,
                # which is exactly the state this whole change started from.
                logger.warning(
                    "category correction marker dropped operation_id=%s",
                    operation.operation_id,
                    exc_info=True,
                )
            return _operation_response(
                deps.keyring,
                operation,
                client_wire_version=auth.client_wire_version,
            )

        return _commit(session, work, retry=False)


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
                # The override joins the source message's turn, and its own
                # Timeline event is appended only after this write, so the
                # identity is built from the source's anchor. Without this scope
                # the confirmed duplicate write -- the highest-risk operation
                # class there is -- would record no result at all, and the tool
                # call the wrapped dispatcher still writes would land with no
                # operation to group it under.
                with deps.recorder.turn(_turn_identity(new_op, anchor)):
                    run_operation(
                        session,
                        new_op,
                        # A `write anyway` operation carries its resolved intent
                        # and never reaches the model, so it assembles no context.
                        build_context=None,
                        interpreter=deps.build_interpreter(auth),
                        dispatcher=deps.build_dispatcher(auth, new_op.trace_id),
                        authorize=deps.build_authorizer(auth),
                        keyring=deps.keyring,
                        now=deps.now,
                        recorder=deps.recorder,
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
            return _operation_response(
                deps.keyring,
                target,
                client_wire_version=auth.client_wire_version,
            )

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


def _owned_action_operation(
    session, action_id: str, *, device_id: str
) -> Operation:
    """Locate the operation a device action id names, for its own device.

    An action id *is* the operation's idempotency key, which is why the two
    device endpoints (the report and the override) look it up this way rather
    than by `operation_id`: the phone only ever holds the action id. Not "no
    such action" and not "another device's action" -- one opaque refusal that
    maps out no surface.
    """
    operation = (
        session.query(Operation)
        .filter(Operation.idempotency_key == action_id)
        .filter(Operation.api_request.has(device_id=device_id))
        .one_or_none()
    )
    if operation is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=(
                f"no operation anchored for action {action_id} on this device"
            ),
        )
    return operation


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


async def _json_body(
    request: Request, *, max_bytes: int = _MAX_JSON_BODY_BYTES
) -> dict[str, Any]:
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
        if declared_length < 0 or declared_length > max_bytes:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"JSON body exceeds {max_bytes} bytes",
            )

    raw = await request.body()
    if len(raw) > max_bytes:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"JSON body exceeds {max_bytes} bytes",
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
        content=_operation_event_content(keyring, target),
        operation_id=target.operation_id,
        now=now,
    )


def _operation_event_content(
    keyring: KeyRing, operation: Operation
) -> dict[str, Any]:
    """The Timeline fact projection of one operation result.

    `tool` is included even when null, so a new event carries the server's
    recorded tool explicitly -- the client distinguishes "no tool recorded"
    from "an old event that predates tool recording". `query_result` is present
    only for a `finance.query_expenses` result that decoded; anything else fails
    closed to an absent field rather than a raw dump.
    """
    # A Timeline event is history, not a hand-off, and it deliberately never
    # carried the action: `device_actions` is not among the names copied below.
    # Wire v4 preserves v2 result envelopes in history. Delivery endpoints
    # separately enforce each client version; this grants no device action.
    projection = _operation_projection(
        keyring, operation, client_wire_version=4
    )
    content: dict[str, Any] = {
        "state": projection["state"],
        "tool": projection["tool"],
    }
    for name in (
        # The domain travels with the history too: a card re-rendered from the
        # Timeline after a restart must still know that its 人工核对 asks about
        # the calendar rather than the ledger. Absent exactly when no tool was
        # recorded, which is the same case the card cannot word either way.
        "domain",
        "record_id",
        # `G1`. History and the live receipt draw the same card, so the fields
        # travel on the event too -- otherwise scrolling back would silently
        # demote a full card to a bare status row. The event's own column is
        # already sealed (`conversation_events.encrypted_content`), so this
        # copy is no more exposed than the dialogue beside it.
        "record",
        "query_result",
        "answer",
        "clarification",
        "duplicate_check_id",
        "duplicate_existing",
        "failure_reason",
        # A calendar receipt says which of the two successes it was, and names
        # the action a 「仍要创建」 would re-issue (design 3.3). `device_action_id`
        # is the idempotency key *alone* -- not the sealed action, which stays
        # out of history for the reason above: the key lets a card point at an
        # action that is already the user's, while the action itself would be a
        # hand-off frozen into a scroll-back. An event written before these two
        # fields existed carries neither, and a card falls back to reading the
        # operation rather than reading "created" into a missing fact.
        "device_result",
        "device_action_id",
    ):
        value = projection.get(name)
        if value is not None:
            content[name] = value
    return content


def _chat_request(body: dict[str, Any], deps: AgentApiDeps) -> tuple[Parts, str]:
    """The request's parts and effective text, from exactly one of `text` and `parts`.

    §3.1: "text 与 parts 恰有一个". A parts request carries its text inside the
    parts, so the two are not merged and not defaulted: sending both is a
    refusal rather than a choice of which one wins, because a caller who sends
    both has a different request in mind than the server can guess at.

    For a parts request the effective text is :func:`parts_text`, which is `""`
    when the user sent images and nothing else -- a legitimate request, and the
    reason this cannot simply delegate to `_required`. The parts themselves
    travel on rather than being reduced to that text: they are what the request
    *is*, from the fingerprint (§3.2) through to the sealed payload.
    """
    has_parts = "parts" in body
    has_text = "text" in body
    if has_parts and has_text:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="text and parts are mutually exclusive",
        )
    if not has_parts:
        return (), _required(body, "text")
    parts = parse_chat_parts(body["parts"])
    _require_multimodal_ready(deps)
    return parts, parts_text(parts)


def _chat_image_ids(parts: Parts) -> list[str]:
    """The media ids this message names, in order and once each.

    Duplicates cannot arrive from the wire (`parse_chat_parts` allows one image
    part), and the lock helper dedupes independently -- this is a list of what
    to lock, not a claim about the request.
    """
    return [
        part.media_id for part in parts if isinstance(part, ImageRefPart)
    ]


def _require_multimodal_ready(deps: AgentApiDeps) -> None:
    """Refuse a parts request this deployment may not serve.

    The whole chain an image needs now exists -- §8's switch (#13) and §6's
    lock-scoped authorized read (`media_read.read_authorized_images`) -- so what
    is left to ask is §8's question and only that: may this deployment serve
    images at all. It is a policy made of decisions people made and facts about
    what is composed, and a refusal names every term that closed it rather than
    the first, so an operator does not have to restart the service to find out
    there was a second reason.
    """
    capability = deps.image_capability()
    if capability.enabled:
        return
    raise AppError(
        ErrorCode.UNSUPPORTED_OPERATION,
        internal_detail=capability.refusal(),
    )


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
    keyring: KeyRing,
    operation: Operation,
    *,
    client_wire_version: int,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    # A parked or in-flight operation is 202; a resolved one is 200. The client
    # polls the same projection either way. `extra` carries transient fields that
    # are not persisted on the operation (a clarification question, the existing
    # duplicate record), returned on the immediate reply only.
    from personal_agent.api.operation_state import is_terminal

    projection = _operation_projection(
        keyring, operation, client_wire_version=client_wire_version
    )
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
    # The device action used to ride here as a transient field, which made the
    # chat response the action's only delivery channel: a request that timed
    # out at 202 lost the action while the operation stayed parked. The action
    # is now sealed on the operation and delivered by `_operation_projection`
    # while the operation sits at `source_in_progress` (review R6, 2026-09-08),
    # so the 200 reply, the by-id poll and a replay all answer through the one
    # door. The worker's copy is deliberately dropped -- two channels would
    # mean two answers about what was handed over.
    return fields


def _parked_plan_actions(
    keyring: KeyRing, operation: Operation, *, client_wire_version: int
) -> list[dict[str, Any]]:
    """Every device action this turn still owes the phone, in plan order.

    A message that asked for several things is one frozen plan (design 4.1), and
    its items are separate operations. The user's message is the row being
    polled, so this is where the whole list has to appear: an item the projection
    did not reach would be issued to nobody. A single-action message is a plan of
    one and carries no plan key, so both shapes read the same way here.

    Each item answers for itself. An envelope that will not open (wrong key,
    tampering) is omitted rather than guessed: that item stays parked and the
    timeout sweep is its witness, exactly as if nothing had been sealed -- and
    its siblings are unaffected, because they are different rows.

    The delivery gate (design 2.5.3) is applied per action. The issuance gate
    already refused a client that cannot implement this action, but it reads the
    version of a *different* request: the one that issued. Between issuing and
    delivering, the same phone can be restored, downgraded or replaced by an
    older build, and this call is the last moment anyone can tell. So the sealed
    action's own `wire_version` -- not the contract's, which a later IR change
    could raise past what was actually sealed -- is compared against the caller's
    claim, and a shortfall withholds the action rather than degrading it. The
    operation stays parked and the 15-minute sweep settles it into
    needs_manual_review, which is the honest terminal state: the phone may or may
    not have written, and no client was told otherwise.
    """
    handed: list[dict[str, Any]] = []
    for row in _plan_rows(operation):
        if (
            row.state != "source_in_progress"
            or row.tool not in _DEVICE_EXECUTED_TOOLS
            or row.encrypted_device_action is None
        ):
            continue
        action = open_device_action(
            keyring,
            operation_id=row.operation_id,
            envelope=row.encrypted_device_action,
        )
        if action is None:
            continue
        if client_supports_wire_version(
            client=client_wire_version, required=action["wire_version"]
        ):
            handed.append(action)
    return handed


def _plan_rows(operation: Operation) -> list[Operation]:
    """This operation's frozen plan in order, or the operation on its own.

    A message with several actions is one plan and its items are separate rows,
    and the whole list is delivered through the plan's *first* item -- the
    message the user is polling. A later item answers only for itself: the
    phone reaches it to report a result, and having it recite its siblings'
    actions back would be handing over what was already handed over. Every
    other operation is a plan of one, so a caller never needs two shapes.
    """
    if operation.plan_key is None or operation.plan_index != 0:
        return [operation]
    session = object_session(operation)
    if session is None:  # pragma: no cover - every caller reads inside a session
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="a frozen action plan was projected outside its session",
        )
    return plan_operations(session, operation.plan_key)


def _operation_projection(
    keyring: KeyRing, operation: Operation, *, client_wire_version: int
) -> dict[str, Any]:
    from personal_agent.api import runtime_v2
    db = object_session(operation)
    v2_answer = runtime_v2.project(db, keyring, operation, client_wire_version) if db is not None else None
    projection = {
        "operation_id": operation.operation_id,
        "state": operation.state,
        "cancel_requested": operation.cancel_requested,
        "client_detached": operation.client_detached,
        "tool": operation.tool,
        # Which domain the operation belongs to, so a card can be chosen by
        # domain instead of by guessing from a tool name (design §10, gap 4:
        # a `needs_manual_review` calendar write asks the user to look in the
        # calendar, not in the ledger). Derived from the tool's own IR
        # contract, never a list beside the IR — the same rule the record and
        # query evidence sets follow. Null when no tool was recorded: an old
        # event's domain is unknown, and unknown is not a default.
        "domain": domain_of_tool(operation.tool),
        "record_id": None,
        "failure_reason": operation.failure_reason,
        "duplicate_check_id": operation.duplicate_check_id,
        # What the phone decided, kept distinct from `state` because the two are
        # not the same fact: `created` and `duplicate` both settle as `succeeded`
        # with the event id above, and only this says whether the phone *made*
        # the event or *found* it. 「仍要创建」 may be offered for exactly one of
        # those (design 3.3), so a receipt that folded them together would put a
        # button on the wrong card. Null for every Finance operation, and null on
        # a receipt written before this field existed.
        "device_result": operation.device_result,
        # The action id the device reports and overrides by. It *is* the
        # operation's idempotency key (`_owned_action_operation`), and it is
        # emitted only for a tool the IR marks device-executed: for anything
        # else an idempotency key is an internal key that no card has business
        # naming, and a client that offered to re-issue one would be offering to
        # replay a connector write. Fail closed -- an unrecognised tool yields
        # null rather than the key.
        "device_action_id": (
            operation.idempotency_key
            if operation.tool in DEVICE_EXECUTED_TOOL_NAMES
            else None
        ),
    }
    # Delivery-or-refusal (review R6, 2026-09-08). An issued action is sealed on
    # its own row, and this is its one delivery door: the 200 reply, the by-id
    # poll and a replay all hand over the same authorised action. Each row
    # answers for itself -- a settled one has already had its seal cleared
    # (`device_action_only_while_parked`), so it drops out without a second
    # rule, and the read-back can never re-arm a finished write.
    handed = _parked_plan_actions(
        keyring, operation, client_wire_version=client_wire_version
    )
    if handed:
        # Always a list, even for the single action a v1-shaped request issues:
        # a client that switched on length would otherwise need two decode paths
        # for one contract (design 2.5.4).
        projection["device_actions"] = handed
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
            if operation.tool in _QUERY_RESULT_TOOLS:
                # A query result is only ever projected through the strict
                # decoder. A safe_result that does not decode is a wiring bug or
                # tampering, and both fail closed: no raw JSON as answer, no
                # query card. `answer` stays the compatibility fallback for old
                # clients, derived deterministically from the same projection.
                try:
                    query = decode_finance_query_projection(operation.safe_result)
                except FinanceQueryProjectionError:
                    pass
                else:
                    projection["query_result"] = query.to_dict()
                    projection["answer"] = summarise_query_projection(query)
            elif operation.tool in _CALENDAR_QUERY_RESULT_TOOLS:
                # Same fail-closed discipline for the calendar mirror read:
                # the safe_result must decode as the whitelisted calendar
                # projection or nothing is shown. The decoder returns exactly
                # the display dict, so `query_result` is the projection itself.
                try:
                    calendar_query = decode_calendar_query_projection(
                        operation.safe_result
                    )
                except CalendarQueryProjectionError:
                    pass
                else:
                    projection["query_result"] = calendar_query
                    projection["answer"] = summarise_calendar_projection(calendar_query)
            else:
                projection["answer"] = operation.safe_result

    # `G1`: the business fields the receipt card draws. Strictly subordinate to
    # `record_id` -- it is emitted only where a record id is, so a card can
    # never show a name and an amount for a write this projection did not also
    # state as recorded. An envelope that will not open resolves to absent
    # (`open_expense_record` fails closed), which costs the card its rows and
    # never the receipt itself.
    if projection["record_id"] and operation.encrypted_result_record is not None:
        record = open_expense_record(
            keyring,
            operation_id=operation.operation_id,
            envelope=operation.encrypted_result_record,
        )
        if record is not None:
            projection["record"] = record.to_dict()
    if v2_answer is not None:
        projection["result_envelope"] = v2_answer
        projection["answer"] = v2_answer["text"]
    return projection


def _error_response(error: AppError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(error.code, 500)
    envelope = error.to_envelope().model_dump(mode="json")
    return JSONResponse({"error": envelope}, status_code=status)
