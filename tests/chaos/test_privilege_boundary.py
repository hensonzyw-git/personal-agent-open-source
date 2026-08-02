"""DEV-038: the 零越权 gate — every layer refuses on its own.

Both interception layers are already covered, but in separate universes.
`tests/integration/test_registry_and_bridge.py` proves the Agent-side governed
bridge denies; `tests/integration/test_finance_mcp_authz.py` proves Finance
MCP's second gate denies. Neither answers the question DEV-038 actually asks:
**if one layer stopped working, would the next one still hold?**

That question matters because in production the Agent bridge runs first and
almost always refuses first, so the Finance gate is the layer nobody watches. A
bug that let one unauthorised call through the bridge would be invisible until
it also got past Finance — and the only reason it would not is a property no
test currently states.

So one named violation set is driven directly against Finance MCP with a
**validly signed** Host Context — the shape a compromised Agent process, or a
bug in the bridge, would actually produce. This is not a forgery test: the
signature is good, and Finance must still refuse because it re-derives
authorisation from the tool and arguments it actually received rather than
trusting that someone upstream checked.

Zero execution rows, not just an error code, is the measurement. An error code
says what was reported; the absence of a row says the handler never ran. One
positive control asserts a row is reachable at all, so the zero is evidence
rather than a broken probe.

Layer 1 is covered where the real object lives; see the note at the end of this
file for why it is not re-tested here.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

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


sys.path.insert(0, str(Path(__file__).parents[1] / "fixtures"))
from service_keys import SignedCaller  # noqa: E402


EXPENSE = {
    "name": "午饭",
    "input_amount": "45.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}
FULL_SCOPES = ("meta.capabilities.read", "finance.expense.write")
NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def finance_session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture()
def caller() -> SignedCaller:
    return SignedCaller(scopes=FULL_SCOPES)


def registry_with_probe(session) -> ToolRegistry:
    """A registry whose write handler leaves a row iff it actually ran."""
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))

    async def log_expense(invocation: ToolInvocation) -> dict:
        prepare_execution(
            session,
            idempotency_key=invocation.verified_call.idempotency_key,
            tool="finance.log_expense",
            request_fingerprint="fp",
            client_token=str(uuid.uuid4()),
            encrypted_payload=None,
            now=NOW,
        )
        session.commit()
        return {
            "status": "created",
            "record_id": "probe",
            "source_system": "test",
            "committed_at": "2026-07-23T15:00:00+08:00",
        }

    registry.register("finance.log_expense", log_expense)
    return registry


def execution_count(session) -> int:
    return len(list(session.scalars(select(ToolExecution))))


# --- the violation set, named once and used by both layers -------------------


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def out_of_scope(caller: SignedCaller):
    """A device without the write scope, asking for the write tool."""
    return (
        "finance.log_expense",
        EXPENSE,
        _lower(
            caller.headers(
                "finance.log_expense", EXPENSE, scopes=("meta.capabilities.read",)
            )
        ),
        ErrorCode.SCOPE_DENIED,
    )


def not_allowlisted(caller: SignedCaller):
    """A tool this build does not serve at all."""
    return (
        "finance.log_expense_batch",
        EXPENSE,
        _lower(caller.headers("finance.log_expense_batch", EXPENSE)),
        ErrorCode.TOOL_NOT_ALLOWLISTED,
    )


def stale_manifest(caller: SignedCaller):
    """A context signed against a tool catalog that has since changed."""
    stale = replace(
        caller.host_context("finance.log_expense", scopes=FULL_SCOPES),
        allowed_tools_version="stale-version",
    )
    # Deliberately `SCOPE_DENIED` rather than a distinct code (server/authz.py):
    # a caller must not be able to map this server's surface by comparing which
    # refusal it gets back.
    return (
        "finance.log_expense",
        EXPENSE,
        _lower(caller.headers("finance.log_expense", EXPENSE, host=stale)),
        ErrorCode.SCOPE_DENIED,
    )


def tampered_arguments(caller: SignedCaller):
    """Signed for one payload, sent with another."""
    return (
        "finance.log_expense",
        {**EXPENSE, "input_amount": "9999.00"},
        _lower(caller.headers("finance.log_expense", EXPENSE)),
        ErrorCode.HOST_CONTEXT_MISMATCH,
    )


def wrong_tool_binding(caller: SignedCaller):
    """A context minted for the read tool, replayed onto the write tool."""
    return (
        "finance.log_expense",
        EXPENSE,
        _lower(caller.headers("meta.capabilities", EXPENSE)),
        ErrorCode.HOST_CONTEXT_MISMATCH,
    )


def forged_host_fields(caller: SignedCaller):
    """The model smuggling Host-owned identity in as tool arguments."""
    arguments = {**EXPENSE, "device_id": "someone-elses-device"}
    return (
        "finance.log_expense",
        arguments,
        _lower(caller.headers("finance.log_expense", arguments)),
        ErrorCode.HOST_CONTEXT_MISMATCH,
    )


VIOLATIONS = [
    pytest.param(out_of_scope, id="out-of-scope"),
    pytest.param(not_allowlisted, id="not-allowlisted"),
    pytest.param(stale_manifest, id="stale-manifest-version"),
    pytest.param(tampered_arguments, id="tampered-arguments"),
    pytest.param(wrong_tool_binding, id="wrong-tool-binding"),
    pytest.param(forged_host_fields, id="forged-host-fields"),
]


# --- layer 2, exercised as if layer 1 were not there -------------------------


@pytest.mark.parametrize("violation", VIOLATIONS)
def test_finance_refuses_even_when_the_agent_bridge_is_bypassed(
    finance_session, caller, violation
) -> None:
    """The layer nobody watches must hold by itself.

    The Host Context here is validly signed, so this is not a forgery test: it
    is what a compromised Agent, or a bridge that let the call through, would
    send. Finance re-derives authorisation from the tool and arguments it
    actually received rather than trusting that someone upstream checked.
    """
    tool, arguments, headers, expected = violation(caller)
    registry = registry_with_probe(finance_session)

    result = run(dispatch(registry, caller.authorizer(), tool, arguments, headers))

    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"]["code"] == expected.value
    assert execution_count(finance_session) == 0, (
        "a refused call must leave no execution row: the row is what proves "
        "the handler ran"
    )


def test_the_probe_really_would_have_recorded_a_row(
    finance_session, caller
) -> None:
    """The zero-row assertion above is only evidence if a row is reachable.

    Without this, every test in the matrix would still pass if the probe were
    silently broken, and the suite would be measuring nothing at all.
    """
    registry = registry_with_probe(finance_session)
    headers = _lower(caller.headers("finance.log_expense", EXPENSE))

    result = run(
        dispatch(
            registry, caller.authorizer(), "finance.log_expense", EXPENSE, headers
        )
    )

    assert result.is_error is not True
    assert execution_count(finance_session) == 1


# --- layer 1 -----------------------------------------------------------------
#
# Deliberately not re-tested here. Layer 1 is the Agent-side `GovernedToolBridge`
# and it is covered against the real object in
# `tests/integration/test_registry_and_bridge.py`, whose
# `test_each_layer_of_the_intersection_can_deny_alone` is exactly the
# "denies on its own" property, and `test_an_invisible_tool_cannot_be_executed_
# by_guessing_its_name` the unauthorised-name case.
#
# A first draft of this file asserted layer 1 through the dispatcher's
# `FakeBridge`. That was worthless and worse than nothing: the fake performs no
# authorisation at all, so the assertions passed for a reason unrelated to the
# security property and would have kept passing if the real bridge stopped
# checking. §5.1 -- never let a fake be the only counterparty -- applies most
# sharply to a test that reads like a security guarantee.
