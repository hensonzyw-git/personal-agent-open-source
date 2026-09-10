"""The calendar tool at the MCP server's own boundary.

`calendar.create_event` is `executor="device"`, so its handler here is a
fail-closed guard and production never calls it — the host-field rule it is
signed under must still hold, because the rule is derived from the contract
and any tool whose schema declares a host-only name goes through the same
door.

The point of these tests is *where* the call is refused. A call that reaches
the device guard has already passed authentication, the host-field gate and
the input schema; one that stops earlier is refused by a different rule, and
conflating the two would hide a real regression behind a plausible code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fixtures.loopback_service import LoopbackFinanceService
from fixtures.service_keys import SignedCaller
from personal_agent.mcp_client.core import McpClientCore, StreamableHttpTransport
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import declared_model_fields
from personal_agent_core.manifest import load_manifest

CALENDAR_EVENT = {
    "title": "东京场网球",
    "start": "2027-01-02T15:00:00+09:00",
    "end": "2027-01-02T17:00:00+09:00",
    "all_day": False,
    "calendar": "演出&活动",
    "timezone": "Asia/Tokyo",
}


def declared_for(tool: str) -> frozenset[str]:
    entry = next(t for t in load_manifest()["tools"] if t["name"] == tool)
    return declared_model_fields(entry["model_input_schema"])


@pytest.fixture()
def calendar_server(tmp_path: Path):
    caller = SignedCaller(scopes=("calendar.event.write",))
    key_dir = tmp_path / "calendar-key"
    key_dir.mkdir()
    service = LoopbackFinanceService(
        caller.env(key_dir),
        database=tmp_path / "calendar.sqlite",
        calendar=True,
    )
    yield service, caller
    service.stop()


def call(service, tool: str, arguments: dict, headers: dict):
    async def scenario():
        async with McpClientCore(
            "calendar", StreamableHttpTransport(url=service.mcp_url)
        ) as client:
            return await client.call_tool(tool, arguments, host_context=headers)

    return asyncio.run(scenario())


def test_a_contract_declared_host_name_is_not_a_smuggled_host_field(
    calendar_server,
) -> None:
    """The event's timezone reaches the fail-closed device guard.

    `INTERNAL_ERROR` is the guard's own code, and it sits downstream of
    authentication, the host-field gate and the input schema — so this code is
    the evidence that none of those three refused the call. Every earlier
    stage has a different stable code (SCOPE_DENIED, HOST_CONTEXT_MISMATCH,
    INVALID_ARGUMENT), which the test below uses as its control.
    """
    service, caller = calendar_server
    headers = caller.headers(
        "calendar.create_event",
        CALENDAR_EVENT,
        declared=declared_for("calendar.create_event"),
    )
    with pytest.raises(AppError) as caught:
        call(service, "calendar.create_event", CALENDAR_EVENT, headers)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR


def test_the_two_sides_must_agree_on_the_exemption(calendar_server) -> None:
    """A signer that did not derive the exemption from the contract produces a
    hash the server cannot reproduce: the exemption is a contract fact, not a
    licence to send whatever the caller likes."""
    service, caller = calendar_server
    headers = caller.headers("calendar.create_event", CALENDAR_EVENT)
    with pytest.raises(AppError) as caught:
        call(service, "calendar.create_event", CALENDAR_EVENT, headers)
    assert caught.value.code is ErrorCode.HOST_CONTEXT_MISMATCH


def test_a_host_field_the_contract_does_not_declare_is_still_refused(
    calendar_server,
) -> None:
    """The exemption is exactly the declared set: a smuggled `device_id` is
    refused at the raw boundary even on the tool that declares `timezone`."""
    service, caller = calendar_server
    hostile = {**CALENDAR_EVENT, "device_id": "someone-elses-device"}
    headers = caller.headers(
        "calendar.create_event",
        hostile,
        declared=declared_for("calendar.create_event"),
    )
    with pytest.raises(AppError) as caught:
        call(service, "calendar.create_event", hostile, headers)
    assert caught.value.code is ErrorCode.HOST_CONTEXT_MISMATCH
