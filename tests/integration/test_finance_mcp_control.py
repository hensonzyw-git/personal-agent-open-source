"""DEV-015 Commit 3: the internal control API.

Two read-only endpoints for the scheduler and the crash-recovery scan, behind a
control token that is not interchangeable with a tool-call Host Context. The
tests drive the composed app in process through an ASGI transport and seed the
Finance database directly, so the queries run against the real schema.

The structural guarantee under test is that the control plane is not on the
model's tool surface: it is not an MCP tool, and it is not on `/mcp`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fixtures.service_keys import SignedCaller
from personal_agent_core.control_token import (
    ControlAction,
    sign_control_token,
)
from personal_data_mcp.server.app import build_app
from personal_data_mcp.server.config import ServerConfig
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import (
    prepare_execution,
    record_receipt,
    transition,
)
from personal_data_mcp.storage.models import ToolExecution


# A commit instant that lands on 2026-07-23 in Asia/Shanghai (UTC+8): local
# 22:00 is 14:00 UTC, well inside the day.
COMMIT_UTC = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
# 2026-07-23 23:30 local is 15:30 UTC, still the 23rd locally.
LATE_SAME_DAY_UTC = datetime(2026, 7, 23, 15, 30, tzinfo=timezone.utc)
# 2026-07-24 00:30 local is 2026-07-23 16:30 UTC: a different ledger day.
NEXT_DAY_UTC = datetime(2026, 7, 23, 16, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def seed_succeeded_expense(session, *, key: str, committed_at: datetime) -> str:
    """Drive one expense execution to `succeeded` with a verified receipt."""
    prepare_execution(
        session,
        idempotency_key=key,
        tool="finance.log_expense",
        request_fingerprint="fp",
        client_token=str(uuid.uuid4()),
        encrypted_payload=None,
        now=committed_at,
    )
    record_id = f"rec_{key}"
    execution = session.get(ToolExecution, key)
    version = execution.state_version
    version = transition(
        session,
        idempotency_key=key,
        current_state="prepared",
        current_version=version,
        target_state="submitting",
        now=committed_at,
    )
    record_receipt(
        session,
        receipt_id=f"rc_{key}",
        idempotency_key=key,
        table_kind="expense",
        record_id=record_id,
        now=committed_at,
        verified=True,
    )
    version = transition(
        session,
        idempotency_key=key,
        current_state="submitting",
        current_version=version,
        target_state="committed_unverified",
        now=committed_at,
    )
    transition(
        session,
        idempotency_key=key,
        current_state="committed_unverified",
        current_version=version,
        target_state="succeeded",
        now=committed_at,
    )
    session.commit()
    return record_id


@pytest.fixture()
def caller() -> SignedCaller:
    return SignedCaller()


@pytest.fixture()
def sf(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


@pytest.fixture()
def client(caller, sf):
    app = build_app(
        ServerConfig(), verification_ring=caller.ring, session_factory=sf
    )
    transport = httpx.ASGITransport(app=app)

    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=transport, base_url="http://control.local"
        )

    return make


def control_headers(caller, action: ControlAction, resource: str) -> dict:
    token = sign_control_token(caller.ring, action=action, resource=resource)
    return {"Authorization": f"Bearer {token}"}


# --- execution status (7.6.1) ------------------------------------------------


def test_get_execution_returns_not_found_for_an_unknown_key(caller, client) -> None:
    key = str(uuid.uuid4())

    async def scenario():
        async with client() as c:
            return await c.get(
                f"/internal/v1/executions/{key}",
                headers=control_headers(caller, ControlAction.GET_EXECUTION, key),
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"status": "not_found"}


def test_get_execution_reports_the_state_for_recovery(caller, client, sf) -> None:
    key = str(uuid.uuid4())
    with sf() as session:
        record_id = seed_succeeded_expense(session, key=key, committed_at=COMMIT_UTC)

    async def scenario():
        async with client() as c:
            return await c.get(
                f"/internal/v1/executions/{key}",
                headers=control_headers(caller, ControlAction.GET_EXECUTION, key),
            )

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "found"
    assert body["execution"]["state"] == "succeeded"
    assert body["execution"]["tool"] == "finance.log_expense"
    assert body["execution"]["record_id"] == record_id
    assert body["execution"]["receipt_verified"] is True


# --- successful writes by day (7.7) -----------------------------------------


def test_successful_writes_are_scoped_to_the_ledger_day(caller, client, sf) -> None:
    with sf() as session:
        seed_succeeded_expense(session, key="k-early", committed_at=COMMIT_UTC)
        seed_succeeded_expense(
            session, key="k-late", committed_at=LATE_SAME_DAY_UTC
        )
        # This one commits after local midnight, so it belongs to 2026-07-24.
        seed_succeeded_expense(session, key="k-next", committed_at=NEXT_DAY_UTC)

    async def scenario(date_str):
        async with client() as c:
            return await c.get(
                "/internal/v1/successful-writes",
                params={"write_date": date_str},
                headers=control_headers(
                    caller, ControlAction.LIST_SUCCESSFUL_WRITES, date_str
                ),
            )

    resp = run(scenario("2026-07-23"))
    assert resp.status_code == 200
    writes = resp.json()["writes"]
    record_ids = {w["record_id"] for w in writes}
    assert record_ids == {"rec_k-early", "rec_k-late"}
    assert all(w["tool"] == "finance.log_expense" for w in writes)

    resp_next = run(scenario("2026-07-24"))
    assert {w["record_id"] for w in resp_next.json()["writes"]} == {"rec_k-next"}


def test_a_bad_write_date_is_a_client_error(caller, client) -> None:
    bad = "23-07-2026"

    async def scenario():
        async with client() as c:
            return await c.get(
                "/internal/v1/successful-writes",
                params={"write_date": bad},
                headers=control_headers(
                    caller, ControlAction.LIST_SUCCESSFUL_WRITES, bad
                ),
            )

    resp = run(scenario())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


# --- the control token cannot be swapped for a tool-call token ---------------


def test_a_missing_token_is_refused(client) -> None:
    key = str(uuid.uuid4())

    async def scenario():
        async with client() as c:
            return await c.get(f"/internal/v1/executions/{key}")

    resp = run(scenario())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "HOST_CONTEXT_MISMATCH"


def test_a_token_bound_to_a_different_key_is_refused(caller, client) -> None:
    key = str(uuid.uuid4())
    other = str(uuid.uuid4())

    async def scenario():
        async with client() as c:
            # Token minted for `other`, presented on `key`.
            return await c.get(
                f"/internal/v1/executions/{key}",
                headers=control_headers(
                    caller, ControlAction.GET_EXECUTION, other
                ),
            )

    resp = run(scenario())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "HOST_CONTEXT_MISMATCH"


def test_a_tool_call_host_context_does_not_authorise_control(caller, client) -> None:
    """A Host Context (aud=personal-data-mcp) is rejected on the control plane."""
    key = str(uuid.uuid4())
    # These are the headers a tool call would carry; the bearer is a Host
    # Context, not a control token, so its audience is wrong for /internal.
    tool_headers = caller.headers("meta.capabilities", {})

    async def scenario():
        async with client() as c:
            return await c.get(
                f"/internal/v1/executions/{key}",
                headers={"Authorization": tool_headers["Authorization"]},
            )

    resp = run(scenario())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "HOST_CONTEXT_MISMATCH"


def test_a_control_token_does_not_authorise_a_tool_call(caller) -> None:
    """The reverse: a control token cannot pass the tool-call gate."""
    from personal_data_mcp.server.authz import Authorizer

    key = str(uuid.uuid4())
    control = sign_control_token(
        caller.ring, action=ControlAction.GET_EXECUTION, resource=key
    )
    authorizer = Authorizer(caller.ring)
    headers = {
        "authorization": f"Bearer {control}",
        "x-request-id": str(uuid.uuid4()),
        "idempotency-key": str(uuid.uuid4()),
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "x-user-id": "henson",
        "x-timezone": "Asia/Shanghai",
    }
    from personal_agent_core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as caught:
        authorizer.authorize(
            tool="meta.capabilities",
            arguments={},
            headers=headers,
            required_scopes=("meta.capabilities.read",),
        )
    assert caught.value.code == ErrorCode.HOST_CONTEXT_MISMATCH


# --- the control plane is not an MCP tool -----------------------------------


def test_control_endpoints_are_not_in_the_mcp_tool_catalog() -> None:
    """Nothing on the tool surface can reach execution state or record ids."""
    from personal_data_mcp.server.app import build_registry

    registry = build_registry()
    names = registry.names()
    assert not any("internal" in n or "execution" in n for n in names)
    assert not any("successful" in n or "review" in n for n in names)


def test_a_get_on_the_control_path_is_not_swallowed_by_the_mcp_guard(
    caller, client
) -> None:
    """The GET-405 guard is scoped to /mcp; control GETs must pass through."""
    key = str(uuid.uuid4())

    async def scenario():
        async with client() as c:
            return await c.get(
                f"/internal/v1/executions/{key}",
                headers=control_headers(caller, ControlAction.GET_EXECUTION, key),
            )

    resp = run(scenario())
    # Not a 405: the guard did not treat this as the MCP path.
    assert resp.status_code == 200
