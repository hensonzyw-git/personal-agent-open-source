"""§6's authorized read, against a real database, a real store and a real lock.

The design names the defect this file exists for: "关闭 v0.5「已授权但尚未读，文
件先被删」的窗口". A message's use of an image is decided and committed at
anchoring time; the bytes come off the disk later, when the model is about to be
asked. In between, the object can be deleted. A read that trusted the earlier
decision would send a photo the user has already withdrawn.

So the interesting cases here are not "does it return the bytes" -- that is one
test -- but the ones where the answer changed between the two moments:

- the object was deleted after the anchor committed, by a *different* session;
- the file itself is already gone when the read asks for it;
- the delivery's allow-list no longer permits the measured type;
- the seal that establishes the bytes are complete is not there;
- the stripe is held by somebody else.

The second case in that list is the sharpest, and it is why the tests do not
stop at "the right exception came out". Where the read must refuse, the test
usually makes the read *impossible* -- unlinking the final file, removing the
seal -- so that an implementation which refused late would crash instead of
passing. Refusing and then not reading is the property; a refusal that still
opened the container would satisfy a weaker test.

The objects are produced by the real create / `PUT` / complete path and the use
relation by the real anchor writer, so a test that depends on an object being
`bound` depends on the same code production runs. Most cases call the read
directly, because that is the unit under test and it is where §6's ordering
lives; the last section drives the production assembly instead, so that "the
turn asks for the read at all" is asserted rather than assumed. There the
interpreter, dispatcher, authorizer and capability registry fail the test if
called: a turn that reached the model would be proving something else.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.api import events
from personal_agent.api.app import (
    AgentApiDeps,
    AuthContext,
    _Anchor,
    _context_factory,
)
from personal_agent.api.chat_anchor import bind_chat_images, resolve_chat_images
from personal_agent.api.chat_parts import parse_chat_parts
from personal_agent.api.media_read import read_authorized_images
from personal_agent.api.operation_store import open_operation
from personal_agent.api.request_payload import ChatRequestPayload
from personal_agent.media.lifecycle import MediaLifecycleError
from personal_agent.media.locking import ensure_lock_files, media_locks
from personal_agent.media.store import MediaStore
from personal_agent.media.uploads import (
    MediaBusyError,
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
from personal_agent.runtime.model_input import ImageInputPart
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import to_rfc3339

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)

JPEG = b"\xff\xd8\xff"

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
    device_id="dev", scopes=("finance.write",), allowed_tools_version="v1"
)

#: The rectangle `_publish` declares, and the bound it must produce:
#: ceil(100 * 200 / 750).
DECLARED_PIXELS = 100 * 200
DECLARED_TOKEN_BOUND = 27


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


def _publish(
    session,
    store,
    keyring,
    *,
    device_id="dev",
    key="key-1",
    content=None,
    width=100,
    height=200,
):
    """Drive one object through the real create / PUT / complete path."""
    payload = content if content is not None else body()
    declaration = UploadDeclaration(
        mime="image/jpeg",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        width=width,
        height=height,
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


def _resolve(session, keyring, media_id):
    return resolve_chat_images(
        session,
        keyring=keyring,
        device_id="dev",
        parts=parse_chat_parts([{"type": "image_ref", "media_id": media_id}]),
        limits=LIMITS,
    )


def _bind(session, keyring, timeline, media_id, *, operation_key="req-1"):
    """Record one real use of the image, through the anchor's own writer.

    Returns the pair §6 asks the read to be valid for -- the message that used
    the image and the operation it was used under. Both are real rows, so a
    test that passes them is passing what a turn would.
    """
    images = _resolve(session, keyring, media_id)
    operation_id = _operation(session, operation_key)
    event_id = events.append_event(
        session,
        keyring,
        conversation_id=timeline,
        session_id="s1",
        turn_id=events.new_turn_id(),
        event_type=events.USER_MESSAGE,
        content={"text": "这张账单记一下"},
        operation_id=None,
        now=NOW,
    )
    bind_chat_images(
        session,
        images=images,
        event_id=event_id,
        operation_id=operation_id,
        reuse_lineage=(),
        now=NOW,
    )
    session.commit()
    return event_id, operation_id


def _operation(session, client_request_id: str) -> str:
    opened = open_operation(
        session,
        device_id="dev",
        client_request_id=client_request_id,
        request_fingerprint=f"fp-{client_request_id}",
        now=NOW,
    )
    session.flush()
    return opened.operation.operation_id


def _read(session, store, keyring, *, event_id, operation_id, media_ids, limits=LIMITS):
    return read_authorized_images(
        session,
        store=store,
        keyring=keyring,
        limits=limits,
        event_id=event_id,
        operation_id=operation_id,
        media_ids=media_ids,
    )


def _state(engine, media_id) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT state FROM media_objects WHERE media_id = :id"),
            {"id": media_id},
        ).scalar_one()


# --- the happy path, once -------------------------------------------------


def test_the_read_returns_the_published_bytes_with_the_servers_own_digest(
    session, store, keyring, engine, timeline
):
    """§6's 认证解密并读完整图片, end to end.

    The digest on the part is the sealed column's, and `ImageInputPart` recomputes
    it over the bytes independently -- so a store that returned the wrong
    object's plaintext, or a truncated one, fails here rather than at the model.
    """
    payload = body(size=64)
    media_id = _publish(session, store, keyring, content=payload)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)

    (part,) = _read(
        session,
        store,
        keyring,
        event_id=event_id,
        operation_id=operation_id,
        media_ids=[media_id],
    )

    assert isinstance(part, ImageInputPart)
    assert part.data == payload
    assert part.mime_type == "image/jpeg"
    assert part.content_sha256 == hashlib.sha256(payload).hexdigest()


def test_the_bound_comes_from_the_declared_rectangle(
    session, store, keyring, engine, timeline
):
    """§8's conservative ceiling, priced where the declaration lives."""
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)
    (part,) = _read(
        session,
        store,
        keyring,
        event_id=event_id,
        operation_id=operation_id,
        media_ids=[media_id],
    )
    assert part.token_upper_bound == DECLARED_TOKEN_BOUND


