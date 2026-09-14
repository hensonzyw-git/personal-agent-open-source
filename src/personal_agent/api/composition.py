"""Where the Agent API service actually gets its keys, model and connectors.

`DEV-027`, the last piece. Every collaborator in `api/` is written so that it
*cannot* load anything: the FastAPI app, the orchestrator, the dispatcher and
the interpreter all receive their dependencies. That discipline is only worth
something if there is exactly one place where those are loaded, and this is it
-- the Agent-side twin of `personal_data_mcp/server/composition.py`.

The refusals here are the ones that keep a credential from leaving this host:

- **the Finance MCP and control URLs must be loopback.** Every governed call
  carries a signed Host Context, and every control read carries a control token.
  A URL pointing anywhere else would send those off the machine, so it is
  refused at composition, before a socket exists;
- **the model endpoint is pinned inside the gateway**, and the gateway is built
  at boot, so a tampered `MODEL_API_BASE` fails at startup rather than on
  Henson's first message;
- **an empty tool catalog is a refusal**, because it is what a URL pointing at
  the wrong server looks like.

Two freshness properties are structural rather than incidental:

- the **device row is re-read on every authorisation and every dispatch**, never
  cached in a closure. A device revoked during a 25-second model turn must not
  be able to commit a write when the turn ends;
- each governed call opens its own short-lived MCP connection (measured at
  ~21 ms at `DEV-015`). The connection made here is used for discovery only, so
  a connector that would need a long-lived session is refused rather than
  silently sharing one across event loops.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Final
from urllib.parse import urlsplit

from personal_agent.api.app import AgentApiDeps, AuthContext
from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
    RecordFields,
    RecordUnavailable,
    require_loopback_url,
)
from personal_agent.api.finance_dispatcher import (
    DispatcherContext,
    McpFinanceDispatcher,
    tool_call_fingerprint,
)
from personal_agent.api.orchestrator import (
    CommitFailedSafe,
    CommitOutcome,
    Dispatcher,
    ResolveFailedSafe,
    ResolveOutcome,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.api.recovery import FinanceExecutionStatus, recover_pending
from personal_agent.api.operation_store import (
    new_traceparent,
    sweep_timed_out_device_actions,
)
from personal_agent.auth.enrollment import decode_device_scopes
from personal_agent.context.builder import ContextBuilder, ContextEnvelope
from personal_agent.context.compactor import Compactor
from personal_agent.context.config import (
    ContextConfig,
    default_context_config,
    operator_override_from_env,
)
from personal_agent.context.continuation import (
    ClarificationContext,
    FinanceRetryContext,
)
from personal_agent.keys import (
    load_access_token_ring,
    load_agent_data_keyring,
    load_cursor_key,
    load_identifier_key,
    load_service_signing_ring,
)
from personal_agent.media.config import MediaConfig
from personal_agent.mcp_client.core import (
    McpClientCore,
    McpTimeoutError,
    McpTransportError,
    StreamableHttpTransport,
)
from personal_agent.mcp_client.registry import ConnectorRegistry, TrustLevel
from personal_agent.policy.bridge import (
    BridgeCallContext,
    DeviceAuthorization,
    GovernedToolBridge,
)
from personal_agent.runtime.glm_gateway import (
    declared_context_limit,
    glm_gateway_from_env,
)
from personal_agent.runtime.model_providers import (
    provider_from_env,
    resolved_model_id,
)
from personal_agent.runtime.modality import (
    ImageCapability,
    image_capability,
    master_switch,
)
from personal_agent.diagnostics.recording_dispatcher import RecordingDispatcher
from personal_agent.diagnostics.transcript import (
    TranscriptRecorder,
    recorder_from_env,
)
from personal_agent.context.compact_state import CheckpointCompactStateProvider
from personal_agent.context.session_manager import SessionManager
from personal_agent.runtime.compactor_provider import GlmCompactorProvider
from personal_agent.runtime.model_gateway import ModelGatewayError
from personal_agent.runtime.interpreter import ModelInterpreter
from personal_agent.runtime.model_input import InputPart
from personal_agent.runtime.prompt import build_system_prompt
from personal_agent.runtime.session_classifier import GlmBoundaryClassifier
from personal_agent.runtime.structured import (
    CLASSIFIER_MODEL_ENV,
    CLASSIFIER_TIMEOUT_SECONDS,
    StructuredCallError,
    structured_client_from_env,
)
from personal_agent.storage.engine import (
    check_integrity,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import HostContext, ISSUER
from personal_agent_core.manifest import load_manifest
from personal_agent_core.mcp_protocol import FINANCE_PROTOCOL_VERSIONS
from personal_agent_core.timeutil import (
    format_ledger_date,
    ledger_date,
    utc_now,
)
from personal_agent_core.write_switch import WriteSwitch


logger = logging.getLogger(__name__)


#: The Agent's own identity in the Host Context, fixed by design 4.3.
AGENT_ID: Final[str] = ISSUER

#: The one trusted Personal Data connector. Its alias namespace is unprefixed,
#: which is exactly why nothing else may ever be registered under this id.
FINANCE_CONNECTOR_ID: Final[str] = "personal-data"

LEDGER_TIMEZONE: Final[str] = "Asia/Shanghai"
RECOVERY_INTERVAL_SECONDS: Final[float] = 60.0

#: The device-side data channel: the sync route executes this connector tool
#: through the governed bridge with the authenticated device as the Host
#: Context caller (`model_callable=False`, so the model never offers it).
_CALENDAR_INGEST_TOOL: Final[str] = "calendar.ingest_events"


class CompositionError(RuntimeError):
    """The service cannot be composed safely and must not start."""


@dataclass(frozen=True)
class AgentServiceConfig:
    """Everything the service needs that is not a secret.

    Secrets come from the environment (systemd credentials in production, a
    mode-600 file sourced into the shell locally); no path here ever names one.
    """

    database: Path
    #: The Finance MCP Streamable HTTP endpoint, e.g. `http://127.0.0.1:8811/mcp`.
    finance_mcp_url: str
    #: The Finance internal control base, e.g. `http://127.0.0.1:8811`.
    finance_control_url: str
    #: The single user this backend serves. Never guessed: it names a person and
    #: travels into Finance's audit trail.
    user_id: str
    agent_id: str = AGENT_ID
    connector_id: str = FINANCE_CONNECTOR_ID
    #: The configured server-side allowlist. `None` means every enabled contract;
    #: a narrower set is how a read-only rollout is expressed.
    allowed_tools: frozenset[str] | None = None
    sync_wait_seconds: float = 30.0
    #: `DEV-031`. The Feishu ledger URL this service names to enrolled devices
    #: (Henson's 2026-07-30 decision: the client never invents it, like the
    #: canonical Timeline id). It is a resource identifier from local
    #: configuration, not a secret; `None` omits the field from
    #: `/v1/capabilities` and the app then offers no jump.
    ledger_url: str | None = None
    #: `#18`. §4.3's versioned ceilings and the storage root. `None` means the
    #: media surface is not composed -- §5.4's "缺配置不启用图片" -- and the
    #: five routes then refuse rather than running half-configured. The store
    #: is built from it here, because it needs the data keyring and this is the
    #: one place that has both.
    media: MediaConfig | None = None
    v2_device_ids: frozenset[str] = frozenset()
    v2_execution_enabled: bool = True
    search_config: Any = None


@dataclass
class ComposedAgentService:
    """The live dependencies, plus what an operator needs to see at boot."""

    deps: AgentApiDeps
    bridge: GovernedToolBridge
    catalog_aliases: tuple[str, ...]
    quarantined: tuple[str, ...] = field(default=())


def image_capability_from(
    config: AgentServiceConfig,
) -> Callable[[], ImageCapability]:
    """§8's switch, bound to this deployment's provider, model and media surface.

    Built here rather than inside the app layer so there is exactly one place
    that knows which model is in use. The provider and the model id come from
    the same resolvers the gateway uses (`provider_from_env`,
    `resolved_model_id`), so the model the evidence is checked against is the
    model that will be sent -- a capability computed from a second reading of
    `MODEL_ID` would be evidence about a model nobody calls.

    The closure re-reads the master switch on every call. The rest cannot move
    without a restart: the provider, the model id and the composed media
    surface are all fixed by the time this runs.
    """

    def capability() -> ImageCapability:
        provider = provider_from_env()
        return image_capability(
            master=master_switch(),
            provider=provider.name,
            model_id=resolved_model_id(provider),
            media_ready=config.media is not None,
        )

    return capability


# --- device state ------------------------------------------------------------


def device_authorization(
    session,
    device_id: str,
    *,
    enabled_tools: frozenset[str],
    manifest_version: str,
) -> DeviceAuthorization | None:
    """Read one device's current authorisation, or None if it does not exist.

    The `devices` table stores `scopes` and `allowed_tools_version`; design 4.4's
    `device_allowed_tools` term is therefore the *version binding*: a device is
    granted the tools of the manifest it was enrolled against, and a device
    carrying any other version is granted none. Narrowing further per device
    would need a column that does not exist, and inventing one here would put a
    second, invisible source of truth beside the manifest.
    """
    device = session.get(Device, device_id)
    if device is None:
        return None
    try:
        scopes = decode_device_scopes(device.scopes)
    except ValueError as exc:
        raise CompositionError(
            f"device {device_id} has an unreadable scopes column"
        ) from exc
    granted = (
        enabled_tools
        if device.allowed_tools_version == manifest_version
        else frozenset()
    )
    return DeviceAuthorization(
        device_id=device.device_id,
        status=device.status,
        scopes=frozenset(scopes),
        allowed_tools=granted,
        allowed_tools_version=device.allowed_tools_version,
    )


class DeviceBoundDispatcher:
    """A `Dispatcher` that re-reads the device immediately before each call.

    `McpFinanceDispatcher` binds one `DeviceAuthorization` snapshot, which is
    right for a single dispatch and wrong for a whole operation: the model turn
    between building the dispatcher and committing the write is up to 25 seconds
    long, and a device revoked inside that window must not be able to commit.
    So the snapshot is taken per phase, and the dispatcher is built around it.

    A device that no longer exists is a safe failure with zero writes. A device
    that exists but is revoked is *not* special-cased here: it is handed to the
    bridge, which refuses it. Duplicating that judgement would create a second
    policy that can drift from the real one.
    """

    def __init__(
        self,
        *,
        device_id: str,
        sessions: Callable[[], Any],
        bridge: GovernedToolBridge,
        control: FinanceControlClient,
        signing_ring,
        user_id: str,
        agent_id: str,
        trace_id: str,
        enabled_tools: frozenset[str],
        manifest_version: str,
        client_wire_version: int,
        run: Callable[[Any], Any] = asyncio.run,
    ) -> None:
        self._device_id = device_id
        self._sessions = sessions
        self._bridge = bridge
        self._control = control
        self._ring = signing_ring
        self._user_id = user_id
        self._agent_id = agent_id
        self._trace_id = trace_id
        self._enabled_tools = enabled_tools
        self._manifest_version = manifest_version
        self._client_wire_version = client_wire_version
        self._run = run

    def _dispatcher(self) -> McpFinanceDispatcher | None:
        with self._sessions() as session:
            device = device_authorization(
                session,
                self._device_id,
                enabled_tools=self._enabled_tools,
                manifest_version=self._manifest_version,
            )
        if device is None:
            return None
        return McpFinanceDispatcher(
            bridge=self._bridge,
            control=self._control,
            signing_ring=self._ring,
            context=DispatcherContext(
                device=device,
                user_id=self._user_id,
                agent_id=self._agent_id,
                conversation_trace_id=self._trace_id,
                client_wire_version=self._client_wire_version,
                timezone=LEDGER_TIMEZONE,
            ),
            run=self._run,
        )

    def resolve(
        self,
        *,
        tool: str,
        model_args: dict[str, Any],
        idempotency_key: str | None = None,
        skip_local_dedup: bool = False,
    ) -> ResolveOutcome:
        dispatcher = self._dispatcher()
        if dispatcher is None:
            return ResolveFailedSafe(reason="policy_denied")
        return dispatcher.resolve(
            tool=tool,
            model_args=model_args,
            idempotency_key=idempotency_key,
            skip_local_dedup=skip_local_dedup,
        )

    def commit(
        self,
        *,
        intent: WriteIntent,
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> CommitOutcome:
        dispatcher = self._dispatcher()
        if dispatcher is None:
            # The device vanished between resolve and commit. Nothing has been
            # sent, so this is a safe failure and never an unknown commit.
            return CommitFailedSafe(reason="policy_denied")
        return dispatcher.commit(
            intent=intent,
            idempotency_key=idempotency_key,
            duplicate_override=duplicate_override,
        )


# --- the composition root ----------------------------------------------------


def _allowlist(config: AgentServiceConfig, enabled: frozenset[str]) -> frozenset[str]:
    if config.allowed_tools is None:
        return enabled
    unknown = config.allowed_tools - enabled
    if unknown:
        # A typo in the allowlist would otherwise silently disable a tool, which
        # looks exactly like a broken deployment and is diagnosed as a model bug.
        raise CompositionError(
            "the configured allowlist names tools that are not enabled "
            f"contracts: {sorted(unknown)}"
        )
    return frozenset(config.allowed_tools)


async def _discover(
    client: McpClientCore, registry: ConnectorRegistry, connector_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Connect once, read the whole catalog, and close.

    The connection is not kept: a governed call opens its own isolated
    connection so that one call's Host Context can never be seen by another.
    """
    try:
        await client.connect()
        try:
            discovered = await client.list_tools()
        finally:
            await client.close()
    except (McpTimeoutError, McpTransportError) as exc:
        raise CompositionError(
            f"the Finance MCP service could not be reached for discovery: {exc}"
        ) from exc
    catalog = registry.refresh(
        connector_id,
        trust_level=TrustLevel.PERSONAL_DATA,
        discovered=discovered,
    )
    quarantined = tuple(
        f"{entry.remote_name}:{entry.reason}" for entry in catalog.quarantined
    )
    if not catalog.entries:
        raise CompositionError(
            "the Finance connector advertised no manifest-verified tool; "
            "refusing to start against a server that cannot be the ledger "
            f"service (quarantined: {list(quarantined)})"
        )
    return tuple(sorted(catalog.entries)), quarantined


