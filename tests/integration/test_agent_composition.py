"""DEV-027: the Agent-side service composition root, against a real service.

Everything here runs the production wiring: `agent_service` loads real key
material, discovers a **real** Finance MCP process over a loopback socket,
builds the governed bridge from the trusted manifest, signs a real Host Context
that the server's own authorisation gate verifies, and serves the real FastAPI
app. The only fake is the model gateway, because a model is not a counterparty a
test may hold still.

The cases were chosen from the failure modes, not the happy path:

- a URL that would send an internal token off this host;
- a Finance service that is absent, or is not the ledger service at all;
- an allowlist typo, which otherwise looks exactly like a broken model;
- a device revoked, unbound or deleted *during* the model turn -- the window a
  cached authorisation snapshot would silently keep open;
- a control-plane body that cannot be understood, which must never be read as
  "Finance never saw this request".
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.exc import IntegrityError

from fixtures.loopback_service import LoopbackFinanceService, free_port
from personal_agent.api.app import build_app
from personal_agent.api.composition import (
    AgentServiceConfig,
    CompositionError,
    DeviceBoundDispatcher,
    agent_service,
    recover_at_startup,
)
from personal_agent.api.control_client import ControlPlaneError
from personal_agent.diagnostics.transcript import (
    DIRECTORY_ENV as TRANSCRIPT_DIRECTORY_ENV,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import CommitFailedSafe, ResolveFailedSafe
from personal_agent.api.operation_store import open_operation, transition_operation
from personal_agent.api.recovery import RECOVERY_QUIET_PERIOD
from personal_agent.auth.tokens import issue_access_token
from personal_agent.context.budget import ComponentKind
from personal_agent.context.config import (
    CAP001_PROVISIONAL_VALUES,
    ContextConfig,
)
from personal_agent.keys import (
    DATA_ACTIVE_KEY_ENV,
    CURSOR_ACTIVE_KEY_ENV,
    CURSOR_ACTIVE_KID_ENV,
    DATA_ACTIVE_KID_ENV,
    IDENTIFIER_ACTIVE_KEY_ENV,
    IDENTIFIER_ACTIVE_KID_ENV,
    SERVICE_ACTIVE_KEY_ENV,
    SERVICE_ACTIVE_KID_ENV,
    TOKEN_ACTIVE_KEY_ENV,
    TOKEN_ACTIVE_KID_ENV,
    load_access_token_ring,
)
from personal_agent.runtime.model_gateway import (
    ModelGatewayError,
    ProposedAnswer,
    ProposedToolCall,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.runtime.structured import StructuredModelClient
from personal_agent.storage.models import (
    ContextCheckpoint,
    ContextSession,
    Conversation,
    ConversationEvent,
    Device,
    Operation,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import load_manifest
from personal_agent_core.timeutil import utc_now
from personal_data_mcp.server.keys import (
    ACTIVE_KID_ENV as MCP_KID_ENV,
    ACTIVE_PEM_ENV as MCP_PEM_ENV,
)
from personal_data_mcp.storage.engine import (
    create_database_engine as create_finance_engine,
    session_factory as finance_session_factory,
)
from personal_data_mcp.storage.models import AuditEvent, ToolExecution
from write_switch_fixtures import shared_enabled_write_switch


MANIFEST_VERSION = load_manifest()["allowed_tools_version"]
DEVICE_ID = "dev-1"
THUMBPRINT = "THUMB"
USER_ID = "test-user"
CAPABILITY_SCOPE = "meta.capabilities.read"
EXPENSE_SCOPE = "finance.expense.write"


# --- environment -------------------------------------------------------------


@dataclass(frozen=True)
class AgentKeyFiles:
    env: dict[str, str]
    service_public_pem: Path


def write_keys(directory: Path) -> AgentKeyFiles:
    directory.mkdir(parents=True, exist_ok=True)
    data_key = directory / "data.key"
    data_key.write_text(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"))
    # `CAP-001` design 5.6: three distinct symmetric secrets, because the
    # loaders compare material and refuse two purposes sharing one key.
    cursor_key = directory / "cursor.key"
    cursor_key.write_text(base64.urlsafe_b64encode(b"c" * 32).decode("ascii"))
    identifier_key = directory / "identifier.key"
    identifier_key.write_text(base64.urlsafe_b64encode(b"i" * 32).decode("ascii"))

    def private(path: Path) -> ec.EllipticCurvePrivateKey:
        key = ec.generate_private_key(ec.SECP256R1())
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return key

    token_pem = directory / "token.pem"
    service_pem = directory / "service.pem"
    private(token_pem)
    service_key = private(service_pem)

    public_pem = directory / "service.pub.pem"
    public_pem.write_bytes(
        service_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return AgentKeyFiles(
        env={
            DATA_ACTIVE_KID_ENV: "agent-data-test",
            DATA_ACTIVE_KEY_ENV: str(data_key),
            CURSOR_ACTIVE_KID_ENV: "agent-cursor-test",
            CURSOR_ACTIVE_KEY_ENV: str(cursor_key),
            IDENTIFIER_ACTIVE_KID_ENV: "agent-identifier-test",
            IDENTIFIER_ACTIVE_KEY_ENV: str(identifier_key),
            TOKEN_ACTIVE_KID_ENV: "tok-test",
            TOKEN_ACTIVE_KEY_ENV: str(token_pem),
            SERVICE_ACTIVE_KID_ENV: "svc-test",
            SERVICE_ACTIVE_KEY_ENV: str(service_pem),
        },
        service_public_pem=public_pem,
    )


@pytest.fixture()
def keys(tmp_path: Path, monkeypatch) -> AgentKeyFiles:
    written = write_keys(tmp_path)
    for name, value in written.env.items():
        monkeypatch.setenv(name, value)
    # The real GLM gateway is built at composition; constructing it makes no
    # network call, so the production path is exercised with a placeholder
    # credential and every test that needs a proposal injects its own gateway.
    monkeypatch.setenv("ZAI_API_KEY", "placeholder-not-a-real-key")
    monkeypatch.delenv("GLM_OPENAI_BASE_URL", raising=False)
    return written


@pytest.fixture()
def agent_db(tmp_path: Path) -> Path:
    path = tmp_path / "agent.sqlite"
    engine = create_database_engine(path)
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id=DEVICE_ID,
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint=THUMBPRINT,
                status="active",
                scopes=json.dumps([CAPABILITY_SCOPE]),
                allowed_tools_version=MANIFEST_VERSION,
                created_at=utc_now(),
            )
        )
        # `CAP-001`: the deployment's one canonical Timeline. A real client
        # reads this id from `/v1/capabilities`; seeding it as `c1` keeps these
        # tests focused on composition while still going through resolution.
        session.add(
            Conversation(
                conversation_id="c1",
                created_at=utc_now(),
                next_sequence=1,
                is_canonical=True,
            )
        )
        session.commit()
    engine.dispose()
    return path


@pytest.fixture()
def finance(keys: AgentKeyFiles):
    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        }
    )
    yield service
    service.stop()


class FakeGateway:
    """A model that proposes exactly what the test needs, once.

    `before` runs between the request arriving and the proposal being returned,
    which is the window a real 25-second model turn opens.
    """

    def __init__(self, proposal, *, before=None, fail: bool = False) -> None:
        self._proposal = proposal
        self._before = before
        self._fail = fail
        self.calls: list[dict] = []

    def propose(self, **kwargs):
        self.calls.append(kwargs)
        if self._before is not None:
            self._before()
        if self._fail:
            raise ModelGatewayError("model unavailable")
        return self._proposal


def config_for(
    database: Path, service: LoopbackFinanceService, **overrides
) -> AgentServiceConfig:
    fields = {
        "database": database,
        "finance_mcp_url": service.mcp_url,
        "finance_control_url": service.control_url,
        "user_id": USER_ID,
    }
    fields.update(overrides)
    return AgentServiceConfig(**fields)


def access_token(scopes=(CAPABILITY_SCOPE,), version: str = MANIFEST_VERSION) -> str:
    return issue_access_token(
        load_access_token_ring(),
        device_id=DEVICE_ID,
        device_key_thumbprint=THUMBPRINT,
        scopes=list(scopes),
        allowed_tools_version=version,
        now=utc_now(),
    )


def http_for(deps) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(deps)),
        base_url="http://agent.local",
    )


def chat_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token()}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def update_device(database: Path, **changes) -> None:
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        device = session.get(Device, DEVICE_ID)
        for name, value in changes.items():
            setattr(device, name, value)
        session.commit()
    engine.dispose()


def delete_device(database: Path) -> None:
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        session.delete(session.get(Device, DEVICE_ID))
        session.commit()
    engine.dispose()


# --- composition refusals ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8811/mcp",
        "https://finance.example.com/mcp",
        "http://finance.internal:8811/mcp",
    ],
)
def test_a_non_loopback_finance_url_is_refused(keys, agent_db, url) -> None:
    """A Host Context must never travel to another host."""

    async def scenario():
        async with agent_service(
            AgentServiceConfig(
                database=agent_db,
                finance_mcp_url=url,
                finance_control_url="http://127.0.0.1:8811",
                user_id=USER_ID,
            ),
            write_switch=shared_enabled_write_switch(),
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="loopback"):
        asyncio.run(scenario())


def test_a_non_loopback_control_url_is_refused(keys, agent_db) -> None:
    async def scenario():
        async with agent_service(
            AgentServiceConfig(
                database=agent_db,
                finance_mcp_url="http://127.0.0.1:8811/mcp",
                finance_control_url="http://control.example.com",
                user_id=USER_ID,
            ),
            write_switch=shared_enabled_write_switch(),
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="loopback"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.feishu.cn/base/APP_TOKEN",
        "not-a-url",
        "https:///base/APP_TOKEN",
    ],
)
def test_a_malformed_ledger_url_is_refused(keys, agent_db, url) -> None:
    """`DEV-031`: the client opens whatever the service names, so a non-https
    or hostless value is refused before a socket exists."""

    async def scenario():
        async with agent_service(
            AgentServiceConfig(
                database=agent_db,
                finance_mcp_url="http://127.0.0.1:8811/mcp",
                finance_control_url="http://127.0.0.1:8811",
                user_id=USER_ID,
                ledger_url=url,
            ),
            write_switch=shared_enabled_write_switch(),
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="ledger URL"):
        asyncio.run(scenario())


def test_an_absent_finance_service_fails_composition(keys, agent_db) -> None:
    """No half-started service: discovery failure is a startup failure."""
    port = free_port()

    async def scenario():
        async with agent_service(
            AgentServiceConfig(
                database=agent_db,
                finance_mcp_url=f"http://127.0.0.1:{port}/mcp",
                finance_control_url=f"http://127.0.0.1:{port}",
                user_id=USER_ID,
            ),
            write_switch=shared_enabled_write_switch(),
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="Finance"):
        asyncio.run(scenario())


def test_a_missing_model_credential_fails_composition(
    keys, agent_db, finance, monkeypatch
) -> None:
    monkeypatch.delenv("ZAI_API_KEY")

    async def scenario():
        async with agent_service(config_for(agent_db, finance), write_switch=shared_enabled_write_switch()):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="model gateway"):
        asyncio.run(scenario())


def test_a_tampered_model_endpoint_fails_composition(
    keys, agent_db, finance, monkeypatch
) -> None:
    """A credential may only travel to the pinned provider endpoint."""
    monkeypatch.setenv("GLM_OPENAI_BASE_URL", "https://open.bigmodel.cn.evil.test/api/paas/v4/")

    async def scenario():
        async with agent_service(config_for(agent_db, finance), write_switch=shared_enabled_write_switch()):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="model gateway"):
        asyncio.run(scenario())


def test_an_allowlist_naming_an_unknown_tool_is_refused(
    keys, agent_db, finance
) -> None:
    async def scenario():
        async with agent_service(
            config_for(
                agent_db,
                finance,
                allowed_tools=frozenset({"finance.log_expence"}),
            ),
            write_switch=shared_enabled_write_switch(),
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="not enabled contracts"):
        asyncio.run(scenario())


def test_a_disabled_contract_may_not_be_allowlisted(keys, agent_db, finance) -> None:
    """`finance.log_expense_batch` ships disabled; naming it is still a refusal."""

    async def scenario():
        async with agent_service(
            config_for(
                agent_db,
                finance,
                allowed_tools=frozenset({"finance.log_expense_batch"}),
            ),
            write_switch=shared_enabled_write_switch(),
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError):
        asyncio.run(scenario())


# --- discovery ---------------------------------------------------------------


def test_composition_discovers_the_real_credential_free_catalog(
    keys, agent_db, finance
) -> None:
    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            return composed.catalog_aliases, composed.quarantined

    aliases, quarantined = asyncio.run(scenario())
    # Without a ledger config the service composes no write tool, so the
    # catalog is exactly what that server can actually run.
    assert aliases == ("meta.capabilities",)
    assert quarantined == ()


# --- the governed path, end to end -------------------------------------------


def test_a_read_tool_call_runs_through_the_real_governed_path(
    keys, agent_db, finance
) -> None:
    """One message, one real MCP call, verified by the server's own gate."""
    gateway = FakeGateway(ProposedToolCall(tool="meta.capabilities", arguments={}))

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance), build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "我有哪些能力？"},
                    headers=chat_headers(),
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    assert body["record_id"] is None  # a read is never projected as evidence
    answer = json.loads(body["answer"])
    assert answer["status"] == "ok"
    assert [tool["name"] for tool in answer["tools"]] == ["meta.capabilities"]
    # The model saw the trusted manifest's catalog, not the server's own text.
    # The catalog reached the model through the assembled envelope, which is the
    # only path context may take since `CAP-001`.
    assert gateway.calls[0]["envelope"].tool_aliases == ("meta.capabilities",)


