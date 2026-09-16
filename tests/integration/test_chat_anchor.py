"""§3.2's first anchoring, against a real database and a real media store.

What is being tested is the *first* use of an image by a message: the read that
proves the requesting device may use it, and the write that records the use and
moves the object out of `ready`. The cases are the ways that can go wrong, in
the order the design names them -- an id that is not this device's, an object
that was not uploaded for chat, one that is not finished or no longer exists,
one whose measured type this deployment does not accept, one that would be a
second use without the lineage §5.1 requires, and one whose version moved
between the read and the write.

The objects are not fabricated: each one is produced by the real
create / `PUT` / complete path, so a case that depends on an object being
`ready` is depending on the same code production runs. Where a test changes a
column directly (the purpose, the measured type, the sealed digest) it says so
and says why -- those are the states a *server* defect or a later configuration
change produces, and there is no API that makes them.

The fakes are the parts the anchor must never reach: the interpreter, the
envelope builder, the dispatcher and the authorizer all raise if anything calls
them. Anchoring happens before any model work, and a test that let one of them
run would be proving something else.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from personal_agent.api import events
from personal_agent.api.app import (
    AgentApiDeps,
    AuthContext,
    _anchor_chat,
    _preflight_chat_replay,
)
from personal_agent.api.chat_anchor import (
    bind_chat_images,
    resolve_chat_images,
    resolved_parts,
)
from personal_agent.api.chat_parts import ImageRefPart, TextPart, parse_chat_parts
from personal_agent.api.operation_store import chat_request_fingerprint, open_operation
from personal_agent.api.request_payload import (
    ChatRequestPayload,
    describes_request,
    open_chat_request,
    seal_chat_request,
    with_clarification_question,
)
from personal_agent.context.session_manager import ResolvedClassification
from personal_agent.media.lifecycle import StaleMediaObjectError, content_sha256
from personal_agent.media.locking import ensure_lock_files
from personal_agent.media.store import MediaStore
from personal_agent.media.uploads import (
    MediaError,
    MediaGoneError,
    MediaLimits,
    MediaNotFoundError,
    MediaNotReadyError,
    UploadDeclaration,
    complete_upload,
    delete_media,
    receive_upload,
    start_upload,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent.storage.models import ApiRequest, ConversationEvent
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import to_rfc3339

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)

JPEG = b"\xff\xd8\xff"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

LIMITS = MediaLimits(
    max_content_bytes=4096,
    max_dimension=4096,
    allowed_mimes=frozenset({"image/jpeg"}),
    target_ttl=timedelta(minutes=30),
    claim_ttl=timedelta(minutes=10),
    retention_ttl=timedelta(days=7),
    image_pixels_per_token=750,
)

AUTH = AuthContext(
    device_id="dev", scopes=("finance.write",), allowed_tools_version="v1",
    client_wire_version=1,
)


def body(size: int = 16) -> bytes:
    return JPEG + b"\x00" * (size - len(JPEG))


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    db.upgrade(engine, "head")
    with engine.begin() as connection:
        for device_id in ("dev", "other"):
            connection.execute(
                text(
                    "INSERT INTO devices (device_id, display_name, public_key, "
                    "device_key_thumbprint, status, scopes, allowed_tools_version, "
                    "created_at) VALUES (:id, 'phone', 'key', :id, 'active', "
                    "'[]', 'v1', :now)"
                ),
                {"id": device_id, "now": to_rfc3339(NOW)},
            )
        # One open Session, so a binding has an event to hang on without every
        # test having to run the Session Manager first. The Timeline is the one
        # `0003` seeded: a second canonical Timeline cannot exist, and
        # `resolve_timeline` refuses any id that does not name it.
        connection.execute(
            text(
                "INSERT INTO context_sessions (session_id, conversation_id, "
                "status, relation_kind, opened_at) "
                "SELECT 's1', conversation_id, 'open', 'new_topic', :now "
                "FROM conversations WHERE is_canonical = 1"
            ),
            {"now": to_rfc3339(NOW)},
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def timeline(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT conversation_id FROM conversations WHERE is_canonical = 1")
        ).scalar_one()


@pytest.fixture()
def store(tmp_path: Path, keyring: KeyRing) -> MediaStore:
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine", "locks"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_lock_files(root)
    return MediaStore(root, keyring)


@pytest.fixture()
def session(engine):
    session = session_factory(engine)()
    yield session
    session.close()


def _publish(session, store, keyring, *, device_id="dev", key="key-1", content=None):
    """Drive one object through the real create / PUT / complete path."""
    payload = content if content is not None else body()
    declaration = UploadDeclaration(
        mime="image/jpeg",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        width=100,
        height=200,
    )
    created = start_upload(
        session,
        keyring=keyring,
        device_id=device_id,
        client_request_id=key,
        declaration=declaration,
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    receive_upload(
        session,
        store=store,
        keyring=keyring,
        media_id=created.media_id,
        device_id=device_id,
        body=payload,
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    complete_upload(
        session,
        store=store,
        keyring=keyring,
        media_id=created.media_id,
        device_id=device_id,
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    return created.media_id


def _resolve(session, keyring, parts):
    return resolve_chat_images(
        session,
        keyring=keyring,
        device_id="dev",
        parts=parts,
        limits=LIMITS,
    )


def _image_only(media_id: str):
    return parse_chat_parts([{"type": "image_ref", "media_id": media_id}])


# --- resolve: who may use which object ------------------------------------


def test_a_ready_image_resolves_to_the_servers_own_measured_digest(
    session, store, keyring
):
    """The digest comes off the sealed column, never from the request (§5.1)."""
    media_id = _publish(session, store, keyring)
    (image,) = _resolve(session, keyring, _image_only(media_id))
    assert image.media_id == media_id
    assert image.content_sha256 == hashlib.sha256(body()).hexdigest()
    assert image.state == "ready"
    assert image.ordinal == 0


def test_the_ordinal_is_the_position_in_the_ordered_parts(session, store, keyring):
    """Text first (§3.1), so an image beside text binds at slot 1."""
    media_id = _publish(session, store, keyring)
    parts = parse_chat_parts(
        [{"type": "text", "text": "这张账单记一下"}, {"type": "image_ref", "media_id": media_id}]
    )
    (image,) = _resolve(session, keyring, parts)
    assert image.ordinal == 1


def test_an_unknown_id_is_not_found(session, keyring):
    with pytest.raises(MediaNotFoundError):
        _resolve(session, keyring, _image_only("11111111-1111-4111-8111-111111111111"))


def test_another_devices_image_is_refused_with_the_same_answer(session, store, keyring):
    """§5.2's existence-hiding, on the message path as well as the endpoints."""
    media_id = _publish(session, store, keyring, device_id="other", key="key-other")
    with pytest.raises(MediaNotFoundError):
        _resolve(session, keyring, _image_only(media_id))