def _finance_status_reader(
    control: FinanceControlClient, run: Callable[[Any], Any]
) -> Callable[[str], FinanceExecutionStatus | None]:
    """Adapt the control-plane body to what recovery projects from.

    A body that cannot be understood raises. Recovery must never read a failed
    or malformed read as "Finance never saw this request", which is the one
    interpretation that would let a committed write be re-dispatched.
    """

    def read(idempotency_key: str) -> FinanceExecutionStatus | None:
        execution = run(control.get_execution(idempotency_key))
        if execution is None:
            return None
        state = execution.get("state")
        record_id = execution.get("record_id")
        verified = execution.get("receipt_verified")
        if not isinstance(state, str) or not state:
            raise ControlPlaneError("execution body carried no state")
        if record_id is not None and not isinstance(record_id, str):
            raise ControlPlaneError("execution body carried a non-string record id")
        if not isinstance(verified, bool):
            raise ControlPlaneError("execution body carried no receipt evidence")
        return FinanceExecutionStatus(
            state=state, record_id=record_id, receipt_verified=verified
        )

    return read


def build_review_control(*, finance_control_url: str) -> FinanceControlClient:
    """The control client the scheduled review job runs on.

    The job needs no model, no MCP connection and no data key -- only the
    Host-to-Host control channel -- so it is composed separately rather than by
    starting the whole service. The loopback rule and the signing ring are the
    same ones the API uses; they are not restated here.
    """
    url = require_loopback_url(finance_control_url)
    return FinanceControlClient(
        base_url=url, signing_ring=load_service_signing_ring()
    )