def test_a_configured_transcript_captures_the_whole_turn(
    keys, agent_db, finance, tmp_path, monkeypatch
) -> None:
    """The transcript is composed for real, not wired only in its own tests.

    A seam that exists only where a unit test constructs it is not wiring
    (AGENTS.md §7). This runs the production composition root with the switch
    set, and asserts that one message leaves a reassemblable turn on disk: the
    input the device sent, the tool call as dispatched, its outcome before any
    projection, the orchestrator's result, and the body the device got back.
    """
    directory = tmp_path / "transcripts"
    monkeypatch.setenv(TRANSCRIPT_DIRECTORY_ENV, str(directory))
    gateway = FakeGateway(ProposedToolCall(tool="meta.capabilities", arguments={}))

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance), build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "我有哪些能力？"},
                    headers=chat_headers(),
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200, response.text

    records = [
        json.loads(line)
        for path in sorted(directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    by_kind = {record["kind"]: record for record in records}
    assert set(by_kind) == {
        "user_message",
        "tool_call",
        "tool_result",
        "turn_result",
        "api_response",
    }
    assert by_kind["user_message"]["payload"]["text"] == "我有哪些能力？"
    assert by_kind["tool_call"]["payload"]["tool"] == "meta.capabilities"
    assert by_kind["turn_result"]["payload"]["state"] == "succeeded"
    assert by_kind["api_response"]["payload"]["status_code"] == 200
    assert by_kind["api_response"]["payload"]["body"]["state"] == "succeeded"
    # One operation id joins every line of the turn. Without it the file is a
    # pile of fragments rather than a transcript.
    operation_ids = {record["turn"]["operation_id"] for record in records}
    assert len(operation_ids) == 1
    assert operation_ids != {None}


def test_no_transcript_directory_writes_nothing(
    keys, agent_db, finance, tmp_path, monkeypatch
) -> None:
    """The default deployment records nothing at all."""
    monkeypatch.delenv(TRANSCRIPT_DIRECTORY_ENV, raising=False)
    directory = tmp_path / "transcripts"

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="你好")),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "在吗"},
                    headers=chat_headers(),
                )

    assert asyncio.run(scenario()).json()["state"] == "succeeded"
    assert not directory.exists()


