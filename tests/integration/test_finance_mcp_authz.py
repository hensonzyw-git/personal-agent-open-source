"""DEV-015 Commit 2: the second authorisation gate, in process.

These tests drive `dispatch` directly with controlled headers and a controlled
signing key, which is where the gate's logic lives. The cross-process transport
is covered separately; here the point is the ordering invariant and every
rejection branch.

The load-bearing test is `test_a_rejected_call_creates_no_execution_row`. The
acceptance for DEV-015 is "no JWT / bad Host Context, zero execution". "It
raised" is not that claim. So a handler is wired to the real execution store and
the execution table is inspected after each rejection: the guarantee is that the
table is still empty, not merely that an exception was thrown.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from fixtures.service_keys import SignedCaller
from personal_agent_core.errors import ErrorCode
from personal_data_mcp.server.app import dispatch
from personal_data_mcp.server.handlers import ToolInvocation, ToolRegistry
from personal_data_mcp.server.meta import build_handler as build_meta_handler
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import prepare_execution
from personal_data_mcp.storage.models import ToolExecution


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
EXPENSE = {
    "name": "午饭",
    "input_amount": "45.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def finance_session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


def execution_count(session) -> int:
    return len(list(session.scalars(select(ToolExecution))))


@pytest.fixture()
def caller() -> SignedCaller:
    return SignedCaller(
        scopes=("meta.capabilities.read", "finance.expense.write"),
    )


def registry_with_write_probe(finance_session) -> ToolRegistry:
    """A registry whose log_expense handler touches the real execution store.

    It is not the DEV-018 write path; it does the one thing that matters for
    this gate's ordering test -- create an execution row -- so that a row's
    presence or absence is a faithful signal of whether the handler ran.
    """
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))

    async def log_expense(invocation: ToolInvocation) -> dict:
        prepare_execution(
            finance_session,
            idempotency_key=invocation.verified_call.idempotency_key,
            tool="finance.log_expense",
            request_fingerprint="fp",
            client_token=str(uuid.uuid4()),
            encrypted_payload=None,
            now=NOW,
        )
        finance_session.commit()
        return {
            "status": "created",
            "record_id": "probe",
            "source_system": "test",
            "committed_at": "2026-07-23T15:00:00+08:00",
        }

    registry.register("finance.log_expense", log_expense)
    return registry


# --- the ordering invariant --------------------------------------------------


@pytest.mark.parametrize(
    "mutate, expected",
    [
        pytest.param(
            lambda h, c: {k: v for k, v in h.items() if k != "Authorization"},
            ErrorCode.HOST_CONTEXT_MISMATCH,
            id="no-bearer",
        ),
        pytest.param(
            lambda h, c: {k: v for k, v in h.items() if k != "Idempotency-Key"},
            ErrorCode.HOST_CONTEXT_MISMATCH,
            id="no-idempotency-key",
        ),
        pytest.param(
            lambda h, c: {k: v for k, v in h.items() if k != "X-User-ID"},
            ErrorCode.HOST_CONTEXT_MISMATCH,
            id="no-user-id",
        ),
        pytest.param(
            lambda h, c: {**h, "Idempotency-Key": str(uuid.uuid4())},
            ErrorCode.HOST_CONTEXT_MISMATCH,
            id="swapped-idempotency-key",
        ),
        pytest.param(
            lambda h, c: {**h, "X-Timezone": "UTC"},
            ErrorCode.HOST_CONTEXT_MISMATCH,
            id="swapped-timezone",
        ),
        pytest.param(
            lambda h, c: c.headers(
                "finance.log_expense", EXPENSE, scopes=("meta.capabilities.read",)
            ),
            ErrorCode.SCOPE_DENIED,
            id="missing-scope",
        ),
    ],
)
def test_a_rejected_call_creates_no_execution_row(
    finance_session, caller, mutate, expected
) -> None:
    registry = registry_with_write_probe(finance_session)
    good = caller.headers("finance.log_expense", EXPENSE)
    headers = {k.lower(): v for k, v in mutate(good, caller).items()}

    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            EXPENSE,
            headers,
        )
    )
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"]["code"] == expected.value
    # The whole point: the write probe never ran.
    assert execution_count(finance_session) == 0


def test_tampered_arguments_create_no_execution_row(
    finance_session, caller
) -> None:
    registry = registry_with_write_probe(finance_session)
    headers = {
        k.lower(): v
        for k, v in caller.headers("finance.log_expense", EXPENSE).items()
    }
    tampered = {**EXPENSE, "input_amount": "9999.00"}

    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            tampered,
            headers,
        )
    )
    assert result.is_error is True
    assert (
        json.loads(result.content[0].text)["error"]["code"]
        == ErrorCode.HOST_CONTEXT_MISMATCH.value
    )
    assert execution_count(finance_session) == 0


def test_host_only_arguments_are_rejected_before_the_handler(
    finance_session, caller
) -> None:
    registry = registry_with_write_probe(finance_session)
    headers = {
        key.lower(): value
        for key, value in caller.headers("finance.log_expense", EXPENSE).items()
    }
    forged = {**EXPENSE, "duplicate_override": {"approved": True}}
    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            forged,
            headers,
        )
    )
    assert result.is_error is True
    assert (
        json.loads(result.content[0].text)["error"]["code"]
        == ErrorCode.HOST_CONTEXT_MISMATCH.value
    )
    assert execution_count(finance_session) == 0


def test_signed_but_schema_invalid_arguments_are_rejected(
    finance_session, caller
) -> None:
    registry = registry_with_write_probe(finance_session)
    invalid = {"name": "missing required fields"}
    headers = {
        key.lower(): value
        for key, value in caller.headers(
            "finance.log_expense", invalid
        ).items()
    }
    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            invalid,
            headers,
        )
    )
    assert result.is_error is True
    assert (
        json.loads(result.content[0].text)["error"]["code"]
        == ErrorCode.INVALID_ARGUMENT.value
    )
    assert execution_count(finance_session) == 0


def test_a_verified_call_runs_the_handler_and_creates_one_row(
    finance_session, caller
) -> None:
    registry = registry_with_write_probe(finance_session)
    expected_key = str(uuid.uuid4())
    host = caller.host_context(
        "finance.log_expense", idempotency_key=expected_key
    )
    headers = {
        k.lower(): v
        for k, v in caller.headers(
            "finance.log_expense", EXPENSE, host=host
        ).items()
    }

    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            EXPENSE,
            headers,
        )
    )
    assert result.is_error is False
    assert result.structured_content["status"] == "created"
    # The gate passed, so the handler ran exactly once.
    assert execution_count(finance_session) == 1
    assert finance_session.get(ToolExecution, expected_key) is not None


# --- binding details, without a database -------------------------------------


def test_a_token_bound_to_a_different_tool_is_refused(caller) -> None:
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))
    # Sign for meta.capabilities, then present the headers on a different tool.
    headers = {
        k.lower(): v
        for k, v in caller.headers("meta.capabilities", {}).items()
    }
    # Call finance.log_expense with meta's headers. finance.log_expense has no
    # handler in this registry, so it is refused as not-allowlisted before the
    # binding is even checked -- which is the correct order.
    result = run(
        dispatch(registry, caller.authorizer(), "finance.log_expense", {}, headers)
    )
    assert (
        json.loads(result.content[0].text)["error"]["code"]
        == ErrorCode.TOOL_NOT_ALLOWLISTED.value
    )


def test_a_meta_call_with_a_valid_context_succeeds(caller) -> None:
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))
    headers = {
        k.lower(): v
        for k, v in caller.headers("meta.capabilities", {}).items()
    }
    result = run(
        dispatch(registry, caller.authorizer(), "meta.capabilities", {}, headers)
    )
    assert result.is_error is False
    assert result.structured_content["status"] == "ok"


def test_a_stale_allowed_tools_version_is_refused(caller) -> None:
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))
    stale = replace(
        caller.host_context("meta.capabilities"),
        allowed_tools_version="stale-version",
    )
    headers = {
        key.lower(): value
        for key, value in caller.headers(
            "meta.capabilities", {}, host=stale
        ).items()
    }
    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "meta.capabilities",
            {},
            headers,
        )
    )
    assert result.is_error is True
    assert (
        json.loads(result.content[0].text)["error"]["code"]
        == ErrorCode.SCOPE_DENIED.value
    )