def record_reader(
    control: FinanceControlClient, run: Callable[[Any], Any] = asyncio.run
) -> Callable[
    [list[tuple[str, str]]],
    list[RecordFields | RecordUnavailable | None],
]:
    """Bridge the async control read into the synchronous review projection.

    Like every other control read from a worker thread, this drives its own
    event loop; `FinanceControlClient` opens one short-lived HTTP client per
    read precisely so that is safe.
    """

    def read(records: list[tuple[str, str]]):
        return run(control.get_record_fields_batch(records))

    return read


def recover_at_startup(
    sessions: Callable[[], Any],
    control: FinanceControlClient,
    *,
    now: Callable[[], datetime] = utc_now,
    run: Callable[[Any], Any] = asyncio.run,
    keyring=None,
) -> list[tuple[str, Any]]:
    """Run one projection of Finance truth onto every recoverable operation.

    The composition root calls this at boot and once per minute, as required by
    technical design 7.6.1.

    Since the 2026-08-03 fix a scan only adopts operations that have been quiet
    for `RECOVERY_QUIET_PERIOD`, which slightly delays the boot case: if the
    service crashed and came back within that window, a genuinely stranded
    operation waits for a later scan instead of the first one. That is the
    intended trade. Recovering a stranded operation a minute late costs a minute
    on a durable id the client is already polling; adopting one that a live
    worker still owns cost a successful ledger write being recorded as "finance
    has no execution".

    An unreadable control plane rolls the whole scan back
    rather than leaving half a projection behind: the operations stay recoverable
    and the next scheduled scan tries again. Refusing to run the service instead
    would wedge the Agent whenever Finance is down, and guessing would be worse
    than both.
    """
    if keyring is not None:
        from personal_agent.runtime.run_repository import RunRepository
        RunRepository(sessions,keyring).sweep(now_ms=round(now().timestamp()*1000))
    read = _finance_status_reader(control, run)
    with sessions() as session:
        try:
            results = recover_pending(session, read, now=now())
            session.commit()
        except ControlPlaneError as exc:
            session.rollback()
            logger.warning(
                "operation recovery could not read the Finance control plane (%s); "
                "recoverable operations are left untouched for the next scan",
                type(exc).__name__,
            )
            # The control plane being down says nothing about a device report
            # that never arrived, so the device sweep still runs: its timeout
            # judgement needs no external read at all.
            return _sweep_timed_out_device_actions_logged(sessions, now)
        except Exception:
            session.rollback()
            raise
    results.extend(_sweep_timed_out_device_actions_logged(sessions, now))
    for operation_id, outcome in results:
        # A Finance projection logs its plan; a device sweep logs the state it
        # parked the operation at. Both are "why this row moved".
        outcome_description = (
            outcome if isinstance(outcome, str) else outcome.action
        )
        logger.info(
            "operation recovery: %s -> %s", operation_id, outcome_description
        )
    return results