def test_a_second_message_reaches_the_model_with_the_first_turn_in_context(
    keys, agent_db, finance
) -> None:
    """`CAP-001` wiring: the composed service assembles a real turn context.

    This is the property that separates "the Context Builder exists" from "the
    running service uses it". Both messages go through the real composition
    root, and the second one has to carry the first message *and* its recorded
    outcome as untrusted history -- while the system instruction stays free of
    both.
    """
    gateway = FakeGateway(ProposedAnswer("好的"))

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance), build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                first = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "我在整理这个月的支出"},
                    headers=chat_headers(),
                )
                second = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "刚才说到哪了？"},
                    headers=chat_headers(),
                )
                return first, second

    first, second = asyncio.run(scenario())
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    opening, follow_up = gateway.calls[0]["envelope"], gateway.calls[1]["envelope"]
    # Turn one had no history at all.
    assert opening.texts_of(ComponentKind.RAW_EVENT) == ()
    assert opening.user_text == "我在整理这个月的支出"

    history = "\n".join(follow_up.texts_of(ComponentKind.RAW_EVENT))
    assert "我在整理这个月的支出" in history
    assert "好的" in history  # the recorded operation result, not model memory
    # The current message appears once, as the current message.
    assert follow_up.user_text == "刚才说到哪了？"
    assert "刚才说到哪了？" not in history
    # History is data. It never becomes instruction.
    assert "我在整理这个月的支出" not in follow_up.system_instruction
    assert history.count("<untrusted_data") == 2
    # One Timeline, one Session, and the envelope says which.
    assert follow_up.timeline_id == opening.timeline_id
    assert follow_up.session_id == opening.session_id