def test_no_object_can_be_created_for_another_purpose(session, store, keyring):
    """The anchor's purpose check is defence in depth, and the table is why.

    `media_objects.purpose` carries a `CHECK` constraint naming `chat_image` as
    the only value, so "an object uploaded for something other than chat" is not
    a state a database can hold -- including the day a second purpose is added,
    which has to widen the constraint and therefore meet this test. What the
    anchor's check defends against is that day's half-done migration, not a
    request a client can make.
    """
    media_id = _publish(session, store, keyring)
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE media_objects SET purpose = 'wardrobe' WHERE media_id = :id"),
            {"id": media_id},
        )
    session.rollback()


def test_an_object_that_has_not_finished_uploading_is_not_ready(session, store, keyring):
    payload = body()
    created = start_upload(
        session,
        keyring=keyring,
        device_id="dev",
        client_request_id="key-1",
        declaration=UploadDeclaration(
            mime="image/jpeg",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    receive_upload(
        session,
        store=store,
        keyring=keyring,
        media_id=created.media_id,
        device_id="dev",
        body=payload,
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    with pytest.raises(MediaNotReadyError):
        _resolve(session, keyring, _image_only(created.media_id))


def test_a_deleted_image_is_gone_not_missing(session, store, keyring):
    """§5.2's tombstone: the id is known and will never come back."""
    media_id = _publish(session, store, keyring)
    delete_media(
        session, keyring=keyring, media_id=media_id, device_id="dev", now=NOW
    )
    session.commit()
    with pytest.raises(MediaGoneError):
        _resolve(session, keyring, _image_only(media_id))


def test_a_rejected_image_is_gone(session, store, keyring):
    """Reached through the real probe: bytes that disagree with the declaration."""
    created = start_upload(
        session,
        keyring=keyring,
        device_id="dev",
        client_request_id="key-1",
        declaration=UploadDeclaration(mime="image/jpeg", size=len(PNG), sha256="0" * 64),
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    with pytest.raises(MediaError):
        receive_upload(
            session,
            store=store,
            keyring=keyring,
            media_id=created.media_id,
            device_id="dev",
            body=PNG,
            limits=LIMITS,
            now=NOW,
        )
    session.commit()
    with pytest.raises(MediaGoneError):
        _resolve(session, keyring, _image_only(created.media_id))


def test_a_type_this_deployment_no_longer_allows_is_refused(session, store, keyring):
    """The registry identifies; the deployment's allow-list permits (§5.1).

    Simulated by changing the measured column, which is what a narrowed
    allow-list finds on an object uploaded under a wider one.
    """
    media_id = _publish(session, store, keyring)
    session.execute(
        text("UPDATE media_objects SET actual_mime = 'image/png' WHERE media_id = :id"),
        {"id": media_id},
    )
    session.commit()
    with pytest.raises(MediaError):
        _resolve(session, keyring, _image_only(media_id))


def test_a_published_object_with_no_measured_digest_is_a_server_error(
    session, store, keyring
):
    """State and content disagree. Retrying cannot fix it and the client is not
    at fault, so it is reported as what it is rather than refused as a 4xx."""
    media_id = _publish(session, store, keyring)
    session.execute(
        text(
            "UPDATE media_objects SET encrypted_content_sha256 = NULL "
            "WHERE media_id = :id"
        ),
        {"id": media_id},
    )
    session.commit()
    with pytest.raises(AppError) as raised:
        _resolve(session, keyring, _image_only(media_id))
    assert raised.value.code is ErrorCode.INTERNAL_ERROR


def test_a_refused_image_refuses_the_whole_request(session, store, keyring):
    """§3.2 anchors every image or none -- there is no half-answered message."""
    good = _publish(session, store, keyring)
    parts = parse_chat_parts(
        [
            {"type": "text", "text": "看看这个"},
            {"type": "image_ref", "media_id": "11111111-1111-4111-8111-111111111111"},
        ]
    )
    with pytest.raises(MediaNotFoundError):
        _resolve(session, keyring, parts)
    # The image that *was* usable is untouched: resolution writes nothing.
    assert content_sha256(session, keyring=keyring, media_id=good) is not None


def test_a_text_only_parts_request_resolves_to_nothing(session, keyring):
    parts = parse_chat_parts([{"type": "text", "text": "午饭 45"}])
    assert _resolve(session, keyring, parts) == ()


# --- resolved_parts: the fingerprint's input ------------------------------


def test_resolved_parts_fill_in_the_measured_digest(session, store, keyring):
    media_id = _publish(session, store, keyring)
    parts = parse_chat_parts(
        [{"type": "text", "text": "记一下"}, {"type": "image_ref", "media_id": media_id}]
    )
    images = _resolve(session, keyring, parts)
    filled = resolved_parts(parts, images)
    assert filled[0] == TextPart("记一下")
    assert filled[1] == ImageRefPart(media_id, hashlib.sha256(body()).hexdigest())


def test_an_unresolved_image_cannot_be_fingerprinted():
    """The fingerprint's image input is a server measurement or nothing (§3.2)."""
    with pytest.raises(AppError) as raised:
        chat_request_fingerprint(
            conversation_id="c1",
            text="",
            parts=(ImageRefPart("media_1"),),
        )
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


# --- bind: recording the use ----------------------------------------------


def test_a_first_use_binds_the_object_and_records_an_origin(
    session, store, keyring, engine, timeline
):
    media_id = _publish(session, store, keyring)
    parts = parse_chat_parts(
        [{"type": "text", "text": "记一下"}, {"type": "image_ref", "media_id": media_id}]
    )
    images = _resolve(session, keyring, parts)
    event_id = _event(session, keyring, timeline)

    bind_chat_images(
        session,
        images=images,
        event_id=event_id,
        operation_id=None,
        reuse_lineage=(),
        now=NOW,
    )
    session.commit()

    assert _state(engine, media_id) == "bound"
    rows = _bindings(engine, media_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "origin"
    assert rows[0]["ordinal"] == 1
    assert rows[0]["event_id"] == event_id
    assert rows[0]["source_operation_id"] is None


def test_a_second_use_without_a_lineage_is_refused(
    session, store, keyring, engine, timeline
):
    """§5.1: an object has one origin, and a reuse needs a recorded use behind it."""
    media_id = _publish(session, store, keyring)
    parts = _image_only(media_id)
    images = _resolve(session, keyring, parts)
    bind_chat_images(
        session,
        images=images,
        event_id=_event(session, keyring, timeline),
        operation_id=None,
        reuse_lineage=(),
        now=NOW,
    )
    session.commit()

    again = _resolve(session, keyring, parts)
    with pytest.raises(MediaError):
        bind_chat_images(
            session,
            images=again,
            event_id=_event(session, keyring, timeline),
            operation_id=None,
            reuse_lineage=(),
            now=NOW,
        )
    session.rollback()
    # Nothing was written: the refusal is the whole request's, not the row's.
    assert _state(engine, media_id) == "bound"
    assert len(_bindings(engine, media_id)) == 1


def test_a_reuse_backed_by_a_recorded_use_is_accepted(
    session, store, keyring, engine, timeline
):
    """The clarification chain: the answer carries the image the question used."""
    media_id = _publish(session, store, keyring)
    parts = _image_only(media_id)
    source = _origin(session, keyring, timeline, media_id, parts)

    reuse = _resolve(session, keyring, parts)
    answer = _operation(session, "answer-request")
    event_id = _event(session, keyring, timeline)
    bind_chat_images(
        session,
        images=reuse,
        event_id=event_id,
        operation_id=answer,
        reuse_lineage=(source,),
        now=NOW,
    )
    session.commit()

    rows = _bindings(engine, media_id)
    assert [row["role"] for row in rows] == ["origin", "reuse"]
    assert rows[1]["source_operation_id"] == source
    assert rows[1]["operation_id"] == answer
    # A reuse is a use relation, not a second capture: the object stays bound.
    assert _state(engine, media_id) == "bound"


def test_a_reuse_naming_an_operation_that_never_used_the_image_is_refused(
    session, store, keyring, timeline
):
    """The lineage is checked against the recorded binding, not taken on trust."""
    media_id = _publish(session, store, keyring)
    parts = _image_only(media_id)
    _origin(session, keyring, timeline, media_id, parts)
    unrelated = _operation(session, "unrelated-request")

    reuse = _resolve(session, keyring, parts)
    with pytest.raises(MediaError):
        bind_chat_images(
            session,
            images=reuse,
            event_id=_event(session, keyring, timeline),
            operation_id=_operation(session, "answer-request"),
            reuse_lineage=(unrelated,),
            now=NOW,
        )


def test_the_origin_move_refuses_a_version_that_moved(session, store, keyring, timeline):
    """§3.2's "锁内行版本必须仍与预校验一致": a lost race refuses rather than
    binding an object another message has already taken."""
    media_id = _publish(session, store, keyring)
    parts = _image_only(media_id)
    images = _resolve(session, keyring, parts)
    _origin(session, keyring, timeline, media_id, parts)

    with pytest.raises(StaleMediaObjectError):
        bind_chat_images(
            session,
            images=images,
            event_id=_event(session, keyring, timeline),
            operation_id=_operation(session, "late-request"),
            reuse_lineage=(),
            now=NOW,
        )


def test_a_ready_object_is_an_origin_whatever_the_caller_declared(
    session, store, keyring, engine, timeline
):
    """The role follows the object's state, never the caller's claim.

    A `ready` object has never been used, so a declared lineage justifies
    nothing -- it is ignored rather than trusted.
    """
    media_id = _publish(session, store, keyring)
    parts = _image_only(media_id)
    unrelated = _operation(session, "unrelated-request")
    images = _resolve(session, keyring, parts)
    bind_chat_images(
        session,
        images=images,
        event_id=_event(session, keyring, timeline),
        operation_id=_operation(session, "answer-request"),
        reuse_lineage=(unrelated,),
        now=NOW,
    )
    session.commit()
    assert _bindings(engine, media_id)[0]["role"] == "origin"


# --- the sealed payload is what a replay is compared against ---------------


def test_a_sealed_payload_describes_the_parts_that_produced_it(keyring):
    parts = parse_chat_parts(
        [
            {"type": "text", "text": "这张账单记一下"},
            {"type": "image_ref", "media_id": "media_1"},
        ]
    )
    payload = ChatRequestPayload(
        conversation_id="c1", text="这张账单记一下", parts=parts
    )
    envelope = seal_chat_request(keyring, request_id="req_1", payload=payload)
    reopened = open_chat_request(keyring, request_id="req_1", envelope=envelope)

    assert describes_request(
        reopened,
        conversation_id="c1",
        text="这张账单记一下",
        parts=parts,
        clarification_of=None,
        start_new_session=False,
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"conversation_id": "c2"},
        {"text": "换个说法"},
        {"clarification_of": "op_1"},
        {"start_new_session": True},
    ],
)
def test_a_sealed_payload_stops_describing_a_changed_request(keyring, changed):
    parts = _image_only("media_1")
    payload = ChatRequestPayload(conversation_id="c1", text="", parts=parts)
    envelope = seal_chat_request(keyring, request_id="req_1", payload=payload)
    reopened = open_chat_request(keyring, request_id="req_1", envelope=envelope)

    wire = {
        "conversation_id": "c1",
        "text": "",
        "parts": parts,
        "clarification_of": None,
        "start_new_session": False,
    }
    wire.update(changed)
    assert not describes_request(reopened, **wire)


def test_a_different_image_is_a_changed_request(keyring):
    """The one thing the text cannot carry, and the reason parts are sealed."""
    payload = ChatRequestPayload(
        conversation_id="c1", text="", parts=_image_only("media_1")
    )
    envelope = seal_chat_request(keyring, request_id="req_1", payload=payload)
    reopened = open_chat_request(keyring, request_id="req_1", envelope=envelope)
    assert not describes_request(
        reopened,
        conversation_id="c1",
        text="",
        parts=_image_only("media_2"),
        clarification_of=None,
        start_new_session=False,
    )


def test_a_text_only_payload_never_describes_a_parts_request(keyring):
    """A key reused across the two shapes is a conflict, not a replay."""
    payload = ChatRequestPayload(conversation_id="c1", text="午饭 45")
    envelope = seal_chat_request(keyring, request_id="req_1", payload=payload)
    reopened = open_chat_request(keyring, request_id="req_1", envelope=envelope)
    assert not describes_request(
        reopened,
        conversation_id="c1",
        text="",
        parts=_image_only("media_1"),
        clarification_of=None,
        start_new_session=False,
    )


def test_an_absent_text_part_is_not_an_empty_one(keyring):
    """§3.1's one distinction the effective text cannot carry."""
    payload = ChatRequestPayload(
        conversation_id="c1", text="", parts=_image_only("media_1")
    )
    envelope = seal_chat_request(keyring, request_id="req_1", payload=payload)
    reopened = open_chat_request(keyring, request_id="req_1", envelope=envelope)
    with_empty_text = parse_chat_parts(
        [{"type": "text", "text": ""}, {"type": "image_ref", "media_id": "media_1"}]
    )
    assert not describes_request(
        reopened,
        conversation_id="c1",
        text="",
        parts=with_empty_text,
        clarification_of=None,
        start_new_session=False,
    )


def test_a_clarification_question_does_not_drop_the_parts(keyring):
    """§3.2 names `with_clarification_question` as where media gets lost."""
    parts = _image_only("media_1")
    payload = ChatRequestPayload(conversation_id="c1", text="", parts=parts)
    asked = with_clarification_question(payload, "是哪个分类？")
    envelope = seal_chat_request(keyring, request_id="req_1", payload=asked)
    reopened = open_chat_request(keyring, request_id="req_1", envelope=envelope)
    assert reopened.parts == parts
    assert reopened.clarification_question == "是哪个分类？"


# --- the anchor end to end, against the real chat path ---------------------


def _capability(enabled: bool):
    """§8's verdict, in the one position this module needs it in.

    A deployment cannot reach `enabled=True` today -- the scanner exemption is
    unapproved -- but the anchoring path still has to be exercised, and §5.1 is
    explicit that a branch nothing can reach is a branch nothing has tested. The
    switch is an *input* of the code under test here, not a stand-in for an
    external system: the tests below drive both positions and assert what each
    one does, and `test_the_anchor_refuses_a_photo_when_the_switch_is_closed`
    is the closed half of that pair.
    """
    from personal_agent.runtime.modality import ImageCapability

    return lambda: ImageCapability(
        enabled=enabled,
        closed_by=() if enabled else ("scanner_exemption",),
    )


def _deps(engine, keyring, store, *, images_enabled: bool = True):
    """The real `AgentApiDeps` with the media surface composed.

    Everything the anchor must not reach raises, so a test cannot pass by
    accidentally running the model path.
    """
    def forbidden(*args, **kwargs):
        raise AssertionError("anchoring must not build a model-side collaborator")

    return AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=None,  # unused: no HTTP layer in this test
        keyring=keyring,
        identifier_key=_hmac("identifier"),
        cursor_key=_hmac("cursor"),
        build_interpreter=forbidden,
        build_envelope=forbidden,
        build_dispatcher=forbidden,
        build_authorizer=forbidden,
        capabilities=forbidden,
        now=lambda: NOW,
        media_store=store,
        media_limits=LIMITS,
        image_capability=_capability(images_enabled),
    )


def _hmac(label: str):
    from personal_agent.keys import HmacKeyRing

    return HmacKeyRing(active=_hmac_key(label))


def _hmac_key(label: str):
    from personal_agent.keys import HmacKey

    return HmacKey(label, b"\x01" * 32)


def _anchor(deps, *, timeline, key, media_id, text="这张账单记一下", clarification_of=None):
    parts = parse_chat_parts(
        [{"type": "text", "text": text}, {"type": "image_ref", "media_id": media_id}]
    )
    return _anchor_chat(
        deps,
        AUTH,
        key,
        timeline,
        text,
        parts,
        clarification_of,
        False,
        ResolvedClassification(
            expected_session_id=None,
            expected_last_event_at=None,
            expected_timeline_sequence=0,
            outcome=None,
        ),
    )


def test_anchoring_a_photo_binds_it_seals_the_parts_and_answers(
    session, store, keyring, engine, timeline
):
    media_id = _publish(session, store, keyring)
    anchored = _anchor(
        _deps(engine, keyring, store), timeline=timeline, key="req-1", media_id=media_id
    )
    assert anchored.state == "accepted"

    with session_factory(engine)() as reopened:
        # Read the row back through the ORM, the way the clarification and retry
        # paths read it: the envelope is a JSON column, and a raw `SELECT` hands
        # back the string it is stored as rather than the mapping the seal wrote.
        row = reopened.execute(select(ApiRequest)).scalar_one()
        payload = open_chat_request(
            keyring, request_id=row.request_id, envelope=row.encrypted_request_payload
        )
    assert payload.parts == parse_chat_parts(
        [
            {"type": "text", "text": "这张账单记一下"},
            {"type": "image_ref", "media_id": media_id},
        ]
    )
    # §3.2's fingerprint input carries the measured digest and the sealed
    # structure does not: the two are different values on purpose, and the
    # fingerprint is the one the request can never be replayed against.
    assert payload.parts[1].content_sha256 is None

    assert _state(engine, media_id) == "bound"
    rows = _bindings(engine, media_id)
    assert len(rows) == 1 and rows[0]["role"] == "origin"


def test_the_anchor_refuses_a_photo_when_the_switch_is_closed(
    session, store, keyring, engine, timeline
):
    """§8's rule about a stale client cache, at the only point it can bite.

    A client that uploaded while the switch was open and re-sends after it
    closed still names a `ready` object, so "the bytes are already here" is not
    a reason to proceed. The refusal has to leave nothing behind: an object
    moved to `bound` and a binding row would be a use the deployment had
    already said no to, and undoing it afterwards is a different, weaker claim
    than never recording it.
    """
    media_id = _publish(session, store, keyring)
    deps = _deps(engine, keyring, store, images_enabled=False)

    with pytest.raises(AppError) as excinfo:
        _anchor(deps, timeline=timeline, key="req-1", media_id=media_id)

    assert excinfo.value.code is ErrorCode.UNSUPPORTED_OPERATION
    assert "scanner_exemption" in excinfo.value.internal_detail
    assert _state(engine, media_id) == "ready"
    assert _bindings(engine, media_id) == []
    with session_factory(engine)() as reopened:
        assert reopened.execute(select(ApiRequest)).scalars().all() == []
        assert (
            reopened.execute(
                select(ConversationEvent).where(
                    ConversationEvent.event_type == "user_message"
                )
            )
            .scalars()
            .all()
            == []
        )


def test_a_text_message_is_untouched_by_a_closed_switch(
    session, store, keyring, engine, timeline
):
    """§8: "文本模型路径不受图片关闭影响"."""
    deps = _deps(engine, keyring, store, images_enabled=False)
    parts = parse_chat_parts([{"type": "text", "text": "午饭 45"}])

    anchored = _anchor_chat(
        deps,
        AUTH,
        "req-1",
        timeline,
        "午饭 45",
        parts,
        None,
        False,
        ResolvedClassification(
            expected_session_id=None,
            expected_last_event_at=None,
            expected_timeline_sequence=0,
            outcome=None,
        ),
    )

    assert anchored.state == "accepted"


def test_a_replayed_photo_message_returns_the_same_operation(
    session, store, keyring, engine, timeline
):
    """The anchor path's own duplicate: the loser binds nothing."""
    media_id = _publish(session, store, keyring)
    deps = _deps(engine, keyring, store)

    first = _anchor(deps, timeline=timeline, key="req-1", media_id=media_id)
    second = _anchor(deps, timeline=timeline, key="req-1", media_id=media_id)
    assert second.operation_id == first.operation_id
    assert _state(engine, media_id) == "bound"
    assert len(_bindings(engine, media_id)) == 1


def test_a_replay_is_answered_without_touching_live_media(
    session, store, keyring, engine, timeline
):
    """§3.2's "不访问活媒体", as a property rather than as an intention.

    The image is deleted between the first send and the retry, which is exactly
    when a replay that re-read the media table would fail a request the server
    has already accepted and answered.
    """
    media_id = _publish(session, store, keyring)
    deps = _deps(engine, keyring, store)
    first = _anchor(deps, timeline=timeline, key="req-1", media_id=media_id)

    delete_media(session, keyring=keyring, media_id=media_id, device_id="dev", now=NOW)
    session.commit()

    parts = parse_chat_parts(
        [{"type": "text", "text": "这张账单记一下"}, {"type": "image_ref", "media_id": media_id}]
    )
    replayed = _preflight_chat_replay(
        deps, AUTH, "req-1", timeline, "这张账单记一下", parts, None, False
    )
    assert replayed is not None
    assert replayed.operation_id == first.operation_id


def test_the_same_key_with_a_different_image_conflicts(
    session, store, keyring, engine, timeline
):
    first_image = _publish(session, store, keyring, key="key-1")
    second_image = _publish(session, store, keyring, key="key-2")
    deps = _deps(engine, keyring, store)

    _anchor(deps, timeline=timeline, key="req-1", media_id=first_image)
    with pytest.raises(AppError) as raised:
        _anchor(deps, timeline=timeline, key="req-1", media_id=second_image)
    assert raised.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


# --- helpers --------------------------------------------------------------


def _event(session, keyring, timeline) -> str:
    """A user event to hang a binding on, with no operation behind it."""
    return events.append_event(
        session,
        keyring,
        conversation_id=timeline,
        session_id="s1",
        turn_id=events.new_turn_id(),
        event_type=events.USER_MESSAGE,
        content={"text": "记一下"},
        operation_id=None,
        now=NOW,
    )


def _operation(session, client_request_id: str) -> str:
    """A real operation, because the binding's columns are foreign keys.

    Anything less (a made-up id) would prove the check against a row that could
    not exist in a real database.
    """
    opened = open_operation(
        session,
        device_id="dev",
        client_request_id=client_request_id,
        request_fingerprint=f"fp-{client_request_id}",
        now=NOW,
    )
    session.flush()
    return opened.operation.operation_id


def _origin(
    session, keyring, timeline, media_id, parts, *, operation_key="src-request"
) -> str:
    """Record a first use under a real operation, for the reuse cases."""
    operation_id = _operation(session, operation_key)
    images = _resolve(session, keyring, parts)
    bind_chat_images(
        session,
        images=images,
        event_id=_event(session, keyring, timeline),
        operation_id=operation_id,
        reuse_lineage=(),
        now=NOW,
    )
    session.commit()
    assert _state(session.get_bind(), media_id) == "bound"
    return operation_id


def _state(engine, media_id) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT state FROM media_objects WHERE media_id = :id"),
            {"id": media_id},
        ).scalar_one()


def _bindings(engine, media_id) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT role, ordinal, event_id, operation_id, source_operation_id "
                "FROM media_bindings WHERE media_id = :id ORDER BY ordinal, role"
            ),
            {"id": media_id},
        )
        return [dict(row._mapping) for row in rows]


