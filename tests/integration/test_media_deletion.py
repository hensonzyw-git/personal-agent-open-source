"""Media deletion: the decision, its manifest entry, and the fan-out (design §6).

§6 splits a media deletion into two acts that must not be confused. Deciding it
marks the object ``deleting`` and writes a manifest entry **in the same
transaction**, so from that commit forward the object grants no new use and a
restore can tell it was removed. Physically removing the bytes is a later act,
under the lock, and only the reaper may call an object ``deleted``.

What these tests are for: the failure they guard against is silent resurrection.
A deletion that is applied but not recorded comes back the next time someone
restores the wrong backup, and nothing in the live system would ever notice --
the live system no longer has the data to compare against.

The fan-out asymmetry is the other half. Deleting the message that *originated*
an image destroys the image; deleting a message that merely *reused* it removes
that use relation and leaves the persisted image alone. Both directions are
pinned here, because only one of them is the "obvious" behaviour and a
regression would look natural either way.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.backup.deletion_manifest import (
    MANIFEST_COLUMN,
    MANIFEST_TABLE,
    export_manifest,
    replay_manifest,
)
from personal_agent.media.deletion import (
    mark_media_deleting,
    origin_media_ids_for_conversation,
    origin_media_ids_for_event,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def engine(tmp_path: Path):
    """A migrated database, built the way every other integration test builds one.

    Worth knowing before reading the assertions below: ``db.upgrade`` on this
    engine leaves ``PRAGMA foreign_keys`` OFF on the pooled connection it runs
    Alembic through, and the connections this fixture hands out are the same
    ones, so no ``ON DELETE CASCADE`` in the schema fires for any test in this
    file. That is a property of the fixture stack, not of the production
    database, and it is why the fan-out ordering test asserts what it measures
    rather than what the schema promises.
    """
    engine = create_database_engine(tmp_path / "agent.sqlite")
    db.upgrade(engine, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', :now)"
            ),
            {"now": to_rfc3339(NOW)},
        )
    yield engine
    engine.dispose()


def _add_message(
    engine, *, conversation_id: str, event_id: str, sequence: int = 1
) -> None:
    """Seed one message, creating its conversation on first use.

    Idempotent on the conversation so a test can put several messages in one
    conversation without the second insert colliding on the primary key; the
    caller supplies ``sequence`` because the timeline position is unique per
    conversation and is not something a helper should be choosing.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO conversations (conversation_id, "
                "created_at, next_sequence) VALUES (:cid, :now, 2)"
            ),
            {"cid": conversation_id, "now": to_rfc3339(NOW)},
        )
        session_id = f"sess-{conversation_id}"
        connection.execute(
            text(
                "INSERT OR IGNORE INTO context_sessions (session_id, "
                "conversation_id, status, relation_kind, opened_at) VALUES "
                "(:sid, :cid, 'open', 'new_topic', :now)"
            ),
            {"sid": session_id, "cid": conversation_id, "now": to_rfc3339(NOW)},
        )
        connection.execute(
            text(
                "INSERT INTO conversation_events (event_id, conversation_id, "
                "session_id, turn_id, event_type, encrypted_content, "
                "timeline_sequence, created_at) VALUES "
                "(:eid, :cid, :sid, :turn, 'user_message', :content, :seq, :now)"
            ),
            {
                "eid": event_id,
                "cid": conversation_id,
                "sid": session_id,
                "turn": f"turn-{event_id}",
                "content": json.dumps({"sealed": "not-read-by-these-tests"}),
                "seq": sequence,
                "now": to_rfc3339(NOW),
            },
        )


def _add_media(engine, *, media_id: str, state: str = "bound") -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO media_objects (media_id, device_id, purpose, "
                "retention_class, state, state_version, created_at, updated_at) "
                "VALUES (:mid, 'dev', 'chat_image', 'timeline_media', :state, "
                "1, :now, :now)"
            ),
            {"mid": media_id, "state": state, "now": to_rfc3339(NOW)},
        )


def _add_binding(
    engine,
    *,
    binding_id: str,
    media_id: str,
    event_id: str,
    role: str,
    ordinal: int = 0,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO media_bindings (binding_id, media_id, event_id, "
                "role, ordinal, created_at) VALUES "
                "(:bid, :mid, :eid, :role, :ord, :now)"
            ),
            {
                "bid": binding_id,
                "mid": media_id,
                "eid": event_id,
                "role": role,
                "ord": ordinal,
                "now": to_rfc3339(NOW),
            },
        )