def test_crossing_the_soft_limit_compacts_after_the_turn_not_before(
    keys, agent_db, finance
) -> None:
    """`CAP-001` §7.3: the soft limit triggers the Compactor, nothing else.

    The composed service is given a small soft limit and a structured client
    that returns valid checkpoints. Both the synchronous 200 path and a detached
    202 path must answer before their Compactor runs. The second turn also proves
    the classifier runs after the terminal operation without holding SQLite and
    that idempotent replay spends no second classifier call.
    """
    block_gateway = threading.Event()
    gateway_started = threading.Event()
    release_gateway = threading.Event()

    def before_gateway() -> None:
        if block_gateway.is_set():
            gateway_started.set()
            assert release_gateway.wait(5), "test did not release the model turn"

    gateway = FakeGateway(ProposedAnswer("好的"), before=before_gateway)
    values = dict(CAP001_PROVISIONAL_VALUES)
    values.update(
        {"CONTEXT_SOFT_LIMIT_TOKENS": 200, "CONTEXT_HARD_LIMIT_TOKENS": 24000}
    )
    eager = ContextConfig.from_mapping("ctx-eager-compaction", values)
    compaction_started = threading.Event()
    release_compaction = threading.Event()
    classifier_committed = threading.Event()
    classifier_calls: list[str] = []

    def structured_client(*, input_budget_tokens: int, **overrides):
        def generate(**kwargs):
            function_name = kwargs["declarations"][0]["function"]["name"]
            if function_name == "session_boundary_decision":
                classifier_calls.append(function_name)
                # A second SQLite connection must be able to commit while the
                # classifier is running. The old wiring held the anchoring
                # transaction's writer lock across this callback.
                concurrent_engine = create_database_engine(agent_db)
                try:
                    with session_factory(concurrent_engine)() as concurrent:
                        device = concurrent.get(Device, DEVICE_ID)
                        device.display_name = "Classifier concurrent write"
                        concurrent.commit()
                finally:
                    concurrent_engine.dispose()
                classifier_committed.set()
                return SimpleNamespace(
                    error_code=None,
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                thought=False,
                                function_call=SimpleNamespace(
                                    name=function_name,
                                    args={
                                        "decision": "open_new_session",
                                        "reason": "task_boundary",
                                        "confidence_band": "high",
                                    },
                                ),
                            )
                        ]
                    ),
                )
            compaction_started.set()
            assert release_compaction.wait(5), "test did not release compaction"
            sources = json.loads(
                kwargs["messages"][0]["content"].split("\n", 1)[1]
                .removeprefix('<untrusted_data kind="compaction_sources">\n')
                .removesuffix("\n</untrusted_data>")
            )
            first = sources["events"][0]["event_id"]
            return SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text=None,
                            thought=False,
                            function_call=SimpleNamespace(
                                name="context_checkpoint",
                                args={
                                    "goal": {
                                        "value": "记录支出",
                                        "source_refs": [first],
                                    },
                                    "constraints": [],
                                    "decisions": [],
                                    "entities": [],
                                    "completed_steps": [],
                                    "open_items": [],
                                    "superseded_items": [],
                                    "evidence_refs": [],
                                    "exact_refs": [],
                                },
                            ),
                        )
                    ]
                ),
            )

        return StructuredModelClient(
            model="openai/glm-5.3-flash",
            api_key="k",
            api_base="https://open.bigmodel.cn/api/paas/v4/",
            generate=generate,
            input_budget_tokens=input_budget_tokens,
            timeout=overrides.get("timeout", 20.0),
            recorder=overrides.get("recorder"),
            purpose=overrides.get("purpose", "structured"),
        )

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: gateway,
            build_structured_client=structured_client,
            context_config=eager,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            app = build_app(composed.deps)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://agent.local",
            ) as client:
                response = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "帮我记一下这个月的支出"},
                    headers=chat_headers(),
                )
                # The HTTP response is already available while the second model
                # call is blocked. No sleep: the provider itself is the barrier.
                assert response.status_code == 200, response.text
                assert await asyncio.to_thread(compaction_started.wait, 5)
                release_compaction.set()
                await app.state.drain_background_tasks()
                # The detached (202) path uses the same completion callback. It
                # must enqueue compaction after the operation finishes, without
                # relying on the original request still awaiting that worker.
                composed.deps.sync_wait_seconds = 0.05
                compaction_started.clear()
                release_compaction.clear()
                block_gateway.set()
                second_headers = chat_headers()
                detached = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "继续整理这些支出"},
                    headers=second_headers,
                )
                assert gateway_started.is_set()
                assert detached.status_code == 202, detached.text
                assert not compaction_started.is_set()
                release_gateway.set()
                assert await asyncio.to_thread(compaction_started.wait, 5)
                assert classifier_committed.is_set()
                release_compaction.set()
                await app.state.drain_background_tasks()
                replayed = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "继续整理这些支出"},
                    headers=second_headers,
                )
                assert replayed.status_code == 200, replayed.text
                assert classifier_calls == ["session_boundary_decision"]
                return response, detached

    response, detached = asyncio.run(scenario())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
    assert detached.json()["state"] == "accepted"

    engine = create_database_engine(agent_db)
    try:
        with session_factory(engine)() as session:
            checkpoints = session.query(ContextCheckpoint).all()
            sessions = session.query(ContextSession).all()
            events = session.query(ConversationEvent).count()
    finally:
        engine.dispose()

    # Each semantically distinct Session has one Checkpoint; the retrospective
    # split reassigns existing events and deliberately does not append a divider
    # after an already-completed response.
    assert [row.status for row in checkpoints] == ["active", "active"]
    assert len(sessions) == 2
    assert {row.session_id for row in checkpoints} == {
        row.session_id for row in sessions
    }
    assert events == 4


def test_a_turn_that_cannot_be_assembled_fails_safe_without_calling_the_model(
    keys, agent_db, finance
) -> None:
    """An unbuildable context is a clean pre-submit failure, not a crash.

    The budget here is too small for the mandatory context alone, which is the
    `CONTEXT_BUDGET_EXCEEDED` refusal of design §7.3 step 5. The operation must
    end `failed_safe` with that reason, and the model must never be asked.
    """
    gateway = FakeGateway(ProposedAnswer("不该被调用"))
    values = dict(CAP001_PROVISIONAL_VALUES)
    values.update(
        {"CONTEXT_SOFT_LIMIT_TOKENS": 8, "CONTEXT_HARD_LIMIT_TOKENS": 16}
    )
    impossible = ContextConfig.from_mapping("ctx-impossible", values)

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: gateway,
            context_config=impossible,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "记一笔咖啡 18 个人支出"},
                    headers=chat_headers(),
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "failed_safe"
    assert body["failure_reason"] == "CONTEXT_BUDGET_EXCEEDED"
    assert gateway.calls == []


def test_unavailable_context_fails_safe_without_calling_the_model(
    keys, agent_db, finance, monkeypatch
) -> None:
    gateway = FakeGateway(ProposedAnswer("不该被调用"))

    def refuse_context(*args, **kwargs):
        raise AppError(
            ErrorCode.CONTEXT_UNAVAILABLE,
            internal_detail="fixture refused inconsistent Timeline state",
        )

    monkeypatch.setattr(
        "personal_agent.api.composition.ContextBuilder.build",
        refuse_context,
    )

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "记一笔咖啡 18"},
                    headers=chat_headers(),
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "failed_safe"
    assert body["failure_reason"] == "CONTEXT_UNAVAILABLE"
    assert gateway.calls == []


def test_an_expense_write_crosses_both_composition_roots_offline(
    keys, agent_db, tmp_path: Path
) -> None:
    """Only Feishu and the model are deterministic fixtures; both services are real."""
    finance_db = tmp_path / "finance-write.sqlite"
    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
        write_fixture=True,
    )
    update_device(
        agent_db,
        scopes=json.dumps([CAPABILITY_SCOPE, EXPENSE_SCOPE]),
    )
    gateway = FakeGateway(
        ProposedToolCall(
            tool="finance.log_expense",
            arguments={
                "name": "午饭",
                "input_amount": "20.00",
                "input_currency": "CNY",
                "occurred_on": "2026-07-25",
                "is_family_expense": False,
                "entry_kind": "expense",
                "category": "餐饮",
            },
        )
    )
    key = str(uuid.uuid4())
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service), build_gateway=lambda: gateway,
                write_switch=shared_enabled_write_switch(),
            ) as composed:
                async with http_for(composed.deps) as client:
                    token = access_token(
                        scopes=(CAPABILITY_SCOPE, EXPENSE_SCOPE)
                    )
                    return await client.post(
                        "/v1/chat/messages",
                        json={
                            "conversation_id": "c1",
                            "text": "午饭 20，个人支出",
                        },
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Idempotency-Key": key,
                            "Content-Type": "application/json",
                        },
                    )

        response = asyncio.run(scenario())
    finally:
        service.stop()

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["state"] == "succeeded"
    assert body["record_id"] == "rec000001"

    agent_engine = create_database_engine(agent_db)
    with session_factory(agent_engine)() as session:
        operation = session.query(Operation).filter_by(idempotency_key=key).one()
        assert operation.safe_result == "rec000001"
        trace_id = operation.trace_id
    agent_engine.dispose()

    finance_engine = create_finance_engine(finance_db)
    with finance_session_factory(finance_engine)() as session:
        execution = session.get(ToolExecution, key)
        assert execution is not None and execution.state == "succeeded"
        audit_traces = {
            event.trace_id
            for event in session.query(AuditEvent).all()
        }
        assert audit_traces == {trace_id}
    finance_engine.dispose()


