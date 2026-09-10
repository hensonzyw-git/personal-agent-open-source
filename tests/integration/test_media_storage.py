"""The media tables enforce their invariants, not just describe them.

Multimodal design §5.1, §5.3 and §6. Every test here pins an invariant that the
*database* holds, so a future code path cannot talk its way past it. The ones
that matter most are the two that make the design's guarantees single-valued --
at most one `origin` binding per object and at most one `published` attempt per
object -- because both are stated as absolutes ("每对象唯一 origin", "每
media_id 至多一个被采纳的内容") and a code-level check would leave a race.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from personal_agent.storage import db
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    MEDIA_ATTEMPT_STATES,
    MEDIA_BINDING_ROLES,
    MEDIA_OBJECT_STATES,
    Conversation,
    ConversationEvent,
    ContextSession,
    Device,
    MediaAttempt,
    MediaBinding,
    MediaObject,
)


NOW = datetime(2026, 9, 10, 7, 0, tzinfo=timezone.utc)
SEALED = {
    "v": 1,
    "kid": "agent-data-2026-09",
    "nonce": "AAAAAAAAAAAAAAAA",
    "ciphertext": "AAAA",
    "tag": "AAAAAAAAAAAAAAAAAAAAAA",
}


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    with session_factory(engine)() as session:
        yield session


def make_device(device_id: str = "dev-1") -> Device:
    return Device(
        device_id=device_id,
        display_name="iPhone",
        public_key="BASE64URL",
        device_key_thumbprint="THUMB",
        status="active",
        scopes="[]",
        allowed_tools_version="v1",
        created_at=NOW,
    )


def make_event(event_id: str = "ev-1") -> tuple[Conversation, ContextSession, ConversationEvent]:
    """A device, canonical Timeline, open Session and one message."""
    conversation = Conversation(
        conversation_id="conv-1", created_at=NOW, next_sequence=2, is_canonical=True
    )
    context_session = ContextSession(
        session_id="sess-1",
        conversation_id="conv-1",
        status="open",
        relation_kind="new_topic",
        opened_at=NOW,
    )
    event = ConversationEvent(
        event_id=event_id,
        conversation_id="conv-1",
        timeline_sequence=1,
        session_id="sess-1",
        turn_id="turn-1",
        event_type="user_message",
        encrypted_content=SEALED,
        created_at=NOW,
    )
    return conversation, context_session, event


def make_object(media_id: str = "media-1", **overrides) -> MediaObject:
    fields = {
        "media_id": media_id,
        "device_id": "dev-1",
        "purpose": "chat_image",
        "retention_class": "timeline_media",
        "state": "ready",
        "state_version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return MediaObject(**fields)


def make_attempt(
    attempt_id: str = "attempt-1", media_id: str = "media-1", **overrides
) -> MediaAttempt:
    fields = {
        "attempt_id": attempt_id,
        "media_id": media_id,
        "attempt_number": 1,
        "state": "claimed",
        "state_version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return MediaAttempt(**fields)


def seed_message(session) -> None:
    session.add(make_device())
    conversation, context_session, event = make_event()
    session.add_all([conversation, context_session, event])
    session.flush()


# --- closed vocabularies ---------------------------------------------------


def test_object_states_are_a_closed_set(session) -> None:
    session.add(make_device())
    session.add(make_object(state="normalizing"))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("state", MEDIA_OBJECT_STATES)
def test_every_declared_object_state_is_representable(session, state: str) -> None:
    # The vocabulary in `models.py` and the CHECK constraint in the migration
    # are written twice; this is what keeps them from drifting apart.
    session.add(make_device())
    session.add(make_object(media_id="m", state=state))
    session.commit()


@pytest.mark.parametrize("state", MEDIA_ATTEMPT_STATES)
def test_every_declared_attempt_state_is_representable(session, state: str) -> None:
    session.add(make_device())
    session.add(make_object())
    session.add(
        make_attempt(state=state, encrypted_seal_record=SEALED)
    )
    session.commit()


def test_an_invented_object_state_is_refused(session) -> None:
    session.add(make_device())
    session.add(make_object(state="processing"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_purpose_outside_the_closed_set_is_refused(session) -> None:
    session.add(make_device())
    session.add(make_object(purpose="avatar"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_retention_outside_the_closed_set_is_refused(session) -> None:
    session.add(make_device())
    session.add(make_object(retention_class="forever"))
    with pytest.raises(IntegrityError):
        session.commit()


# --- the single-valued guarantees ------------------------------------------


def test_a_sealed_attempt_must_carry_its_seal_record(session) -> None:
    # Without the record there is nothing for recovery to compare against, so
    # the row would look adoptable while being unverifiable.
    session.add(make_device())
    session.add(make_object())
    session.add(make_attempt(state="sealed"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_an_abandoned_attempt_may_lack_a_seal_record(session) -> None:
    # A claim that never received a whole body is abandoned, not sealed, and
    # the cleanup path must still be able to record it.
    session.add(make_device())
    session.add(make_object())
    session.add(make_attempt(state="abandoned"))
    session.commit()


def test_only_one_attempt_per_object_may_be_published(session) -> None:
    session.add(make_device())
    session.add(make_object())
    session.add(
        make_attempt("a-1", attempt_number=1, state="published", encrypted_seal_record=SEALED)
    )
    session.commit()
    session.add(
        make_attempt("a-2", attempt_number=2, state="published", encrypted_seal_record=SEALED)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_attempt_numbers_are_unique_within_an_object(session) -> None:
    session.add(make_device())
    session.add(make_object())
    session.add(make_attempt("a-1", attempt_number=1))
    session.commit()
    session.add(make_attempt("a-2", attempt_number=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_objects_in_one_message_may_each_have_an_origin(session) -> None:
    # The uniqueness is per object, not per message: a message with two photos
    # is two origins, and both photos are owned by that same message.
    seed_message(session)
    session.add(make_object("media-1"))
    session.add(make_object("media-2"))
    session.add_all(
        [
            MediaBinding(
                binding_id="b-1",
                media_id="media-1",
                event_id="ev-1",
                role="origin",
                ordinal=0,
                created_at=NOW,
            ),
            MediaBinding(
                binding_id="b-2",
                media_id="media-2",
                event_id="ev-1",
                role="origin",
                ordinal=1,
                created_at=NOW,
            ),
        ]
    )
    session.commit()


def test_only_one_origin_binding_may_exist_per_object(session) -> None:
    # §6's fan-out has to know which message owns the object; a second origin
    # would make that question have two answers.
    seed_message(session)
    session.add(make_object())
    session.add(
        MediaBinding(
            binding_id="b-1",
            media_id="media-1",
            event_id="ev-1",
            role="origin",
            ordinal=0,
            created_at=NOW,
        )
    )
    session.commit()
    session.add(
        MediaBinding(
            binding_id="b-2",
            media_id="media-1",
            event_id="ev-1",
            role="reuse",
            ordinal=1,
            created_at=NOW,
        )
    )
    session.commit()
    session.add(
        MediaBinding(
            binding_id="b-3",
            media_id="media-1",
            event_id="ev-1",
            role="origin",
            ordinal=2,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_reuse_binding_may_repeat_for_the_same_object(session) -> None:
    seed_message(session)
    session.add(make_object())
    session.add_all(
        [
            MediaBinding(
                binding_id="b-1",
                media_id="media-1",
                event_id="ev-1",
                role="origin",
                ordinal=0,
                created_at=NOW,
            ),
            MediaBinding(
                binding_id="b-2",
                media_id="media-1",
                event_id="ev-1",
                role="reuse",
                ordinal=1,
                created_at=NOW,
            ),
        ]
    )
    session.commit()


def test_two_bindings_may_not_share_a_message_slot(session) -> None:
    seed_message(session)
    session.add(make_object("media-1"))
    session.add(make_object("media-2"))
    session.add(
        MediaBinding(
            binding_id="b-1",
            media_id="media-1",
            event_id="ev-1",
            role="origin",
            ordinal=0,
            created_at=NOW,
        )
    )
    session.commit()
    session.add(
        MediaBinding(
            binding_id="b-2",
            media_id="media-2",
            event_id="ev-1",
            role="origin",
            ordinal=0,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_an_invented_binding_role_is_refused(session) -> None:
    seed_message(session)
    session.add(make_object())
    session.add(
        MediaBinding(
            binding_id="b-1",
            media_id="media-1",
            event_id="ev-1",
            role="forwarded",
            ordinal=0,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("role", MEDIA_BINDING_ROLES)
def test_every_declared_binding_role_is_representable(session, role: str) -> None:
    seed_message(session)
    session.add(make_object())
    session.add(
        MediaBinding(
            binding_id="b-1",
            media_id="media-1",
            event_id="ev-1",
            role=role,
            ordinal=0,
            created_at=NOW,
        )
    )
    session.commit()


# --- deletion semantics ----------------------------------------------------


def test_deleting_a_message_removes_its_bindings_but_not_the_image(session) -> None:
    # §6: deleting a reuse message removes that use relation only. The persisted
    # image outlives every binding, which is why the FK to `media_objects` is
    # RESTRICT and this one cascades.
    seed_message(session)
    session.add(make_object())
    session.add(
        MediaBinding(
            binding_id="b-1",
            media_id="media-1",
            event_id="ev-1",
            role="origin",
            ordinal=0,
            created_at=NOW,
        )
    )
    session.commit()

    event = session.get(ConversationEvent, "ev-1")
    session.delete(event)
    session.commit()

    assert session.query(MediaBinding).count() == 0
    assert session.get(MediaObject, "media-1") is not None


def test_a_bound_object_may_not_be_deleted_out_from_under_its_bindings(session) -> None:
    # The fan-out is an explicit decision under the §6 lock, with a manifest
    # entry -- not a cascade a stray delete could trigger.
    seed_message(session)
    session.add(make_object())
    session.add(
        MediaBinding(
            binding_id="b-1",
            media_id="media-1",
            event_id="ev-1",
            role="origin",
            ordinal=0,
            created_at=NOW,
        )
    )
    session.commit()

    session.delete(session.get(MediaObject, "media-1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_an_object_takes_its_attempts_with_it(session) -> None:
    session.add(make_device())
    session.add(make_object())
    session.add(make_attempt())
    session.commit()

    session.delete(session.get(MediaObject, "media-1"))
    session.commit()

    assert session.query(MediaAttempt).count() == 0


# --- sealed columns and counters -------------------------------------------


def test_media_hashes_refuse_plaintext(session) -> None:
    # A digest of a photo is a fingerprint of that photo, so a column named
    # `encrypted_*` holding the literal value would be the leak, not the fix.
    session.add(make_device())
    session.add(
        make_object(encrypted_content_sha256="a" * 64)  # type: ignore[arg-type]
    )
    with pytest.raises((StatementError, ValueError)):
        session.commit()


def test_media_hashes_refuse_an_incomplete_envelope(session) -> None:
    session.add(make_device())
    session.add(
        make_object(encrypted_storage_ref={"v": 1, "ciphertext": "AAAA"})  # type: ignore[arg-type]
    )
    with pytest.raises((StatementError, ValueError)):
        session.commit()


def test_state_version_never_drops_below_one(session) -> None:
    session.add(make_device())
    session.add(make_object(state_version=0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_negative_sizes_are_refused(session) -> None:
    session.add(make_device())
    session.add(make_object(content_size=-1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_zero_declared_dimension_is_refused(session) -> None:
    # Width and height arrive from the client's declaration (§5.1 option-1
    # revision). Zero is not a small image, it is a missing value wearing a
    # number, and it would divide by zero in any aspect-ratio check.
    session.add(make_device())
    session.add(make_object(declared_width=0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_client_declared_dimensions_are_stored_as_declared(session) -> None:
    # Named `declared_*` on purpose: the server has no decoder, so these are
    # the client's numbers and nothing may re-label them a measurement.
    session.add(make_device())
    session.add(make_object(declared_width=1536, declared_height=2048))
    session.commit()

    stored = session.get(MediaObject, "media-1")
    assert (stored.declared_width, stored.declared_height) == (1536, 2048)


def test_an_unknown_device_is_refused(session) -> None:
    session.add(make_object())
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_claim_deadline_round_trips_as_utc(session) -> None:
    session.add(make_device())
    deadline = NOW + timedelta(minutes=15)
    session.add(make_object(claim_deadline=deadline, owner_token="owner-1"))
    session.commit()

    session.expire_all()
    stored = session.get(MediaObject, "media-1")
    assert stored.claim_deadline == deadline
    assert stored.claim_deadline.tzinfo is not None


# --- the migration ---------------------------------------------------------


def test_0009_adds_the_tables_to_a_populated_database(tmp_path: Path) -> None:
    """The constraints must exist in the database, not only in `__table_args__`.

    Recovery, the reaper and a restore replay all write through raw SQL, so a
    CHECK that lives only in the model is not a constraint. This also proves the
    migration is additive: rows written before it keep their values.
    """
    path = tmp_path / "media-migration.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine, "0008_session_automatic_boundaries")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', '2026-09-10T00:00:00Z')"
            )
        )

    db.upgrade(engine)

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM devices")
        ).scalar_one() == 1
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO media_objects (media_id, device_id, purpose, "
                    "retention_class, state, state_version, created_at, "
                    "updated_at) VALUES ('m', 'dev', 'chat_image', "
                    "'timeline_media', 'normalizing', 1, "
                    "'2026-09-10T00:00:00Z', '2026-09-10T00:00:00Z')"
                )
            )
    engine.dispose()


def test_0009_downgrade_refuses_to_destroy_media_rows(tmp_path: Path) -> None:
    # A downgrade that silently dropped the images would remove the user's
    # photos from the Timeline with no manifest entry and no tombstone.
    path = tmp_path / "media-downgrade.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', '2026-09-10T00:00:00Z')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO media_objects (media_id, device_id, purpose, "
                "retention_class, state, state_version, created_at, updated_at) "
                "VALUES ('m', 'dev', 'chat_image', 'timeline_media', 'pending', "
                "1, '2026-09-10T00:00:00Z', '2026-09-10T00:00:00Z')"
            )
        )

    with pytest.raises(RuntimeError):
        db.downgrade(engine, "0008_session_automatic_boundaries")
    engine.dispose()
