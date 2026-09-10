"""The calendar-name resolution read on the internal control plane.

Design 2.1 has the dispatcher resolve the model's calendar *name* to an EventKit
identifier before it issues an action. The directory lives in the MCP database
and the Agent API may not read it directly, while routing is a policy decision
the model must not be able to make -- so the lookup travels on the control
plane, next to the execution and duplicate reads, and never appears as an MCP
tool.

The interesting cases are the ones that must *not* resolve. A name that matches
nothing, a name that matches two accounts, and a name that matches only a
subscribed or read-only calendar are four different answers, because the user
has to be told four different things, and only one of them is a dead end.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fixtures.service_keys import SignedCaller
from personal_agent_core.control_token import (
    ControlAction,
    calendar_lookup_resource,
    sign_control_token,
)
from personal_data_mcp.server.app import build_app
from personal_data_mcp.server.config import ServerConfig
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import CalendarDirectory
from write_switch_fixtures import shared_enabled_write_switch


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _calendar(
    identifier: str,
    title: str,
    *,
    device_id: str = "device-1",
    source_title: str | None = "iCloud",
    allows_content_modifications: bool = True,
    is_subscribed: bool = False,
) -> CalendarDirectory:
    return CalendarDirectory(
        device_id=device_id,
        calendar_identifier=identifier,
        title=title,
        source_title=source_title,
        allows_content_modifications=allows_content_modifications,
        is_subscribed=is_subscribed,
        updated_at=NOW,
    )


@pytest.fixture()
def caller() -> SignedCaller:
    return SignedCaller()


@pytest.fixture()
def sf(tmp_path: Path):
    engine = create_database_engine(tmp_path / "calendar.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


@pytest.fixture()
def client(caller, sf):
    app = build_app(
        ServerConfig(),
        verification_ring=caller.ring,
        session_factory=sf,
        write_switch=shared_enabled_write_switch(),
    )
    transport = httpx.ASGITransport(app=app)

    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://control.local")

    return make


def _headers(
    caller, *, device_id: str = "device-1", title: str, action=ControlAction.RESOLVE_CALENDAR
) -> dict:
    token = sign_control_token(
        caller.ring,
        action=action,
        resource=calendar_lookup_resource(device_id, title),
    )
    return {"Authorization": f"Bearer {token}"}


def _resolve(client, caller, *, title, device_id="device-1"):
    async def scenario():
        async with client() as c:
            return await c.get(
                f"/internal/v1/calendars/{device_id}/resolve",
                params={"title": title},
                headers=_headers(caller, device_id=device_id, title=title),
            )

    return run(scenario())


def _seed(sf, *rows: CalendarDirectory) -> None:
    with sf() as session:
        session.add_all(list(rows))
        session.commit()


# --- the four answers --------------------------------------------------------


def test_a_unique_writable_name_resolves_to_its_identifier(caller, client, sf) -> None:
    _seed(
        sf,
        _calendar("uuid-ri-chang", "日常安排"),
        _calendar("uuid-chu-you", "出游计划"),
    )

    response = _resolve(client, caller, title="出游计划")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "resolved",
        "calendar_identifier": "uuid-chu-you",
        "title": "出游计划",
    }


def test_an_unknown_name_is_a_miss_not_an_error(caller, client, sf) -> None:
    """A calendar the phone knows nothing about is the answer to the question
    asked, not a failure of the read -- so it is a 200 with a status."""
    _seed(sf, _calendar("uuid-ri-chang", "日常安排"))

    response = _resolve(client, caller, title="健身")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "not_found"}


def test_a_device_that_has_never_synced_its_directory_says_so(
    caller, client, sf
) -> None:
    """Distinct from "no such calendar": the user who has not opened the app
    since this feature shipped needs a different sentence, and a title miss
    would send them looking for a calendar that may well exist."""
    response = _resolve(client, caller, title="出游计划")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "directory_empty"}


def test_a_name_on_two_accounts_offers_both(caller, client, sf) -> None:
    """Choosing one would write to a calendar the user did not name."""
    _seed(
        sf,
        _calendar("uuid-work", "日常安排", source_title="iCloud"),
        _calendar("uuid-home", "日常安排", source_title="Gmail"),
    )

    response = _resolve(client, caller, title="日常安排")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ambiguous"
    assert body["candidates"] == [
        {"title": "日常安排", "source_title": "Gmail"},
        {"title": "日常安排", "source_title": "iCloud"},
    ]
    # A candidate exists to be asked about, and a question names calendars
    # rather than identifiers: no EventKit UUID may leave on this path, or the
    # ambiguity resolution stops being the user's decision.
    assert "uuid-work" not in response.text
    assert "uuid-home" not in response.text


# --- writability is part of the match, not a post-filter ----------------------


def test_a_read_only_calendar_is_refused_with_its_reason(caller, client, sf) -> None:
    _seed(
        sf,
        _calendar(
            "uuid-holidays", "假期", allows_content_modifications=False
        ),
    )

    response = _resolve(client, caller, title="假期")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "read_only"


def test_a_subscribed_calendar_is_refused_rather_than_skipped(
    caller, client, sf
) -> None:
    """Design 2.4 keeps subscribed calendars in the directory precisely so the
    server can recognise them. Dropping them from the rows instead would report
    "no such calendar" for one the user is looking at."""
    _seed(sf, _calendar("uuid-feed", "球赛", is_subscribed=True))

    response = _resolve(client, caller, title="球赛")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "read_only"


def test_a_writable_twin_wins_over_a_subscribed_namesake(
    caller, client, sf
) -> None:
    """The refusal above must not become a veto: a name that also exists as a
    normal calendar is routable, and the subscribed one is simply not it."""
    _seed(
        sf,
        _calendar("uuid-feed", "球赛", is_subscribed=True),
        _calendar("uuid-mine", "球赛", source_title="iCloud"),
    )

    response = _resolve(client, caller, title="球赛")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    assert response.json()["calendar_identifier"] == "uuid-mine"


# --- per-device, and bound to the question asked -----------------------------


def test_another_devices_calendar_is_invisible(caller, client, sf) -> None:
    _seed(sf, _calendar("uuid-other", "出游计划", device_id="device-2"))

    response = _resolve(client, caller, title="出游计划", device_id="device-1")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "directory_empty"}


def test_a_token_for_another_title_is_refused(caller, client, sf) -> None:
    _seed(sf, _calendar("uuid-a", "日常安排"), _calendar("uuid-b", "出游计划"))

    async def scenario():
        async with client() as c:
            return await c.get(
                "/internal/v1/calendars/device-1/resolve",
                params={"title": "出游计划"},
                headers=_headers(caller, title="日常安排"),
            )

    response = run(scenario())
    assert response.status_code == 403


def test_a_token_for_another_device_is_refused(caller, client, sf) -> None:
    _seed(sf, _calendar("uuid-a", "日常安排"))

    async def scenario():
        async with client() as c:
            return await c.get(
                "/internal/v1/calendars/device-1/resolve",
                params={"title": "日常安排"},
                headers=_headers(caller, device_id="device-2", title="日常安排"),
            )

    response = run(scenario())
    assert response.status_code == 403


def test_the_lookup_resource_cannot_be_confused_across_its_two_parts() -> None:
    """The resource binds a device *and* a name. Joining them with a separator
    would make these two pairs the same resource, and one device's token could
    then be replayed against another's lookup."""
    assert calendar_lookup_resource("a", "b:c") != calendar_lookup_resource("a:b", "c")
    assert calendar_lookup_resource("dev-1", "出游计划") != calendar_lookup_resource(
        "dev-2", "出游计划"
    )


def test_the_lookup_is_not_a_model_visible_tool() -> None:
    """It must not be reachable through the tool surface the model sees: the
    model chooses a calendar *name*, and turning that into an identifier is
    policy. Asserted against the registry the server actually advertises, not
    the IR, so a future handler registration cannot quietly reintroduce it.
    """
    from personal_data_mcp.server.app import build_registry

    names = build_registry().names()
    assert not any("resolve" in name for name in names)
    assert not any("directory" in name for name in names)