def test_category_picker_is_governed_but_not_model_visible_and_replays_in_timeline(
    keys, agent_db, tmp_path: Path
) -> None:
    """The production composition keeps execution and model visibility separate.

    This is the seam fake API authorizers cannot prove: the direct picker must
    cross both real policy/transport roots, while the same tool stays absent
    from model context and `/v1/capabilities`. Its verified row is then an
    append-only Timeline marker a fresh app launch can replay.
    """
    finance_db = tmp_path / "finance-category.sqlite"
    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
        write_fixture=True,
    )
    update_device(
        agent_db,
        scopes=json.dumps([CAPABILITY_SCOPE, EXPENSE_SCOPE]),
    )
    gateway = FakeGateway(
        ProposedToolCall(
            tool="finance.log_expense",
            arguments={
                "name": "午饭",
                "input_amount": "20.00",
                "input_currency": "CNY",
                "occurred_on": "2026-07-25",
                "is_family_expense": False,
                "entry_kind": "expense",
                "category": "餐饮",
            },
        )
    )
    create_key = str(uuid.uuid4())
    correction_key = str(uuid.uuid4())
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service),
                build_gateway=lambda: gateway,
                write_switch=shared_enabled_write_switch(),
            ) as composed:
                async with http_for(composed.deps) as client:
                    token = access_token(scopes=(CAPABILITY_SCOPE, EXPENSE_SCOPE))
                    auth = {"Authorization": f"Bearer {token}"}
                    created = await client.post(
                        "/v1/chat/messages",
                        json={
                            "conversation_id": "c1",
                            "text": "午饭 20，个人支出",
                        },
                        headers={
                            **auth,
                            "Idempotency-Key": create_key,
                            "Content-Type": "application/json",
                        },
                    )
                    corrected = await client.post(
                        "/v1/expense-records/rec000001/category",
                        json={
                            "category": "购物",
                            "expected_current_category": "餐饮",
                        },
                        headers={
                            **auth,
                            "Idempotency-Key": correction_key,
                            "Content-Type": "application/json",
                        },
                    )
                    timeline = await client.get(
                        "/v1/conversations/c1/events", headers=auth
                    )
                    capabilities = await client.get("/v1/capabilities", headers=auth)
                    return created, corrected, timeline, capabilities

        created, corrected, timeline, capabilities = asyncio.run(scenario())
    finally:
        service.stop()

    assert created.status_code == 200, created.text
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["record"]["category"] == "购物"
    markers = [
        event
        for event in timeline.json()["events"]
        if event["event_type"] == "expense_category_corrected"
    ]
    assert len(markers) == 1
    assert markers[0]["content"]["record_id"] == "rec000001"
    assert markers[0]["content"]["record"]["category"] == "购物"
    assert gateway.calls[0]["envelope"].tool_aliases
    assert "finance.update_expense_category" not in (
        gateway.calls[0]["envelope"].tool_aliases
    )
    assert "finance.update_expense_category" not in {
        tool["alias"] for tool in capabilities.json()["tools"]
    }

    finance_engine = create_finance_engine(finance_db)
    with finance_session_factory(finance_engine)() as session:
        assert session.get(ToolExecution, create_key).state == "succeeded"
        assert session.get(ToolExecution, correction_key).state == "succeeded"
    finance_engine.dispose()


def test_a_host_context_the_server_cannot_verify_fails_safe(
    keys, agent_db, tmp_path: Path
) -> None:
    """The server's own gate is the counterparty, and its refusal is honoured.

    The Finance service is started holding *someone else's* public key, so the
    Agent's signature cannot verify. The call must end as a safe failure with a
    stable code -- never as an answer, and never as a crash.
    """
    other = write_keys(tmp_path / "other")
    service = LoopbackFinanceService(
        {MCP_KID_ENV: "svc-test", MCP_PEM_ENV: str(other.service_public_pem)}
    )
    gateway = FakeGateway(ProposedToolCall(tool="meta.capabilities", arguments={}))
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service), build_gateway=lambda: gateway,
                write_switch=shared_enabled_write_switch(),
            ) as composed:
                async with http_for(composed.deps) as client:
                    return await client.post(
                        "/v1/chat/messages",
                        json={"conversation_id": "c1", "text": "能力"},
                        headers=chat_headers(),
                    )

        body = asyncio.run(scenario()).json()
    finally:
        service.stop()

    assert body["state"] == "failed_safe"
    assert body["failure_reason"] == ErrorCode.HOST_CONTEXT_MISMATCH.value


def test_a_direct_answer_needs_no_connector(keys, agent_db, finance) -> None:
    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="你好")),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "在吗"},
                    headers=chat_headers(),
                )

    body = asyncio.run(scenario()).json()
    assert body["state"] == "succeeded"
    assert body["answer"] == "你好"


def test_a_model_failure_is_a_safe_failure(keys, agent_db, finance) -> None:
    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(None, fail=True),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "记一笔"},
                    headers=chat_headers(),
                )

    body = asyncio.run(scenario()).json()
    assert body["state"] == "failed_safe"
    assert body["failure_reason"] == "model_unavailable"


def test_a_tool_the_service_never_composed_is_refused(keys, agent_db, finance) -> None:
    """The model naming an undiscovered write tool gains nothing."""
    gateway = FakeGateway(
        ProposedToolCall(
            tool="finance.log_expense",
            arguments={
                "input_amount": "20.00",
                "expense_name": "午饭",
                "occurred_on": "2026-07-25",
                "is_family_expense": False,
                "category": "餐饮",
                "entry_kind": "expense",
                "input_currency": "CNY",
            },
        )
    )

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance), build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "午饭 20 个人支出"},
                    headers=chat_headers(),
                )

    body = asyncio.run(scenario()).json()
    assert body["state"] == "failed_safe"
    assert body["failure_reason"] == "policy_denied"


def test_a_tool_outside_the_configured_allowlist_is_invisible(
    keys, agent_db, finance
) -> None:
    """A read-only rollout is expressed by narrowing the allowlist."""
    gateway = FakeGateway(ProposedToolCall(tool="meta.capabilities", arguments={}))

    async def scenario():
        async with agent_service(
            config_for(
                agent_db, finance, allowed_tools=frozenset({"finance.query_expenses"})
            ),
            build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                chat = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "能力"},
                    headers=chat_headers(),
                )
                capabilities = await client.get(
                    "/v1/capabilities",
                    headers={"Authorization": f"Bearer {access_token()}"},
                )
                return chat.json(), capabilities.json()

    chat, capabilities = asyncio.run(scenario())
    assert chat["state"] == "failed_safe"
    assert chat["failure_reason"] == "policy_denied"
    assert capabilities["tools"] == []
    assert gateway.calls[0]["envelope"].tool_aliases == ()