def test_an_undeclared_rectangle_is_priced_at_the_deployments_ceiling(
    session, store, keyring, engine, timeline
):
    """The declaration is optional (§5.2), so a bound has to exist without it.

    `max_dimension` is the right figure rather than a guess: `create` refuses any
    larger declaration, so no object reaching this point can exceed it. Pricing
    the image at one token instead would be the one answer a *bound* may not
    give -- the turn would fit the budget on paper and exceed it in fact.
    """
    media_id = _publish(session, store, keyring, width=None, height=None)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)

    (part,) = _read(
        session,
        store,
        keyring,
        event_id=event_id,
        operation_id=operation_id,
        media_ids=[media_id],
    )

    ceiling = LIMITS.image_token_upper_bound(
        LIMITS.max_dimension, LIMITS.max_dimension
    )
    assert part.token_upper_bound == ceiling
    assert part.token_upper_bound > DECLARED_TOKEN_BOUND


def test_a_turn_with_no_image_never_enters_the_store(
    session, store, keyring, engine
):
    """§8: "文本模型路径不受图片关闭影响".

    The store is pointed at a directory that does not exist, so a read that took
    the storage lock would fail here. A text-only turn returns before it.
    """
    absent = MediaStore(store.roots.root / "elsewhere", keyring)
    assert (
        _read(
            session,
            absent,
            keyring,
            event_id="evt",
            operation_id="op",
            media_ids=[],
        )
        == ()
    )


# --- the authorization, re-asked under the lock ---------------------------


def test_an_image_bound_to_another_message_is_not_readable(
    session, store, keyring, engine, timeline
):
    """§6's "来源有效", as the question it is: may *this* message read it.

    The object is `bound` and readable in every other respect; what it lacks is a
    use relation naming the message asking for it. Reading anyway would send a
    photo this turn has no record of using.
    """
    media_id = _publish(session, store, keyring)
    _event_id, operation_id = _bind(session, keyring, timeline, media_id)
    other_event = events.append_event(
        session,
        keyring,
        conversation_id=timeline,
        session_id="s1",
        turn_id=events.new_turn_id(),
        event_type=events.USER_MESSAGE,
        content={"text": "另一条消息"},
        operation_id=None,
        now=NOW,
    )
    session.commit()

    with pytest.raises(MediaNotFoundError):
        _read(
            session,
            store,
            keyring,
            event_id=other_event,
            operation_id=operation_id,
            media_ids=[media_id],
        )