def _sweep_timed_out_device_actions_logged(
    sessions: Callable[[], Any], now: Callable[[], datetime]
) -> list[tuple[str, Any]]:
    """Run the device-report timeout sweep in its own session.

    Separate from the Finance projection's session so a Finance-side failure
    can never hold a transaction open across the sweep, and the sweep's CAS
    moves survive independently. Only device-executed tools are touched (the
    filter is derived from the IR); every Finance operation at
    `source_in_progress` stays the reconciler's alone.
    """
    try:
        with sessions() as session:
            settled = sweep_timed_out_device_actions(session, now=now())
            session.commit()
    except Exception:
        logger.exception("device-action timeout sweep failed; retrying next scan")
        return []
    for operation_id, target in settled:
        logger.info(
            "device action report timed out: %s -> %s", operation_id, target
        )
    return settled


async def _recover_periodically(
    sessions: Callable[[], Any],
    control: FinanceControlClient,
    *,
    now: Callable[[], datetime],
    interval_seconds: float,
    stop: asyncio.Event,
    keyring=None,
) -> None:
    """Re-run the recovery projection for the lifetime of the service."""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            # The scan is synchronous SQLite work and its control reads drive
            # their own event loop, so it must not block the API event loop.
            await asyncio.to_thread(recover_at_startup, sessions, control, now=now, keyring=keyring)
        except Exception:
            # A single broken scan must not permanently remove recovery from a
            # long-running service. The next minute retries from durable state.
            logger.exception(
                "operation recovery scan failed unexpectedly; retrying next interval"
            )


