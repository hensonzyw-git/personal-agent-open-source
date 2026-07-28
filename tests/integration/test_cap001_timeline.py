"""CAP-001 slice C: one canonical Timeline, one order, opaque cursors.

Covers failure set F-C1..F-C11 (`docs/CAP-001失败集_v0.1.md` §1). The
concurrency and cursor tests are the point of the file: those are the two
places a client or a race can reach, and the properties there have to hold
against a real database and a real signature rather than against a fake that
shares the code's assumptions.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from envelope_factory import envelope_factory
from cap001_fixtures import CURSOR_KEY, IDENTIFIER_KEY
from personal_agent.api import events
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.orchestrator import DirectAnswer
from personal_agent.auth.tokens import (
    SigningKey,
    TokenKeyRing,
    issue_access_token,
)
from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    ContextSession,
    Conversation,
    ConversationAlias,
    ConversationEvent,
    Device,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode


NOW = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)
CANONICAL = "tl_canonical"


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def token_ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(
        active=SigningKey("tok-2026", private, private.public_key())
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="THUMB",
                status="active",
                scopes='["finance.write"]',
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.add(
            Conversation(
                conversation_id=CANONICAL,
                created_at=NOW,
                next_sequence=1,
                is_canonical=True,
            )
        )
        session.add(
            ContextSession(
                session_id="ses-1",
                conversation_id=CANONICAL,
                status="open",
                relation_kind="new_topic",
                opened_at=NOW,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture()
def db(engine):
    with session_factory(engine)() as session:
        yield session


def _seed(session, keyring: KeyRing, count: int) -> list[str]:
    ids = []
    for index in range(count):
        ids.append(
            events.append_event(
                session,
                keyring,
                conversation_id=CANONICAL,
                session_id="ses-1",
                turn_id=f"trn-{index}",
                event_type=events.USER_MESSAGE,
                content={"text": f"message {index}"},
                operation_id=None,
                now=NOW + timedelta(seconds=index),
            )
        )
    session.commit()
    return ids


# -- F-C1 / F-C2: one Timeline, and only one -------------------------------


def test_the_canonical_timeline_is_returned_not_recreated(db) -> None:
    assert events.canonical_timeline_id(db, now=NOW) == CANONICAL
    assert db.query(Conversation).count() == 1


def test_a_fresh_database_creates_exactly_one_canonical_timeline(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "empty.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        first = events.canonical_timeline_id(session, now=NOW)
        session.commit()
    with session_factory(engine)() as session:
        assert events.canonical_timeline_id(session, now=NOW) == first
        assert session.query(Conversation).count() == 1
    engine.dispose()


def test_unknown_conversation_id_never_creates_a_second_timeline(db) -> None:
    with pytest.raises(AppError) as excinfo:
        events.resolve_timeline(
            db,
            IDENTIFIER_KEY,
            client_conversation_id="someone-elses-id",
            now=NOW,
        )
    assert excinfo.value.code is ErrorCode.TIMELINE_MISMATCH
    assert db.query(Conversation).count() == 1


@pytest.mark.parametrize("bad", ["", None, 17])
def test_a_malformed_conversation_id_is_refused(db, bad: object) -> None:
    with pytest.raises(AppError) as excinfo:
        events.resolve_timeline(
            db, IDENTIFIER_KEY, client_conversation_id=bad, now=NOW  # type: ignore[arg-type]
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_a_legacy_alias_resolves_to_the_canonical_timeline(db) -> None:
    db.add(
        ConversationAlias(
            alias_hmac=events.timeline_alias_hmac(IDENTIFIER_KEY, "old-conv"),
            conversation_id=CANONICAL,
            created_at=NOW,
        )
    )
    db.commit()
    assert (
        events.resolve_timeline(
            db, IDENTIFIER_KEY, client_conversation_id="old-conv", now=NOW
        )
        == CANONICAL
    )
    # The alias table holds a digest, never the historical id itself.
    stored = db.query(ConversationAlias).one()
    assert "old-conv" not in stored.alias_hmac


def test_an_alias_made_with_another_key_does_not_resolve(db) -> None:
    other = HmacKey(kid="identifier:other", secret=b"\x99" * 32)
    db.add(
        ConversationAlias(
            alias_hmac=events.timeline_alias_hmac(other, "old-conv"),
            conversation_id=CANONICAL,
            created_at=NOW,
        )
    )
    db.commit()
    with pytest.raises(AppError) as excinfo:
        events.resolve_timeline(
            db, IDENTIFIER_KEY, client_conversation_id="old-conv", now=NOW
        )
    assert excinfo.value.code is ErrorCode.TIMELINE_MISMATCH


def test_an_alias_made_with_a_retired_key_stays_resolvable(db) -> None:
    retired = HmacKey(kid="identifier:retired", secret=b"\x98" * 32)
    rotated = HmacKeyRing(active=IDENTIFIER_KEY, previous=(retired,))
    db.add(
        ConversationAlias(
            alias_hmac=events.timeline_alias_hmac(retired, "old-conv"),
            conversation_id=CANONICAL,
            created_at=NOW,
        )
    )
    db.commit()
    assert (
        events.resolve_timeline(
            db, rotated, client_conversation_id="old-conv", now=NOW
        )
        == CANONICAL
    )


# -- F-C4: concurrent appends get unique, strictly increasing sequences ------


def test_concurrent_appends_produce_unique_increasing_sequences(
    engine, keyring: KeyRing
) -> None:
    # Two devices appending at once. Read-then-write would hand both the same
    # number; the conditional UPDATE is what makes this true.
    factory = session_factory(engine)
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def append(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            with factory() as session:
                events.append_event(
                    session,
                    keyring,
                    conversation_id=CANONICAL,
                    session_id="ses-1",
                    turn_id=f"trn-{index}",
                    event_type=events.USER_MESSAGE,
                    content={"text": f"concurrent {index}"},
                    operation_id=None,
                    now=NOW,
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    with factory() as session:
        sequences = sorted(
            row[0]
            for row in session.query(ConversationEvent.timeline_sequence).all()
        )
    assert sequences == [1, 2, 3, 4]


def test_the_sequence_uniqueness_is_a_database_constraint(
    db, keyring: KeyRing
) -> None:
    from sqlalchemy.exc import IntegrityError

    _seed(db, keyring, 1)
    db.add(
        ConversationEvent(
            event_id="evt-clash",
            conversation_id=CANONICAL,
            timeline_sequence=1,
            session_id="ses-1",
            turn_id="trn-x",
            event_type=events.USER_MESSAGE,
            encrypted_content=keyring.encrypt(
                b"{}",
                table="conversation_events",
                column="encrypted_content",
                row_id="evt-clash",
            ),
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# -- F-C10: pagination is complete, ordered and without duplicates ----------


def test_the_first_page_is_the_newest_and_reads_oldest_first(
    db, keyring: KeyRing
) -> None:
    _seed(db, keyring, 10)
    page = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=3,
    )
    assert [entry.content["text"] for entry in page.entries] == [
        "message 7",
        "message 8",
        "message 9",
    ]
    assert page.has_older is True
    assert page.has_newer is False
    assert page.older_cursor is not None


def test_full_pagination_is_complete_without_duplicates(
    db, keyring: KeyRing
) -> None:
    _seed(db, keyring, 17)
    seen: list[str] = []
    cursor = None
    while True:
        page = events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=cursor,
            direction="older",
            limit=5,
        )
        seen = [entry.content["text"] for entry in page.entries] + seen
        if not page.has_older:
            break
        cursor = page.older_cursor
        assert cursor is not None
    assert seen == [f"message {index}" for index in range(17)]
    assert len(set(seen)) == 17


def test_incremental_sync_reads_only_what_is_newer(db, keyring: KeyRing) -> None:
    _seed(db, keyring, 4)
    first = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=10,
    )
    _seed(db, keyring, 2)
    page = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=first.newer_cursor,
        direction="newer",
        limit=10,
    )
    assert [entry.content["text"] for entry in page.entries] == [
        "message 0",
        "message 1",
    ]
    assert page.has_newer is False


def test_an_empty_newer_page_reports_no_more_and_mints_no_cursor(
    db, keyring: KeyRing
) -> None:
    _seed(db, keyring, 2)
    first = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=10,
    )
    page = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=first.newer_cursor,
        direction="newer",
        limit=10,
    )
    assert page.entries == ()
    assert page.has_newer is False
    assert page.newer_cursor is None


# -- F-C5..F-C8: cursors are opaque, bound and directional ------------------


def test_a_tampered_cursor_is_refused(db, keyring: KeyRing) -> None:
    _seed(db, keyring, 6)
    page = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=2,
    )
    assert page.older_cursor is not None
    tampered = page.older_cursor[:-2] + ("AB" if page.older_cursor[-2:] != "AB" else "CD")
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=tampered,
            direction="older",
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


@pytest.mark.parametrize(
    "bad", ["", "not-a-cursor", "a.b", "....", "AAAA.BBBB"]
)
def test_a_malformed_cursor_is_refused(db, keyring: KeyRing, bad: str) -> None:
    _seed(db, keyring, 2)
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=bad,
            direction="older",
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


def test_a_cursor_signed_with_another_key_is_refused(
    db, keyring: KeyRing
) -> None:
    _seed(db, keyring, 4)
    forged = HmacKey(kid="cursor:forged", secret=b"\xaa" * 32)
    page = events.read_page(
        db,
        keyring,
        forged,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=2,
    )
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=page.older_cursor,
            direction="older",
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


def test_a_cursor_signed_with_a_retired_key_stays_usable(
    db, keyring: KeyRing
) -> None:
    _seed(db, keyring, 3)
    retired = HmacKey(kid="cursor:retired", secret=b"\xab" * 32)
    old_page = events.read_page(
        db,
        keyring,
        retired,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=2,
    )
    rotated = HmacKeyRing(active=CURSOR_KEY, previous=(retired,))
    page = events.read_page(
        db,
        keyring,
        rotated,
        conversation_id=CANONICAL,
        cursor=old_page.older_cursor,
        direction="older",
        limit=2,
    )
    assert len(page.entries) == 1


def test_a_cursor_from_another_timeline_is_refused(db, keyring: KeyRing) -> None:
    _seed(db, keyring, 4)
    page = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=2,
    )
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id="tl_somewhere_else",
            cursor=page.older_cursor,
            direction="older",
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


def test_a_cursor_used_in_the_wrong_direction_is_refused(
    db, keyring: KeyRing
) -> None:
    _seed(db, keyring, 6)
    page = events.read_page(
        db,
        keyring,
        CURSOR_KEY,
        conversation_id=CANONICAL,
        cursor=None,
        direction="older",
        limit=2,
    )
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=page.older_cursor,
            direction="newer",
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


def test_newer_without_a_cursor_is_refused(db, keyring: KeyRing) -> None:
    _seed(db, keyring, 2)
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=None,
            direction="newer",
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("direction", ["", "sideways", "OLDER"])
def test_an_unknown_direction_is_refused(
    db, keyring: KeyRing, direction: str
) -> None:
    with pytest.raises(AppError) as excinfo:
        events.read_page(
            db,
            keyring,
            CURSOR_KEY,
            conversation_id=CANONICAL,
            cursor=None,
            direction=direction,
            limit=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


# -- the HTTP surface -------------------------------------------------------


class _Answering:
    def interpret(self, *, envelope, clarification_context=None):
        return DirectAnswer("ok")


def _headers(token_ring, key: str) -> dict:
    token = issue_access_token(
        token_ring,
        device_id="dev-1",
        device_key_thumbprint="THUMB",
        scopes=["finance.write"],
        allowed_tools_version="v1",
        now=NOW,
    )
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


@pytest.fixture()
def client(engine, token_ring, keyring) -> TestClient:
    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        identifier_key=IDENTIFIER_KEY,
        cursor_key=CURSOR_KEY,
        build_interpreter=lambda auth: _Answering(),
        build_envelope=envelope_factory(keyring),
        build_dispatcher=lambda auth, trace: None,
        build_authorizer=lambda auth: (lambda *, tool, model_args: model_args),
        capabilities=lambda auth: [],
        now=lambda: NOW,
    )
    return TestClient(build_app(deps))


def test_capabilities_returns_the_canonical_timeline(client, token_ring) -> None:
    response = client.get(
        "/v1/capabilities", headers=_headers(token_ring, "k")
    )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == CANONICAL


def test_the_events_route_paginates_and_hides_internal_fields(
    client, token_ring, engine, keyring
) -> None:
    with session_factory(engine)() as session:
        _seed(session, keyring, 5)
    response = client.get(
        f"/v1/conversations/{CANONICAL}/events?limit=2",
        headers=_headers(token_ring, "k"),
    )
    assert response.status_code == 200
    body = response.json()
    assert [event["content"]["text"] for event in body["events"]] == [
        "message 3",
        "message 4",
    ]
    assert body["has_older"] is True
    assert body["older_cursor"]
    # F-C11: the ordering key, the Session and the sealed envelope are server
    # state and never reach the client.
    for event in body["events"]:
        assert set(event) == {
            "event_id",
            "event_type",
            "operation_id",
            "created_at",
            "content",
        }


def test_the_events_route_clamps_the_limit_down_never_up(
    client, token_ring, engine, keyring
) -> None:
    with session_factory(engine)() as session:
        _seed(session, keyring, 200)
    response = client.get(
        f"/v1/conversations/{CANONICAL}/events?limit=10000",
        headers=_headers(token_ring, "k"),
    )
    assert len(response.json()["events"]) == 100


def test_the_events_route_refuses_an_unknown_timeline(client, token_ring) -> None:
    response = client.get(
        "/v1/conversations/not-mine/events", headers=_headers(token_ring, "k")
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TIMELINE_MISMATCH"


def test_the_events_route_refuses_a_tampered_cursor(
    client, token_ring, engine, keyring
) -> None:
    with session_factory(engine)() as session:
        _seed(session, keyring, 5)
    response = client.get(
        f"/v1/conversations/{CANONICAL}/events?cursor=forged.signature",
        headers=_headers(token_ring, "k"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("limit", ["0", "-3", "abc"])
def test_the_events_route_refuses_a_malformed_limit(
    client, token_ring, limit: str
) -> None:
    response = client.get(
        f"/v1/conversations/{CANONICAL}/events?limit={limit}",
        headers=_headers(token_ring, "k"),
    )
    assert response.status_code == 400