def _media_state(engine, media_id: str) -> tuple[str, int, datetime | None]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT state, state_version, deleted_at FROM media_objects "
                "WHERE media_id = :mid"
            ),
            {"mid": media_id},
        ).one()


def _restore(engine) -> None:
    """Put the object back the way an older backup would have it.

    A replay is only ever run against a database that *lost* the deletion, so a
    test that replays without undoing first is asking a question no restore
    asks -- and it would report "already absent" for a reason that has nothing
    to do with replay.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE media_objects SET state = 'bound', state_version = 7, "
                "deleted_at = NULL WHERE media_id = 'media-1'"
            )
        )


def _manifest_rows(engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT entry_id, object_type FROM deletion_manifest")
            ).all()
        )


# --- the decision and its record -------------------------------------------


def test_deciding_a_deletion_records_it_in_the_same_transaction(
    engine, keyring: KeyRing
) -> None:
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        assert mark_media_deleting(
            session, media_id="media-1", keyring=keyring, now=NOW
        )
        session.commit()

    assert _media_state(engine, "media-1")[0] == "deleting"
    rows = _manifest_rows(engine)
    assert [row[1] for row in rows] == ["media_object"]


def test_the_decision_stamps_the_tombstone_time(engine, keyring: KeyRing) -> None:
    """§6's tombstone is a property of the decision, not of the reaper.

    "已删除图在事件显示墓碑" and "取消后续复用" both have to hold from the moment
    the user deletes, while the bytes are still on disk waiting for the reaper.
    """
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        mark_media_deleting(session, media_id="media-1", keyring=keyring, now=NOW)
        session.commit()

    state, version, deleted_at = _media_state(engine, "media-1")
    assert (state, version) == ("deleting", 2)
    assert deleted_at is not None


def test_the_recorded_entry_opens_back_to_the_object_it_names(
    engine, keyring: KeyRing
) -> None:
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        mark_media_deleting(session, media_id="media-1", keyring=keyring, now=NOW)
        session.commit()
        exported = export_manifest(session)

    assert [entry["object_type"] for entry in exported] == ["media_object"]
    entry = exported[0]
    opened = keyring.decrypt(
        dict(entry["encrypted_object_id"]),
        table=MANIFEST_TABLE,
        column=MANIFEST_COLUMN,
        row_id=entry["entry_id"],
    )
    assert opened.decode("utf-8") == "media-1"


def test_the_manifest_never_names_the_object_in_plaintext(
    engine, keyring: KeyRing
) -> None:
    """A manifest that named photos in plaintext would leak what was removed."""
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        mark_media_deleting(session, media_id="media-1", keyring=keyring, now=NOW)
        session.commit()
        exported = export_manifest(session)

    assert "media-1" not in json.dumps(exported, ensure_ascii=False)


def test_deciding_twice_records_one_entry(engine, keyring: KeyRing) -> None:
    """A retried delete and a replayed one must not double-record (§6).

    Two entries for one deletion would also mean two ``backup_expiry_after``
    values, and retention would have to expire both before either could go.
    """
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        assert mark_media_deleting(
            session, media_id="media-1", keyring=keyring, now=NOW
        )
        assert not mark_media_deleting(
            session, media_id="media-1", keyring=keyring, now=NOW
        )
        session.commit()

    assert len(_manifest_rows(engine)) == 1


def test_an_unknown_object_is_not_an_error(engine, keyring: KeyRing) -> None:
    """A re-replay and a delete of something already gone both land here."""
    with session_factory(engine)() as session:
        assert not mark_media_deleting(
            session, media_id="never-existed", keyring=keyring, now=NOW
        )
        session.commit()

    assert _manifest_rows(engine) == []


@pytest.mark.parametrize("state", ["expired", "rejected"])
def test_an_object_that_never_published_is_marked_without_an_entry(
    engine, keyring: KeyRing, state: str
) -> None:
    """There are no persisted bytes, so an entry would protect nothing.

    The row still becomes a tombstone: the id must never be reused, and a
    second delete must still be a no-op rather than re-deciding a dead object.
    """
    _add_media(engine, media_id="media-1", state=state)

    with session_factory(engine)() as session:
        assert mark_media_deleting(
            session, media_id="media-1", keyring=keyring, now=NOW
        )
        session.commit()

    assert _media_state(engine, "media-1")[0] == "deleting"
    assert _manifest_rows(engine) == []


def test_a_decision_another_writer_already_made_is_respected(
    engine, keyring: KeyRing
) -> None:
    """Two deletes racing end in one decision and one entry, not two.

    The second caller reads the object *after* the first committed, so it sees
    ``deleting`` and stops -- the same no-op a retry gets. Without that, the
    loser would re-mark an object somebody already owns and write a second
    manifest entry for a single deletion.

    This used to be written as a true read-then-write race across two
    connections, and it cannot be constructed on this engine: §5.2's second
    consequence is that a session which has read is refused the write once
    another session commits, so the second writer gets "database is locked"
    rather than reaching the compare-and-swap. What is reachable is the order
    below, where the first decision has already committed.
    """
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as first:
        assert mark_media_deleting(
            session=first, media_id="media-1", keyring=keyring, now=NOW
        )
        first.commit()

    with session_factory(engine)() as second:
        assert not mark_media_deleting(
            session=second, media_id="media-1", keyring=keyring, now=NOW
        )
        second.commit()

    state, version, deleted_at = _media_state(engine, "media-1")
    # Still v2: the loser did not stamp the tombstone a second time.
    assert (state, version) == ("deleting", 2)
    assert deleted_at is not None


def test_the_decision_never_downgrades_an_object_that_already_moved_on(
    engine, keyring: KeyRing
) -> None:
    """Every state that means "a deletion is already under way" is a no-op.

    ``reaping`` and ``deleted`` are in the set alongside ``deleting`` because
    the reaper owns the row from ``reaping`` onwards: a delete arriving then
    must not stamp a fresh ``deleted_at``, which would move the tombstone time
    the user was shown after the bytes had already started going.
    """
    for state in ("deleting", "reaping", "deleted"):
        _add_media(engine, media_id=f"media-{state}", state=state)
        with session_factory(engine)() as session:
            assert not mark_media_deleting(
                session=session,
                media_id=f"media-{state}",
                keyring=keyring,
                now=NOW,
            )
            session.commit()

    assert _manifest_rows(engine) == []


# --- the fan-out, and the asymmetry it must keep ----------------------------


def test_deleting_a_message_fans_out_to_the_image_it_originated(
    engine, keyring: KeyRing
) -> None:
    _add_message(engine, conversation_id="c1", event_id="evt-1")
    _add_media(engine, media_id="media-1")
    _add_binding(engine, binding_id="b1", media_id="media-1", event_id="evt-1", role="origin")

    with session_factory(engine)() as session:
        replay_manifest(
            session,
            [
                {
                    "entry_id": "m1",
                    "object_type": "conversation_event",
                    "encrypted_object_id": keyring.encrypt(
                        b"evt-1",
                        table=MANIFEST_TABLE,
                        column=MANIFEST_COLUMN,
                        row_id="m1",
                    ),
                }
            ],
            keyring,
        )

    assert _media_state(engine, "media-1")[0] == "deleting"


def test_deleting_a_message_that_only_reused_an_image_leaves_the_image_alone(
    engine, keyring: KeyRing
) -> None:
    """The asymmetry §6 names: a reuse is a relation, not ownership.

    Getting this wrong in either direction is invisible in a green suite -- the
    reuse case silently destroys a photo a second message still shows, and the
    origin case silently keeps one the user deleted.
    """
    _add_message(engine, conversation_id="c1", event_id="evt-origin", sequence=1)
    _add_message(engine, conversation_id="c1", event_id="evt-reuse", sequence=2)
    _add_media(engine, media_id="media-1")
    _add_binding(
        engine,
        binding_id="b1",
        media_id="media-1",
        event_id="evt-origin",
        role="origin",
    )
    _add_binding(
        engine, binding_id="b2", media_id="media-1", event_id="evt-reuse", role="reuse"
    )

    with session_factory(engine)() as session:
        replay_manifest(
            session,
            [
                {
                    "entry_id": "m1",
                    "object_type": "conversation_event",
                    "encrypted_object_id": keyring.encrypt(
                        b"evt-reuse",
                        table=MANIFEST_TABLE,
                        column=MANIFEST_COLUMN,
                        row_id="m1",
                    ),
                }
            ],
            keyring,
        )

    assert _media_state(engine, "media-1")[0] == "bound"


def test_deleting_a_conversation_fans_out_to_its_images(
    engine, keyring: KeyRing
) -> None:
    _add_message(engine, conversation_id="c1", event_id="evt-1")
    _add_media(engine, media_id="media-1")
    _add_media(engine, media_id="media-2")
    # Two images on one message, so the two bindings have to claim distinct
    # ordinals -- which is also the schema saying a message's parts are ordered.
    _add_binding(
        engine,
        binding_id="b1",
        media_id="media-1",
        event_id="evt-1",
        role="origin",
        ordinal=0,
    )
    _add_binding(
        engine,
        binding_id="b2",
        media_id="media-2",
        event_id="evt-1",
        role="origin",
        ordinal=1,
    )

    with session_factory(engine)() as session:
        summary = replay_manifest(
            session,
            [
                {
                    "entry_id": "m1",
                    "object_type": "conversation",
                    "encrypted_object_id": keyring.encrypt(
                        b"c1",
                        table=MANIFEST_TABLE,
                        column=MANIFEST_COLUMN,
                        row_id="m1",
                    ),
                }
            ],
            keyring,
        )

    assert summary["applied"] == 1
    assert _media_state(engine, "media-1")[0] == "deleting"
    assert _media_state(engine, "media-2")[0] == "deleting"


def test_the_fan_out_finds_the_images_before_the_deleting_message_goes(
    engine, keyring: KeyRing
) -> None:
    """The order inside the handler, measured rather than assumed.

    ``media_bindings.event_id`` carries ``ON DELETE CASCADE``, so the bindings
    that name a message's images go away with the message -- but only when
    ``PRAGMA foreign_keys`` is on for the connection, which it is not after
    ``db.upgrade`` has run on this engine (see the note in this file's fixture).
    The measurement below pins the current truth, because the handler's order
    is only *provably* necessary under the other setting:

    - the traversal answers while the message exists,
    - the media is marked, so the fan-out ran,
    - the binding outlives the message here, which is the thing that would make
      a reordered fan-out look harmless in this environment and destructive in
      one where the cascade does fire.
    """
    _add_message(engine, conversation_id="c1", event_id="evt-1")
    _add_media(engine, media_id="media-1")
    _add_binding(
        engine, binding_id="b1", media_id="media-1", event_id="evt-1", role="origin"
    )

    with session_factory(engine)() as session:
        assert origin_media_ids_for_event(session, event_id="evt-1") == ["media-1"]

    with session_factory(engine)() as session:
        replay_manifest(
            session,
            [
                {
                    "entry_id": "m1",
                    "object_type": "conversation_event",
                    "encrypted_object_id": keyring.encrypt(
                        b"evt-1",
                        table=MANIFEST_TABLE,
                        column=MANIFEST_COLUMN,
                        row_id="m1",
                    ),
                }
            ],
            keyring,
        )

    assert _media_state(engine, "media-1")[0] == "deleting"
    with session_factory(engine)() as session:
        assert origin_media_ids_for_event(session, event_id="evt-1") == ["media-1"]


# --- replay ------------------------------------------------------------------


def test_replay_marks_media_without_recording_a_second_entry(
    engine, keyring: KeyRing
) -> None:
    """The entry being replayed is already the record of this deletion."""
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        mark_media_deleting(session, media_id="media-1", keyring=keyring, now=NOW)
        session.commit()
        exported = export_manifest(session)

    _restore(engine)

    with session_factory(engine)() as session:
        summary = replay_manifest(session, exported, keyring)

    assert summary == {"applied": 1, "already_absent": 0}
    assert _media_state(engine, "media-1")[0] == "deleting"
    assert len(_manifest_rows(engine)) == 1


def test_replaying_the_same_manifest_twice_reports_the_second_as_absent(
    engine, keyring: KeyRing
) -> None:
    """A re-replay is not an error, and must not look like one.

    ``already_absent`` is how a restore distinguishes "this deletion was already
    absorbed by the snapshot" from "this deletion failed"; a re-replay that
    raised would make the restore unrunnable.
    """
    _add_media(engine, media_id="media-1")

    with session_factory(engine)() as session:
        mark_media_deleting(session, media_id="media-1", keyring=keyring, now=NOW)
        session.commit()
        exported = export_manifest(session)

    _restore(engine)

    with session_factory(engine)() as session:
        first = replay_manifest(session, exported, keyring)
    with session_factory(engine)() as session:
        second = replay_manifest(session, exported, keyring)

    assert first == {"applied": 1, "already_absent": 0}
    assert second == {"applied": 0, "already_absent": 1}


def test_an_unknown_type_is_still_refused_with_media_in_the_vocabulary(
    engine, keyring: KeyRing
) -> None:
    """Adding a type must not soften the fail-closed rule for the others."""
    from personal_agent.backup.deletion_manifest import ManifestReplayError

    with session_factory(engine)() as session:
        with pytest.raises(ManifestReplayError, match="unknown object_type"):
            replay_manifest(
                session,
                [
                    {
                        "entry_id": "m1",
                        "object_type": "media_stripe",
                        "encrypted_object_id": keyring.encrypt(
                            b"x",
                            table=MANIFEST_TABLE,
                            column=MANIFEST_COLUMN,
                            row_id="m1",
                        ),
                    }
                ],
                keyring,
            )
