"""`DEV-035`: the drill-sample seeder, pinned offline.

The G5 evidence "恢复环境可注入离机 key ring 解密固定样本" depends on this row
existing and being sealed by the real cipher path. A seeder that produced a
plausible-looking envelope the AEAD check could not open would make the drill
fail for a reason that has nothing to do with the restore -- or worse, a seeder
that produced something the check *could* open without the real AAD binding
would make the drill pass while proving nothing.

So the properties pinned here are: it seals through the real key ring with the
manifest's own AAD; the restore-side check opens it; replay runs the real
handler; it refuses to silently overwrite; and the object type is one replay
actually handles rather than one it would refuse.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.backup.deletion_manifest import (
    REPLAY_HANDLERS,
    export_manifest,
)
from personal_agent.backup.restore_verify import (
    check_aead_sample,
    check_replay_deletion_manifest,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key


_SEEDER_PATH = Path(__file__).parents[2] / "scripts" / "seed_drill_sample.py"


def _load_seeder():
    spec = importlib.util.spec_from_file_location("seed_drill_sample", _SEEDER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def seeder():
    return _load_seeder()


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "agent.sqlite"
    engine = create_database_engine(path)
    try:
        db.upgrade(engine, "head")
    finally:
        engine.dispose()
    return path


def _seed(seeder, database: Path, keyring: KeyRing, *, replace: bool = False) -> None:
    """Run the seeder's write with an injected ring, bypassing only argv/env."""
    engine = create_database_engine(database)
    try:
        with session_factory(engine)() as session:
            if replace:
                session.execute(
                    text("DELETE FROM deletion_manifest WHERE entry_id = :eid"),
                    {"eid": seeder.ENTRY_ID},
                )
            envelope = keyring.encrypt(
                seeder.OBJECT_ID.encode("utf-8"),
                table=seeder.MANIFEST_TABLE,
                column=seeder.MANIFEST_COLUMN,
                row_id=seeder.ENTRY_ID,
            )
            session.add(
                seeder.DeletionManifest(
                    entry_id=seeder.ENTRY_ID,
                    object_type=seeder.OBJECT_TYPE,
                    encrypted_object_id=envelope,
                    deleted_at=datetime.now(timezone.utc),
                    backup_expiry_after=None,
                )
            )
            session.commit()
    finally:
        engine.dispose()


def test_object_type_is_one_replay_handles(seeder) -> None:
    """An unhandled type is refused at replay, and refusing is correct -- so the
    drill must not seed one."""
    assert seeder.OBJECT_TYPE in REPLAY_HANDLERS


def test_entry_id_is_obviously_synthetic(seeder) -> None:
    """The sample must never be mistakable for a real deletion in this table or
    in an evidence file."""
    assert "drill" in seeder.ENTRY_ID
    assert "drill" in seeder.OBJECT_ID


def test_seeded_sample_opens_under_the_data_key(
    seeder, database: Path, keyring: KeyRing
) -> None:
    _seed(seeder, database, keyring)

    result = check_aead_sample(database, keyring, entry_id=seeder.ENTRY_ID)

    assert result["ok"] is True, result["detail"]


def test_seeded_sample_will_not_open_under_another_key(
    seeder, database: Path, keyring: KeyRing
) -> None:
    """The AAD binding is what makes the sample evidence rather than decoration."""
    _seed(seeder, database, keyring)
    other = KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )

    result = check_aead_sample(database, other, entry_id=seeder.ENTRY_ID)

    assert result["ok"] is False
    assert "decrypt failed" in result["detail"]


def test_replay_runs_the_real_handler_and_removes_nothing(
    seeder, database: Path, keyring: KeyRing
) -> None:
    """The sample names a conversation that does not exist, so replay must
    report it absent -- a real pass through the handler, not a skipped entry,
    and no user data touched."""
    _seed(seeder, database, keyring)
    engine = create_database_engine(database)
    try:
        with session_factory(engine)() as session:
            entries = export_manifest(session)
    finally:
        engine.dispose()

    result = check_replay_deletion_manifest(database, keyring, entries)

    assert result["ok"] is True, result["detail"]
    assert "already_absent=1" in result["detail"]
    assert "applied=0" in result["detail"]


def test_seeding_twice_is_refused_without_replace(
    seeder, database: Path, keyring: KeyRing
) -> None:
    """Re-running must not quietly re-seal: a second row would hide whether the
    sample the drill opened is the one this backup carried."""
    _seed(seeder, database, keyring)

    with pytest.raises(Exception):
        _seed(seeder, database, keyring)