def test_an_image_bound_under_another_operation_is_not_readable(
    session, store, keyring, engine, timeline
):
    """The same question about the turn rather than the message.

    A retry is a new operation with a new id; the binding travels with the
    operation that recorded the use, so a turn that never bound the image cannot
    read through somebody else's binding.
    """
    media_id = _publish(session, store, keyring)
    event_id, _operation_id = _bind(session, keyring, timeline, media_id)
    other_operation = _operation(session, "req-2")
    session.commit()

    with pytest.raises(MediaNotFoundError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=other_operation,
            media_ids=[media_id],
        )


def test_a_reused_image_is_readable_by_the_message_that_reused_it(
    session, store, keyring, engine, timeline
):
    """§5.1's other use: a second message reusing a photo must still read it.

    The use relation is a different row -- role `reuse`, this message's event and
    this message's operation -- and it is the only difference. A read that
    demanded an `origin` binding would make reuse write-only: the message would
    be accepted, the binding recorded, and the model asked about an image whose
    bytes never arrived.
    """
    payload_bytes = body(size=48)
    media_id = _publish(session, store, keyring, content=payload_bytes)
    _origin_event, origin_operation = _bind(session, keyring, timeline, media_id)

    reused_event = events.append_event(
        session,
        keyring,
        conversation_id=timeline,
        session_id="s1",
        turn_id=events.new_turn_id(),
        event_type=events.USER_MESSAGE,
        content={"text": "刚才那张，再记一次"},
        operation_id=None,
        now=NOW,
    )
    reuse_operation = _operation(session, "req-2")
    bind_chat_images(
        session,
        images=_resolve(session, keyring, media_id),
        event_id=reused_event,
        operation_id=reuse_operation,
        reuse_lineage=(origin_operation,),
        now=NOW,
    )
    session.commit()

    (part,) = _read(
        session,
        store,
        keyring,
        event_id=reused_event,
        operation_id=reuse_operation,
        media_ids=[media_id],
    )
    assert part.data == payload_bytes
    assert part.content_sha256 == hashlib.sha256(payload_bytes).hexdigest()


# --- the window: what changed between the anchor and the read -------------


def test_a_deletion_committed_after_the_sessions_last_read_wins(
    session, store, keyring, engine, timeline
):
    """The window §6 exists to close, and the reason the read rolls back first.

    The turn reads the media table while assembling itself -- that read opens a
    transaction, and in WAL every later read inside it answers from the same
    snapshot. A deletion committed by another session afterwards is invisible
    from there. Without ending that transaction before the lock, the in-lock
    re-read would confirm the state as it was *before* the lock existed, which is
    precisely the "已授权但尚未读，文件先被删" failure.

    The final file is unlinked as well, so an implementation that read first and
    checked later cannot pass by accident: it would raise the container's error,
    not this one.
    """
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)

    # The assembly's own read, before the lock: the snapshot the read must not
    # be answered from.
    assert (
        session.execute(
            text("SELECT state FROM media_objects WHERE media_id = :id"),
            {"id": media_id},
        ).scalar_one()
        == "bound"
    )

    with session_factory(engine)() as other:
        delete_media(
            other, keyring=keyring, media_id=media_id, device_id="dev", now=NOW
        )
    assert _state(engine, media_id) == "deleting"
    store.final_path(media_id).unlink()

    with pytest.raises(MediaGoneError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=operation_id,
            media_ids=[media_id],
        )


def test_a_file_that_vanished_before_the_read_is_an_incident_not_a_deletion(
    session, store, keyring, engine, timeline
):
    """§5.3's row for a `ready` object whose file is missing: refuse and alert.

    Reached here without a state change, and that is the point: a deleted image
    is the user's decision and a terminal answer, while a `bound` object with no
    file is a storage defect. The two must not be reported as each other --
    `MediaGoneError` would tell the user they withdrew a photo they did not, and
    `MediaNotReadyError` would invite a retry that cannot work. The store's own
    signal for this is `ContainerError`, which is not a `MediaError` at all; the
    read translates it so the refusal is classified rather than a crash.
    """
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)
    store.final_path(media_id).unlink()

    with pytest.raises(MediaLifecycleError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=operation_id,
            media_ids=[media_id],
        )
    # The object is untouched: refusing is not the same as condemning, and §5.3
    # forbids treating this as recovery by generating another content.
    assert _state(engine, media_id) == "bound"


def test_bytes_that_do_not_authenticate_are_refused_before_they_are_sent(
    session, store, keyring, engine, timeline
):
    """The other half of §5.3's row: the file is there and is not the image.

    A flipped byte in the persisted container. The seal is what catches it, and
    the read must not hand unauthenticated bytes to the model just because the
    object's state says `bound`.
    """
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)
    path = store.final_path(media_id)
    corrupted = bytearray(path.read_bytes())
    corrupted[-1] ^= 0x01
    path.write_bytes(bytes(corrupted))

    with pytest.raises(MediaLifecycleError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=operation_id,
            media_ids=[media_id],
        )