@contextmanager
def _engine_for(database: Path) -> Iterator[Any]:
    engine = create_database_engine(database)
    try:
        if database.exists():
            check_integrity(engine)
        yield engine
    finally:
        engine.dispose()


@asynccontextmanager
async def agent_service(
    config: AgentServiceConfig,
    *,
    write_switch: WriteSwitch,
    now: Callable[[], datetime] = utc_now,
    #: `None` builds the production GLM gateway bound to this composition's
    #: transcript recorder. Tests inject their own zero-argument builder, whose
    #: gateway records nothing -- the fake is the thing under test, not the
    #: provider boundary the transcript exists to explain.
    build_gateway: Callable[[], Any] | None = None,
    build_structured_client: Callable[..., Any] = structured_client_from_env,
    context_config: ContextConfig | None = None,
    recovery_interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
) -> AsyncIterator[ComposedAgentService]:
    """Compose the Agent API for the lifetime of the service."""
    if recovery_interval_seconds <= 0:
        raise ValueError("recovery_interval_seconds must be positive")
    # Both internal channels are checked here, before a key is read or a socket
    # is opened, so a misconfigured host can never receive a signed token.
    try:
        mcp_url = require_loopback_url(config.finance_mcp_url)
        require_loopback_url(config.finance_control_url)
        ledger_url = _checked_ledger_url(config.ledger_url)
    except ValueError as exc:
        raise CompositionError(str(exc)) from exc

    manifest = load_manifest()
    manifest_version = manifest["allowed_tools_version"]
    # "The service may execute it" and "the model may be offered it" are two
    # different permissions. The receipt-card category update is enabled for a
    # deterministic device action and explicitly absent from model context.
    # Using the model subset for bridge/device policy would make that route
    # fail in production even though fake-authorizer API tests stayed green.
    enabled_tools = frozenset(manifest["enabled_tools"])
    model_callable_tools = frozenset(manifest["model_callable_tools"])
    allowlist = _allowlist(config, enabled_tools)

    # Keys and the model gateway come first: a service that cannot sign, cannot
    # seal or cannot reach the model must fail before it opens a connection.
    keyring = load_agent_data_keyring()
    token_ring = load_access_token_ring()
    service_ring = load_service_signing_ring()
    # `CAP-001`. Loaded here for the same reason as the rings above: a service
    # that cannot sign a page cursor would otherwise discover it on the first
    # scroll, and one whose identifier key is missing could not tell a legacy
    # conversation id from an unknown one.
    cursor_key = load_cursor_key()
    identifier_key = load_identifier_key()
    # An injected `context_config` is authoritative (tests, callers that have
    # already applied overrides). The composed default applies the operator's
    # environment overrides on top of the code baseline; an invalid value
    # raises here so a bad tuning fails at startup, never mid-turn.
    context_config = context_config or default_context_config(
        operator_override_from_env()
    )
    # The budget is checked against what the adapter says it can accept, so a
    # ceiling larger than the model's window fails at startup, not mid-turn.
    context_config.require_within_model_limit(declared_context_limit())
    # The transcript sink, before the gateway that writes to it. A malformed
    # transcript configuration is a deployment error and fails here; once
    # running, recording never fails a turn.
    try:
        recorder = recorder_from_env(service="api")
    except (OSError, ValueError) as exc:
        raise CompositionError(f"the transcript sink could not be built: {exc}") from exc
    if isinstance(recorder, TranscriptRecorder):
        # Names the directory, never a record. An operator has to be able to see
        # that full-fidelity capture is on without reading the files.
        logger.info("turn transcript enabled dir=%s", recorder.directory)
    build = build_gateway or (lambda: glm_gateway_from_env(recorder=recorder))
    try:
        gateway = build()
        # `CAP-001`. The auxiliary structured model path: the same pinned
        # endpoint as Chat, one declared function per call. Built here beside
        # the gateway so a deployment that cannot reach the model fails at
        # startup rather than on the first boundary decision.
        # Two clients, not one. The deadline guard keeps a single in-flight
        # call per client, so sharing one would let a background compaction
        # disable classification for every message that arrived while it ran.
        # They also run on different models and worker deadlines.  Classification
        # starts after the response has been anchored; compaction starts only
        # after its boundary assignment has settled.
        compactor_client = build_structured_client(
            input_budget_tokens=context_config.hard_limit_tokens,
            recorder=recorder,
            purpose="compactor",
        )
        classifier_client = build_structured_client(
            input_budget_tokens=context_config.hard_limit_tokens,
            timeout=CLASSIFIER_TIMEOUT_SECONDS,
            model_env=CLASSIFIER_MODEL_ENV,
            recorder=recorder,
            purpose="session_classifier",
        )
    except (ModelGatewayError, StructuredCallError) as exc:
        # A missing model credential or a tampered endpoint is a deployment
        # failure, not something to discover on the first message.
        raise CompositionError(f"the model gateway could not be built: {exc}") from exc
    compactor = Compactor(
        context_config, provider=GlmCompactorProvider(compactor_client)
    )
    # The one assembly point for model context.
    context_builder = ContextBuilder(context_config, compactor=compactor)
    from personal_agent.runtime.input_budget import input_budget_from_env
    from personal_agent.context.budget import ContextBudgeter
    from dataclasses import replace
    v2_input_budget = input_budget_from_env()
    v2_context_builder = context_builder
    if v2_input_budget:
        v2_context_config = replace(context_config, name='adk-200k-v1',
            hard_limit_tokens=v2_input_budget.limit,
            soft_limit_tokens=min(160000, v2_input_budget.limit-1),
            product_ceiling_tokens=v2_input_budget.limit+v2_input_budget.output_limit+4096,
            reserved_output_tokens=v2_input_budget.output_limit)
        v2_context_config.require_within_model_limit(declared_context_limit())
        v2_context_builder = ContextBuilder(v2_context_config, compactor=compactor,
            budgeter=ContextBudgeter(v2_context_config, estimator=v2_input_budget))

    with _engine_for(config.database) as engine:
        sessions = session_factory(engine)
        registry = ConnectorRegistry()
        client = McpClientCore(
            config.connector_id,
            StreamableHttpTransport(url=mcp_url),
            allowed_protocol_versions=FINANCE_PROTOCOL_VERSIONS,
        )
        aliases, quarantined = await _discover(client, registry, config.connector_id)
        bridge = GovernedToolBridge(
            registry,
            global_allowlist=allowlist,
            write_switch=write_switch,
            clients={config.connector_id: client},
        )
        control = FinanceControlClient(
            base_url=config.finance_control_url, signing_ring=service_ring
        )
        recovery_task: asyncio.Task[None] | None = None
        recovery_stop = asyncio.Event()
        try:
            # In a worker thread, because the scan is synchronous SQLite work
            # and its control reads drive their own event loop.
            await asyncio.to_thread(recover_at_startup, sessions, control, now=now, keyring=keyring)
            recovery_task = asyncio.create_task(
                _recover_periodically(
                    sessions,
                    control,
                    now=now,
                    interval_seconds=recovery_interval_seconds,
                    stop=recovery_stop,
                    keyring=keyring,
                ),
                name="operation-recovery",
            )

            def device_for(auth: AuthContext) -> DeviceAuthorization | None:
                with sessions() as session:
                    return device_authorization(
                        session,
                        auth.device_id,
                        enabled_tools=enabled_tools,
                        manifest_version=manifest_version,
                    )

            def build_interpreter(auth: AuthContext) -> ModelInterpreter:
                return ModelInterpreter(gateway)

            def build_envelope(
                session,
                auth: AuthContext,
                *,
                conversation_id: str,
                session_id: str,
                current_event_id: str,
                user_text: str,
                clarification_context: ClarificationContext | None,
                finance_retry_context: FinanceRetryContext | None,
                input_parts: tuple[InputPart, ...] = (),
            ) -> ContextEnvelope:
                """Assemble this turn's context (`CAP-001` design §9).

                The effective tool set is re-read here, at assembly time, from
                the same governed bridge the authorizer uses. A device revoked
                between anchoring and the model turn therefore sees an envelope
                with no declarations rather than the catalog it had a moment
                earlier -- and the write would still be refused downstream.

                `input_parts` are already-authorized bytes (§6) handed down
                from the caller that read them under the media lock. They
                travel through this seam rather than being read here for the
                same reason the tool set is: this function owns assembly, not
                authorization, and a media read performed inside it would run
                outside the lock discipline `media_read` exists to keep.
                """
                device = device_for(auth)
                tools = (
                    []
                    if device is None
                    else [
                        tool
                        for tool in bridge.visible_tools(device)
                        if tool.alias in model_callable_tools
                    ]
                )
                from personal_agent.api.runtime_v2 import row as runtime_row
                from personal_agent.storage.models import ConversationEvent
                event = session.get(ConversationEvent, current_event_id)
                is_v2 = event is not None and runtime_row(session, event.operation_id) is not None
                return (v2_context_builder if is_v2 else context_builder).build(
                    session,
                    keyring,
                    identifier_key,
                    conversation_id=conversation_id,
                    session_id=session_id,
                    current_event_id=current_event_id,
                    system_instruction=build_system_prompt(
                        today=format_ledger_date(ledger_date(now())), runtime_v2=is_v2
                    ),
                    user_text=user_text,
                    effective_tools=tools,
                    essential_tools=tuple(t.alias for t in tools) if is_v2 else (),
                    clarification_context=clarification_context,
                    finance_retry_context=finance_retry_context,
                    input_parts=input_parts,
                )

            def compact_session(session, session_id: str) -> None:
                """Compact one Session, after its turn has already answered."""
                result = compactor.build_checkpoint(
                    session,
                    keyring,
                    identifier_key,
                    session_id=session_id,
                    now=now(),
                )
                # Enumerations only: a build outcome names no content.
                logger.info(
                    "compaction after a turn: %s", result.status
                )

            def build_authorizer(auth: AuthContext):
                def authorize(*, tool: str, model_args: dict[str, Any]):
                    device = device_for(auth)
                    if device is None:
                        raise _no_such_device(auth.device_id)
                    _, cleaned = bridge.authorize(tool, model_args, device)
                    return cleaned

                return authorize

            def build_dispatcher(auth: AuthContext, trace_id: str) -> Dispatcher:
                dispatcher = DeviceBoundDispatcher(
                    device_id=auth.device_id,
                    sessions=sessions,
                    bridge=bridge,
                    control=control,
                    signing_ring=service_ring,
                    user_id=config.user_id,
                    agent_id=config.agent_id,
                    trace_id=trace_id,
                    enabled_tools=enabled_tools,
                    manifest_version=manifest_version,
                    client_wire_version=auth.client_wire_version,
                )
                # Always wrapped, on every composition. A recorder that is
                # disabled records nothing; a conditional wrap would be one more
                # path that only production exercises.
                return RecordingDispatcher(dispatcher, recorder)

            def search_allowed(auth: AuthContext, tool: str) -> bool:
                device = device_for(auth)
                search = config.search_config
                return bool(search and search.enabled and config.v2_execution_enabled
                    and auth.client_wire_version >= 4 and auth.device_id in config.v2_device_ids
                    and device is not None and device.status == "active"
                    and device.allowed_tools_version == manifest_version
                    and "public_web.read" in device.scopes
                    and tool in {"search.web", "search.read_page"}
                    and tool in allowlist and tool in device.allowed_tools
                    and (tool != "search.read_page" or search.extract_enabled))

            def capabilities(auth: AuthContext) -> list[dict[str, Any]]:
                device = device_for(auth)
                if device is None:
                    return []
                result = [
                    {
                        "alias": tool.alias,
                        "description": tool.description,
                        "risk_level": tool.risk_level,
                        "required_scopes": list(tool.required_scopes),
                    }
                    for tool in bridge.visible_tools(device)
                    if tool.alias in model_callable_tools
                ]

                from personal_agent_core.tool_ir import SEARCH_WEB,SEARCH_READ_PAGE
                for tool in (SEARCH_WEB,SEARCH_READ_PAGE):
                    if search_allowed(auth, tool.name):
                        result.append({'alias':tool.name,'description':tool.summary,'risk_level':tool.risk_level,'required_scopes':list(tool.required_scopes)})
                return result

            def sync_ingest(auth: AuthContext, body: dict[str, Any]) -> dict[str, Any]:
                """Mirror one calendar snapshot batch into the MCP database.

                The device-side data channel (`calendar.ingest_events`): the
                route has already authenticated the caller and checked the
                read scope, but the *execution* still crosses the same
                governed bridge as every other tool call, under a Host Context
                naming this caller as the device. The bridge's `execute`
                authorises again -- the stale-manifest and scope gates are
                proven there -- and the MCP side stamps each mirror row with
                the signed device identity.

                Runs synchronously on a worker thread: it must not hold any
                API-side transaction across the MCP call (`CLAUDE.md` §5.2).
                """
                device = device_for(auth)
                if device is None:
                    raise _no_such_device(auth.device_id)
                host = HostContext(
                    agent_id=config.agent_id,
                    device_id=device.device_id,
                    user_id=config.user_id,
                    scopes=tuple(sorted(device.scopes)),
                    tool=_CALENDAR_INGEST_TOOL,
                    request_id=str(uuid.uuid4()),
                    trace_id=new_traceparent(),
                    idempotency_key=f"calendar-sync-{uuid.uuid4()}",
                    request_fingerprint=tool_call_fingerprint(
                        _CALENDAR_INGEST_TOOL, body
                    ),
                    allowed_tools_version=device.allowed_tools_version,
                    timezone="Asia/Shanghai",
                    # The barrier's protocol version is the header the device
                    # already sends on every request, read once at the edge and
                    # signed here. It is never a payload field: the ingest gate
                    # decides from `client_wire_version` whether this call's own
                    # arguments may be written, so a device able to state it in
                    # the payload would be grading its own paper.
                    client_wire_version=auth.client_wire_version,
                )
                execution = asyncio.run(
                    bridge.execute(
                        _CALENDAR_INGEST_TOOL,
                        body,
                        device,
                        call_context=BridgeCallContext(
                            host=host, signing_keys=service_ring
                        ),
                    )
                )
                return execution.trusted_result

            yield ComposedAgentService(
                deps=AgentApiDeps(
                    session_factory=sessions,
                    v2_input_budget=v2_input_budget,
                    v2_device_ids=config.v2_device_ids,
                    v2_execution_enabled=config.v2_execution_enabled,
                    v2_search_adapter=_search_adapter(config.search_config),
                    v2_search_allowed=search_allowed,
                    token_ring=token_ring,
                    keyring=keyring,
                    identifier_key=identifier_key,
                    cursor_key=cursor_key,
                    context_config=context_config,
                    # `CAP-001` design 6.1 step 7. The classifier only ever runs
                    # when a verified Checkpoint can supply the compact state,
                    # and every uncertain answer still continues the Session.
                    session_manager=SessionManager(
                        context_config,
                        classifier=GlmBoundaryClassifier(classifier_client),
                        state_provider=CheckpointCompactStateProvider(
                            compactor, keyring
                        ),
                    ),
                    build_interpreter=build_interpreter,
                    build_envelope=build_envelope,
                    compact_session=compact_session,
                    build_dispatcher=build_dispatcher,
                    build_authorizer=build_authorizer,
                    capabilities=capabilities,
                    sync_ingest=sync_ingest,
                    # The same data keyring seals an issued device action onto
                    # its operation (review R6); the explicit field keeps the
                    # seal a visible seam instead of an implicit right of every
                    # `keyring` call site.
                    action_keyring=keyring,
                    now=now,
                    read_record=record_reader(control),
                    # A device is enrolled against the manifest this service is
                    # actually running, so the version comes from the loaded
                    # manifest and never from the enrolling client.
                    enrollment_manifest_version=manifest_version,
                    sync_wait_seconds=config.sync_wait_seconds,
                    ledger_url=ledger_url,
                    recorder=recorder,
                    # `#18`. Both or neither, which is why they are read off
                    # one `config.media`: a store with no limits has no bound
                    # to enforce, and limits with no store cannot store.
                    media_store=(
                        config.media.store(keyring)
                        if config.media is not None
                        else None
                    ),
                    media_limits=(
                        config.media.limits() if config.media is not None else None
                    ),
                    # `#13`. §8's switch. Recomputing per call rather than
                    # freezing a verdict is what makes "服务端再次校验" a
                    # property of the code instead of a promise about a value.
                    image_capability=image_capability_from(config),
                ),
                bridge=bridge,
                catalog_aliases=aliases,
                quarantined=quarantined,
            )
        finally:
            if recovery_task is not None:
                # Do not cancel `asyncio.to_thread`: cancellation cannot stop its
                # worker thread and closing `control` underneath that thread
                # would race an in-flight signed control read. Wake a sleeping
                # task immediately, or let its bounded current scan finish.
                recovery_stop.set()
                await recovery_task
            await control.aclose()
            await client.close()


def _checked_ledger_url(raw: str | None) -> str | None:
    """Validate the configured ledger URL, or pass `None` through.

    `DEV-031`. The URL is opened on the phone, so only `https` with a real host
    is acceptable: anything else is a configuration error to refuse at startup,
    not a string to hand every enrolled device.
    """
    if raw is None:
        return None
    parts = urlsplit(raw.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(
            "the ledger URL must be an https URL with a host; the client opens "
            "whatever the service names, so a malformed value cannot be served"
        )
    return raw.strip()


def _no_such_device(device_id: str) -> AppError:
    return AppError(
        ErrorCode.TOOL_NOT_ALLOWLISTED,
        internal_detail=f"no device row for {device_id}",
    )


def _search_adapter(config):
    from personal_agent.search.adapter import SearchAdapter,SearchConfig
    import os
    return SearchAdapter(config or SearchConfig(),key=os.environ.get('ANYSEARCH_API_KEY'))