@pytest.mark.parametrize("caption", ["", "photo caption"])
def test_timeline_projects_existing_image_bindings(session, store, keyring, engine, timeline, caption):
    media_id = _publish(session, store, keyring)
    _anchor(_deps(engine, keyring, store), timeline=timeline, key="timeline-photo", media_id=media_id, text=caption)
    with session_factory(engine)() as reopened:
        page = events.read_page(reopened, keyring, _hmac("cursor"), conversation_id=timeline,
                                cursor=None, direction="older", limit=20)
        entry = next(e for e in page.entries if e.event_type == events.USER_MESSAGE)
        assert entry.content["text"] == caption
        assert entry.content["parts"] == [{"type": "image_ref", "media_id": media_id}]
        # Projection does not rewrite the archived message or change model history.
        original = events._entry(keyring, reopened.get(ConversationEvent, entry.event_id))
        assert "parts" not in original.content
        # A removed image retains its reference for the client's unavailable placeholder.
        delete_media(reopened, keyring=keyring, media_id=media_id, device_id="dev", now=NOW)
        reopened.commit()
        page = events.read_page(reopened, keyring, _hmac("cursor"), conversation_id=timeline,
                                cursor=None, direction="older", limit=20)
        assert next(e for e in page.entries if e.event_id == entry.event_id).content["parts"] == entry.content["parts"]