def test_an_object_that_leaves_the_usable_states_is_refused(
    session, store, keyring, engine, timeline
):
    """A `deleting` row is refused, and the file is not opened to find out."""
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)
    delete_media(session, keyring=keyring, media_id=media_id, device_id="dev", now=NOW)
    store.final_path(media_id).unlink()

    with pytest.raises(MediaGoneError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=operation_id,
            media_ids=[media_id],
        )


def test_a_measured_type_the_deployment_no_longer_allows_is_refused(
    session, store, keyring, engine, timeline
):
    """An allow-list narrowed after the upload, re-asked under the lock.

    The object is untouched and the binding stands; only the deployment's own
    answer changed. This is the branch that reaches `INTERNAL_ERROR` at the wire
    rather than `INVALID_ARGUMENT` -- nothing about the request is wrong.
    """
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)
    narrowed = MediaLimits(
        max_content_bytes=LIMITS.max_content_bytes,
        max_dimension=LIMITS.max_dimension,
        allowed_mimes=frozenset({"image/png"}),
        target_ttl=LIMITS.target_ttl,
        claim_ttl=LIMITS.claim_ttl,
        retention_ttl=LIMITS.retention_ttl,
        image_pixels_per_token=LIMITS.image_pixels_per_token,
    )

    with pytest.raises(MediaLifecycleError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=operation_id,
            media_ids=[media_id],
            limits=narrowed,
        )


def test_an_object_whose_attempt_holds_no_seal_is_refused(
    session, store, keyring, engine, timeline
):
    """`bound` with nothing establishing completeness is a defect, not a read.

    `read_container` refuses an unsealed container for the same reason; refusing
    here first means the failure names the state instead of the reader's
    internals, and the bytes are never opened.

    The state is built by abandoning the attempt: `media_objects.
    current_attempt_number` deliberately has no foreign key to `media_attempts`
    (§5.3's attempt rows outlive the object's pointer to one), so an object whose
    pointer names an attempt that abandoned without sealing is a row pair a
    database can hold. No API produces it -- recovery abandons an attempt that
    never published -- and it is one of the two states `seal_record` returns
    `None` for.
    """
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE media_attempts SET state = 'abandoned', "
                "encrypted_seal_record = NULL WHERE media_id = :id"
            ),
            {"id": media_id},
        )

    with pytest.raises(MediaNotReadyError):
        _read(
            session,
            store,
            keyring,
            event_id=event_id,
            operation_id=operation_id,
            media_ids=[media_id],
        )


# --- the lock ------------------------------------------------------------


def test_a_held_stripe_refuses_the_read_instead_of_waiting(
    session, store, keyring, engine, timeline
):
    """§4.1's "每次等待有界", met by not waiting (§6's answer is "失败释放锁且不发送")."""
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)

    with media_locks(store.roots.root, [media_id]):
        with pytest.raises(MediaBusyError):
            _read(
                session,
                store,
                keyring,
                event_id=event_id,
                operation_id=operation_id,
                media_ids=[media_id],
            )


def test_the_stripe_is_free_again_after_a_refusal(session, store, keyring, engine):
    """§6's "失败释放锁". The lock is not held past the failure that ended the read.

    Asserted by taking it: a stripe still held by the failed read leaves this
    non-blocking acquisition as the one thing that can fail.
    """
    with pytest.raises(MediaNotFoundError):
        _read(
            session,
            store,
            keyring,
            event_id="evt",
            operation_id="op",
            media_ids=["11111111-1111-4111-8111-111111111111"],
        )

    with media_locks(
        store.roots.root, ["11111111-1111-4111-8111-111111111111"], blocking=False
    ):
        pass


# --- the turn's assembly, over the read ----------------------------------
#
# The read is only half of §6; the other half is that a turn asks for it at all,
# and at the moment the design names -- after the anchor, before the model. The
# tests below drive the production assembly through its own seam rather than
# asserting that the seam exists.