# --- device freshness --------------------------------------------------------


def test_a_device_revoked_during_the_model_turn_cannot_dispatch(
    keys, agent_db, finance
) -> None:
    """The window a cached authorisation snapshot would leave open."""
    gateway = FakeGateway(
        ProposedToolCall(tool="meta.capabilities", arguments={}),
        before=lambda: update_device(
            agent_db, status="revoked", revoked_at=datetime.now(timezone.utc)
        ),
    )

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance), build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "能力"},
                    headers=chat_headers(),
                )

    body = asyncio.run(scenario()).json()
    assert body["state"] == "failed_safe"
    assert body["failure_reason"] == "policy_denied"


def test_a_device_row_cannot_be_deleted_while_its_requests_exist(
    keys, agent_db
) -> None:
    """Deletion is not how a device goes away; the audit trail holds it."""
    seed_in_flight_operation(agent_db, str(uuid.uuid4()))
    with pytest.raises(IntegrityError):
        delete_device(agent_db)


def test_an_unknown_device_dispatches_nothing(keys, agent_db) -> None:
    """The one case a foreign key cannot cover: no row, so no call at all.

    The bridge and control plane are `None` on purpose -- touching either would
    raise rather than quietly fail safe, so this proves nothing is attempted.
    """
    dispatcher = DeviceBoundDispatcher(
        device_id="dev-does-not-exist",
        sessions=session_factory(create_database_engine(agent_db)),
        bridge=None,
        control=None,
        signing_ring=None,
        user_id=USER_ID,
        agent_id="personal-agent-api",
        trace_id="00-trace-span-01",
        enabled_tools=frozenset({"meta.capabilities"}),
        manifest_version=MANIFEST_VERSION,
    )

    resolved = dispatcher.resolve(tool="meta.capabilities", model_args={})
    committed = dispatcher.commit(
        intent=WriteIntent(tool="finance.log_expense", model_args={}),
        idempotency_key=str(uuid.uuid4()),
        duplicate_override=None,
    )
    assert isinstance(resolved, ResolveFailedSafe)
    assert resolved.reason == "policy_denied"
    assert isinstance(committed, CommitFailedSafe)
    assert committed.reason == "policy_denied"


def test_a_device_bound_to_another_manifest_sees_nothing(
    keys, agent_db, finance
) -> None:
    """A stale `allowed_tools_version` grants no tool at all, not a stale set."""
    update_device(agent_db, allowed_tools_version="v-old")
    gateway = FakeGateway(ProposedToolCall(tool="meta.capabilities", arguments={}))

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance), build_gateway=lambda: gateway,
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                token = issue_access_token(
                    load_access_token_ring(),
                    device_id=DEVICE_ID,
                    device_key_thumbprint=THUMBPRINT,
                    scopes=[CAPABILITY_SCOPE],
                    allowed_tools_version="v-old",
                    now=utc_now(),
                )
                chat = await client.post(
                    "/v1/chat/messages",
                    json={"conversation_id": "c1", "text": "能力"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": str(uuid.uuid4()),
                        "Content-Type": "application/json",
                    },
                )
                capabilities = await client.get(
                    "/v1/capabilities",
                    headers={"Authorization": f"Bearer {token}"},
                )
                return chat.json(), capabilities.json()

    chat, capabilities = asyncio.run(scenario())
    assert capabilities["tools"] == []
    assert chat["state"] == "failed_safe"
    assert chat["failure_reason"] == "policy_denied"
    assert gateway.calls[0]["envelope"].tool_aliases == ()


def test_capabilities_reports_the_effective_tools_without_schemas(
    keys, agent_db, finance
) -> None:
    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.get(
                    "/v1/capabilities",
                    headers={"Authorization": f"Bearer {access_token()}"},
                )

    body = asyncio.run(scenario()).json()
    assert body["allowed_tools_version"] == MANIFEST_VERSION
    assert body["tools"] == [
        {
            "alias": "meta.capabilities",
            "description": "返回当前设备实际可用的工具，用于渐进披露能力。",
            "risk_level": "R0",
            "required_scopes": [CAPABILITY_SCOPE],
        }
    ]


def test_capabilities_reports_current_device_binding_before_token_refresh(
    keys, agent_db, finance
) -> None:
    """The capability projection cannot lag a rebinding token claim."""
    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                return await client.get(
                    "/v1/capabilities",
                    headers={
                        "Authorization": (
                            f"Bearer {access_token(version='old-token-claim')}"
                        )
                    },
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json()["allowed_tools_version"] == MANIFEST_VERSION


# --- startup recovery --------------------------------------------------------


class UnreadableControl:
    """A control plane that is down, not one that says 'nothing here'."""

    async def get_execution(self, idempotency_key: str):
        raise ControlPlaneError("control plane is unreachable")


class BodyControl:
    """Returns one verbatim control body, so a malformed one can be tested."""

    def __init__(self, body) -> None:
        self._body = body

    async def get_execution(self, idempotency_key: str):
        return self._body


def seed_in_flight_operation(
    database: Path,
    key: str,
    *,
    target: str = "source_in_progress",
    stranded: bool = True,
) -> str:
    """Seed an operation these recovery tests can adopt.

    `stranded` backdates every transition past `RECOVERY_QUIET_PERIOD`, which is
    what "in flight when the process died" actually looks like: the row stopped
    moving before the restart. Seeding it at `utc_now()` would describe an
    operation a live worker still owns, and since 2026-08-03 recovery correctly
    refuses to touch one of those -- so a test that seeds it that way is not
    testing startup recovery at all.
    """
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        now = utc_now()
        if stranded:
            now = now - RECOVERY_QUIET_PERIOD - timedelta(seconds=1)
        operation = open_operation(
            session,
            device_id=DEVICE_ID,
            client_request_id=key,
            request_fingerprint="fp",
            now=now,
        ).operation
        session.flush()
        path = ("interpreting", "dispatching", "source_in_progress")
        for next_state in path[: path.index(target) + 1]:
            session.refresh(operation)
            transition_operation(
                session,
                operation_id=operation.operation_id,
                current_state=operation.state,
                current_version=operation.state_version,
                target_state=next_state,
                now=now,
            )
        session.commit()
        operation_id = operation.operation_id
    engine.dispose()
    return operation_id


def read_operation(database: Path, operation_id: str) -> Operation:
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        operation = session.get(Operation, operation_id)
        session.expunge(operation)
    engine.dispose()
    return operation


def test_startup_recovery_projects_a_verified_finance_success(
    keys, agent_db
) -> None:
    """The body shape is Finance's own, taken from the control contract."""
    key = str(uuid.uuid4())
    operation_id = seed_in_flight_operation(agent_db, key)

    recover_at_startup(
        session_factory(create_database_engine(agent_db)),
        BodyControl(
            {
                "idempotency_key": key,
                "tool": "finance.log_expense",
                "state": "succeeded",
                "record_id": "recTEST123",
                "receipt_verified": True,
            }
        ),
    )

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "succeeded"
    assert operation.safe_result == "recTEST123"


def test_an_unreadable_control_plane_leaves_operations_untouched(
    keys, agent_db
) -> None:
    key = str(uuid.uuid4())
    operation_id = seed_in_flight_operation(agent_db, key)

    assert (
        recover_at_startup(
            session_factory(create_database_engine(agent_db)), UnreadableControl()
        )
        == []
    )

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "source_in_progress"


def test_startup_recovery_fails_safe_when_no_replayable_intent_exists(
    keys, agent_db
) -> None:
    key = str(uuid.uuid4())
    operation_id = seed_in_flight_operation(agent_db, key, target="dispatching")

    recover_at_startup(
        session_factory(create_database_engine(agent_db)), BodyControl(None)
    )

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "failed_safe"
    assert "no complete replayable tool intent" in operation.failure_reason


@pytest.mark.parametrize(
    "body",
    [
        {"idempotency_key": "k", "record_id": "rec1", "receipt_verified": True},
        {"state": "succeeded", "record_id": 12, "receipt_verified": True},
        {"state": "succeeded", "record_id": "rec1"},
        {"state": "", "record_id": None, "receipt_verified": False},
    ],
)
def test_a_malformed_execution_body_is_never_read_as_never_seen(
    keys, agent_db, body
) -> None:
    """"Finance never saw this" is the one reading that could duplicate a write."""
    key = str(uuid.uuid4())
    operation_id = seed_in_flight_operation(agent_db, key)

    recover_at_startup(
        session_factory(create_database_engine(agent_db)), BodyControl(body)
    )

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "source_in_progress"


def test_startup_recovery_reads_the_real_control_plane(
    keys, agent_db, tmp_path: Path
) -> None:
    """The reader is exercised against the deployed control endpoint."""
    finance_db = tmp_path / "finance.sqlite"
    key = str(uuid.uuid4())
    _seed_finance_success(finance_db, key)
    operation_id = seed_in_flight_operation(agent_db, key)

    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
    )
    try:

        async def scenario():
            async with agent_service(config_for(agent_db, service), write_switch=shared_enabled_write_switch()) as composed:
                assert composed.catalog_aliases == ("meta.capabilities",)

        asyncio.run(scenario())
    finally:
        service.stop()

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "succeeded"
    assert operation.safe_result == f"rec_{key}"


