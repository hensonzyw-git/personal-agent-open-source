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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import CommitFailedSafe, ResolveFailedSafe
from personal_agent.api.operation_store import open_operation, transition_operation
from personal_agent.auth.tokens import issue_access_token
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
from personal_agent.storage.models import Conversation, Device, Operation
from personal_agent_core.errors import ErrorCode
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
            )
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
            )
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="loopback"):
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
            )
        ):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="Finance"):
        asyncio.run(scenario())


def test_a_missing_model_credential_fails_composition(
    keys, agent_db, finance, monkeypatch
) -> None:
    monkeypatch.delenv("ZAI_API_KEY")

    async def scenario():
        async with agent_service(config_for(agent_db, finance)):
            pytest.fail("composition should have refused")

    with pytest.raises(CompositionError, match="model gateway"):
        asyncio.run(scenario())


def test_a_tampered_model_endpoint_fails_composition(
    keys, agent_db, finance, monkeypatch
) -> None:
    """A credential may only travel to the pinned provider endpoint."""
    monkeypatch.setenv("GLM_OPENAI_BASE_URL", "https://open.bigmodel.cn.evil.test/api/paas/v4/")

    async def scenario():
        async with agent_service(config_for(agent_db, finance)):
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
            )
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
            )
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
            config_for(agent_db, finance), build_gateway=lambda: gateway
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
    assert [tool.alias for tool in gateway.calls[0]["tools"]] == ["meta.capabilities"]


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
                config_for(agent_db, service), build_gateway=lambda: gateway
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
                config_for(agent_db, service), build_gateway=lambda: gateway
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
            config_for(agent_db, finance), build_gateway=lambda: gateway
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
    assert gateway.calls[0]["tools"] == []


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
            config_for(agent_db, finance), build_gateway=lambda: gateway
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
            config_for(agent_db, finance), build_gateway=lambda: gateway
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
    assert gateway.calls[0]["tools"] == []


def test_capabilities_reports_the_effective_tools_without_schemas(
    keys, agent_db, finance
) -> None:
    async def scenario():
        async with agent_service(
            config_for(agent_db, finance),
            build_gateway=lambda: FakeGateway(ProposedAnswer(text="hi")),
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
    database: Path, key: str, *, target: str = "source_in_progress"
) -> str:
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        now = utc_now()
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
            async with agent_service(config_for(agent_db, service)) as composed:
                assert composed.catalog_aliases == ("meta.capabilities",)

        asyncio.run(scenario())
    finally:
        service.stop()

    operation = read_operation(agent_db, operation_id)
    assert operation.state == "succeeded"
    assert operation.safe_result == f"rec_{key}"


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