def _assembly_deps(engine, keyring, store, captured: dict, *, images_enabled=True):
    """`AgentApiDeps` whose envelope factory records what it was handed."""

    class _Envelope:
        pass

    def build_envelope(session, auth, **kwargs):
        captured.update(kwargs)
        return _Envelope()

    from personal_agent.runtime.modality import ImageCapability

    return AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=None,
        keyring=keyring,
        identifier_key=_hmac("identifier"),
        cursor_key=_hmac("cursor"),
        build_interpreter=lambda *a, **k: pytest.fail("no model work in this test"),
        build_envelope=build_envelope,
        build_dispatcher=lambda *a, **k: pytest.fail("no model work in this test"),
        build_authorizer=lambda *a, **k: pytest.fail("no model work in this test"),
        capabilities=lambda *a, **k: pytest.fail("no model work in this test"),
        now=lambda: NOW,
        media_store=store,
        media_limits=LIMITS,
        image_capability=lambda: ImageCapability(
            enabled=images_enabled,
            closed_by=() if images_enabled else ("scanner_exemption",),
        ),
    )


def _hmac(label: str):
    from personal_agent.keys import HmacKey, HmacKeyRing

    return HmacKeyRing(active=HmacKey(label, b"\x01" * 32))


def test_the_turn_assembly_hands_the_authorized_bytes_to_the_envelope(
    session, store, keyring, engine, timeline
):
    """§6's read reaches the builder, which is the only thing that sends it."""
    payload_bytes = body(size=32)
    media_id = _publish(session, store, keyring, content=payload_bytes)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)

    captured: dict = {}
    deps = _assembly_deps(engine, keyring, store, captured)
    payload = ChatRequestPayload(
        conversation_id=timeline,
        text="这张账单记一下",
        parts=parse_chat_parts(
            [
                {"type": "text", "text": "这张账单记一下"},
                {"type": "image_ref", "media_id": media_id},
            ]
        ),
    )
    turn = _context_factory(
        deps,
        AUTH,
        session,
        payload=payload,
        anchor=_Anchor(
            conversation_id=timeline,
            session_id="s1",
            turn_id="turn-1",
            event_id=event_id,
        ),
        operation_id=operation_id,
    )
    turn()

    (part,) = captured["input_parts"]
    assert part.data == payload_bytes
    assert part.content_sha256 == hashlib.sha256(payload_bytes).hexdigest()
    assert captured["current_event_id"] == event_id
    assert captured["user_text"] == "这张账单记一下"


def test_the_turn_assembly_refuses_when_the_switch_closed_after_the_anchor(
    session, store, keyring, engine, timeline
):
    """§8: a stale client cache is not a reason to proceed, even this late.

    The message was accepted while the switch was open and the image is `bound`.
    Re-asking is what makes "已上传" irrelevant to "may be sent".
    """
    media_id = _publish(session, store, keyring)
    event_id, operation_id = _bind(session, keyring, timeline, media_id)

    deps = _assembly_deps(engine, keyring, store, {}, images_enabled=False)
    payload = ChatRequestPayload(
        conversation_id=timeline,
        text="这张账单记一下",
        parts=parse_chat_parts(
            [{"type": "image_ref", "media_id": media_id}]
        ),
    )
    turn = _context_factory(
        deps,
        AUTH,
        session,
        payload=payload,
        anchor=_Anchor(
            conversation_id=timeline,
            session_id="s1",
            turn_id="turn-1",
            event_id=event_id,
        ),
        operation_id=operation_id,
    )

    with pytest.raises(AppError) as excinfo:
        turn()

    assert excinfo.value.code is ErrorCode.UNSUPPORTED_OPERATION
    assert "scanner_exemption" in excinfo.value.internal_detail


def test_a_text_turn_builds_with_no_parts_at_all(
    session, store, keyring, engine, timeline
):
    """The other half of §8's sentence: no image, no media surface, no refusal."""
    captured: dict = {}
    deps = _assembly_deps(engine, keyring, store, captured, images_enabled=False)
    payload = ChatRequestPayload(conversation_id=timeline, text="午饭 45")
    event_id = events.append_event(
        session,
        keyring,
        conversation_id=timeline,
        session_id="s1",
        turn_id=events.new_turn_id(),
        event_type=events.USER_MESSAGE,
        content={"text": "午饭 45"},
        operation_id=None,
        now=NOW,
    )
    session.commit()

    turn = _context_factory(
        deps,
        AUTH,
        session,
        payload=payload,
        anchor=_Anchor(
            conversation_id=timeline,
            session_id="s1",
            turn_id="turn-1",
            event_id=event_id,
        ),
        operation_id="op-none",
    )
    turn()

    assert captured["input_parts"] == ()