def test_periodic_recovery_projects_an_operation_without_a_service_restart(
    keys, agent_db, tmp_path: Path
) -> None:
    """Design 7.6.1 requires a scan every minute, not only at process startup."""
    finance_db = tmp_path / "finance-periodic.sqlite"
    key = str(uuid.uuid4())
    _seed_finance_success(finance_db, key)

    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
    )
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service),
                recovery_interval_seconds=0.01,
                write_switch=shared_enabled_write_switch(),
            ):
                operation_id = seed_in_flight_operation(agent_db, key)
                for _ in range(100):
                    if read_operation(agent_db, operation_id).state == "succeeded":
                        return operation_id
                    await asyncio.sleep(0.01)
                pytest.fail("periodic recovery did not resolve the operation")

        operation_id = asyncio.run(scenario())
    finally:
        service.stop()

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "succeeded"
    assert operation.safe_result == f"rec_{key}"


def test_periodic_recovery_never_adopts_an_operation_a_worker_still_owns(
    keys, agent_db, tmp_path: Path
) -> None:
    """The 2026-08-03 P0, at the composition root rather than at the planner.

    Same wiring as the test above -- a real Finance MCP over a socket, the real
    control client, recovery running continuously -- but the operation is fresh,
    which is what a turn in progress looks like. Finance already reports success
    for the key, so a recovery worker that ignored liveness would happily and
    *correctly-looking-ly* resolve it, and race the live worker's own transition
    while doing so. It must leave the row exactly where it found it.
    """
    finance_db = tmp_path / "finance-live.sqlite"
    key = str(uuid.uuid4())
    _seed_finance_success(finance_db, key)

    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
    )
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service),
                recovery_interval_seconds=0.01,
                write_switch=shared_enabled_write_switch(),
            ):
                operation_id = seed_in_flight_operation(
                    agent_db, key, stranded=False
                )
                # Many scans, all of which must decline to touch it.
                await asyncio.sleep(0.5)
                return operation_id

        operation_id = asyncio.run(scenario())
    finally:
        service.stop()

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "source_in_progress"
    assert operation.safe_result is None
    assert operation.failure_reason is None


def _seed_finance_success(database: Path, key: str) -> None:
    from personal_data_mcp.storage.engine import (
        create_all as finance_create_all,
        create_database_engine as finance_engine,
        session_factory as finance_sessions,
    )
    from personal_data_mcp.storage.execution_store import (
        prepare_execution,
        record_receipt,
        transition,
    )
    from personal_data_mcp.storage.models import ToolExecution

    engine = finance_engine(database)
    finance_create_all(engine)
    now = utc_now()
    with finance_sessions(engine)() as session:
        prepare_execution(
            session,
            idempotency_key=key,
            tool="finance.log_expense",
            request_fingerprint="fp",
            client_token=str(uuid.uuid4()),
            encrypted_payload=None,
            now=now,
        )
        version = session.get(ToolExecution, key).state_version
        version = transition(
            session,
            idempotency_key=key,
            current_state="prepared",
            current_version=version,
            target_state="submitting",
            now=now,
        )
        record_receipt(
            session,
            receipt_id=f"rc_{key}",
            idempotency_key=key,
            table_kind="expense",
            record_id=f"rec_{key}",
            now=now,
            verified=True,
        )
        version = transition(
            session,
            idempotency_key=key,
            current_state="submitting",
            current_version=version,
            target_state="committed_unverified",
            now=now,
        )
        transition(
            session,
            idempotency_key=key,
            current_state="committed_unverified",
            current_version=version,
            target_state="succeeded",
            now=now,
        )
        session.commit()
    engine.dispose()


# --- the daily review surface (DEV-028) --------------------------------------


def _seed_review_card(database: Path, *, known_record: str, unknown_record: str) -> str:
    """One card with two items: one Finance wrote, one it never did."""
    from personal_agent.storage.models import DailyReview, DailyReviewItem

    review_id = str(uuid.uuid4())
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        now = utc_now()
        session.add(
            DailyReview(
                review_id=review_id,
                review_date="2026-07-25",
                status="pending",
                created_at=now,
            )
        )
        for record_id in (known_record, unknown_record):
            session.add(
                DailyReviewItem(
                    item_id=str(uuid.uuid4()),
                    review_id=review_id,
                    tool="finance.log_expense",
                    record_id=record_id,
                    committed_at=now,
                )
            )
        session.commit()
    engine.dispose()
    return review_id


def test_opening_a_card_reaches_the_real_finance_control_plane(
    keys, agent_db, tmp_path: Path
) -> None:
    """The composed reader is wired to the deployed endpoint, not to a fake.

    The two items differ only in whether Finance holds a verified receipt, and
    only Finance can tell them apart -- so two different answers on one card is
    evidence the read really crossed the socket. Neither carries values: this
    service has no ledger config, so it refuses to read Feishu rather than
    inventing a card, which is the honest answer offline.
    """
    finance_db = tmp_path / "finance.sqlite"
    key = str(uuid.uuid4())
    _seed_finance_success(finance_db, key)
    review_id = _seed_review_card(
        agent_db, known_record=f"rec_{key}", unknown_record="recNeverWritten"
    )

    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
    )
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service),
                build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
                write_switch=shared_enabled_write_switch(),
            ) as composed:
                async with http_for(composed.deps) as client:
                    return await client.get(
                        f"/v1/daily-reviews/{review_id}",
                        headers={"Authorization": f"Bearer {access_token()}"},
                    )

        response = asyncio.run(scenario())
    finally:
        service.stop()

    assert response.status_code == 200
    by_record = {item["record_id"]: item for item in response.json()["items"]}
    assert by_record[f"rec_{key}"]["unavailable"] == "source_unavailable"
    assert by_record["recNeverWritten"]["unavailable"] == "no_receipt"


def test_the_review_list_is_served_by_the_composed_app(
    keys, agent_db, finance, tmp_path: Path
) -> None:
    review_id = _seed_review_card(
        agent_db, known_record="recA", unknown_record="recB"
    )

    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
            write_switch=shared_enabled_write_switch(),
        ) as composed:
            async with http_for(composed.deps) as client:
                listed = await client.get(
                    "/v1/daily-reviews",
                    params={"status": "pending"},
                    headers={"Authorization": f"Bearer {access_token()}"},
                )
                acked = await client.post(
                    f"/v1/daily-reviews/{review_id}/ack",
                    headers={"Authorization": f"Bearer {access_token()}"},
                )
                return listed, acked

    listed, acked = asyncio.run(scenario())

    assert [r["review_id"] for r in listed.json()["reviews"]] == [review_id]
    assert listed.json()["reviews"][0]["item_count"] == 2
    assert acked.json()["status"] == "reviewed"


def test_calendar_sync_crosses_both_composition_roots_offline(
    keys, agent_db, tmp_path: Path
) -> None:
    """The device-side data channel is composed, not just seam-tested.

    The sync route must execute `calendar.ingest_events` through the real
    governed bridge against a real MCP process over a loopback socket, under a
    Host Context naming the authenticated caller. The mirror row is then
    stamped with that signed device identity and the text is sealed with the
    server's keyring -- the assertions read the Finance database through a
    raw connection exactly as an operator would.
    """
    from personal_agent_core.tool_ir import SCOPE_CALENDAR_READ
    from personal_data_mcp.storage.engine import (
        create_database_engine as finance_engine,
        session_factory as finance_sessions,
    )
    from personal_data_mcp.storage.models import CalendarEvent

    finance_db = tmp_path / "finance-calendar.sqlite"
    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
        calendar=True,
    )
    update_device(
        agent_db,
        scopes=json.dumps([CAPABILITY_SCOPE, SCOPE_CALENDAR_READ]),
    )
    body = {
        "window_start": "2026-09-07T00:00:00+08:00",
        "window_end": "2026-09-08T00:00:00+08:00",
        "events": [
            {
                "event_identifier": "ek-loopback-1",
                "calendar_identifier": "cal-1",
                "title": "网球",
                "start": "2026-09-07T15:00:00+08:00",
                "end": "2026-09-07T16:30:00+08:00",
                "all_day": False,
                "location": None,
                "notes": None,
                "last_modified": "2026-09-06T20:00:00+08:00",
            }
        ],
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
    }
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service),
                build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
                write_switch=shared_enabled_write_switch(),
            ) as composed:
                # The production composition must actually wire the route, or
                # the endpoint below would answer INTERNAL_ERROR, not succeed.
                assert composed.deps.sync_ingest is not None
                async with http_for(composed.deps) as client:
                    token = access_token(
                        scopes=(CAPABILITY_SCOPE, SCOPE_CALENDAR_READ)
                    )
                    return await client.post(
                        "/v1/calendar/sync",
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )

        response = asyncio.run(scenario())
    finally:
        service.stop()

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "upserted": 1,
        "skipped": 0,
        "marked_deleted": 0,
    }

    engine = finance_engine(finance_db)
    with finance_sessions(engine)() as session:
        row = session.query(CalendarEvent).one()
        assert row.event_identifier == "ek-loopback-1"
        # The device identity is the signed Host Context claim -- the caller's
        # device id -- never a composition constant and never a payload field.
        assert row.device_id == DEVICE_ID
        assert row.is_deleted is False
    engine.dispose()


def test_calendar_sync_refuses_a_device_without_the_current_manifest(
    keys, agent_db, tmp_path: Path
) -> None:
    """The stale-manifest gate lives in the bridge's `execute`, and the sync
    route passes through it: a device enrolled against an old version is
    refused by the real policy root even though its token is valid and its
    scope present."""
    from personal_agent_core.tool_ir import SCOPE_CALENDAR_READ

    finance_db = tmp_path / "finance-calendar-stale.sqlite"
    service = LoopbackFinanceService(
        {
            MCP_KID_ENV: "svc-test",
            MCP_PEM_ENV: str(keys.service_public_pem),
        },
        database=finance_db,
        calendar=True,
    )
    update_device(
        agent_db,
        scopes=json.dumps([CAPABILITY_SCOPE, SCOPE_CALENDAR_READ]),
        allowed_tools_version="0.0.1-stale",
    )
    body = {
        "window_start": "2026-09-07T00:00:00+08:00",
        "window_end": "2026-09-08T00:00:00+08:00",
        "events": [],
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
    }
    try:

        async def scenario():
            async with agent_service(
                config_for(agent_db, service),
                build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
                write_switch=shared_enabled_write_switch(),
            ) as composed:
                async with http_for(composed.deps) as client:
                    token = access_token(
                        scopes=(CAPABILITY_SCOPE, SCOPE_CALENDAR_READ),
                        version="0.0.1-stale",
                    )
                    return await client.post(
                        "/v1/calendar/sync",
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )

        response = asyncio.run(scenario())
    finally:
        service.stop()

    assert response.status_code == 403, response.text
    # One opaque code for "not an effective tool" (not allowlisted, not
    # discovered, drifted, or granted) by design: distinguishing them would map
    # out the tool surface. The version gate denies through the same door.
    assert response.json()["error"]["code"] == "TOOL_NOT_ALLOWLISTED"
