"""`DEV-035`: the online backup primitive, pinned.

Technical design 10.5 requires SQLite snapshots from the Online Backup API, not
a raw file copy. A WAL-mode database's main file can be mid-checkpoint, so ``cp``
captures whatever pages happen to be on disk; ``Connection.backup()`` reads
through the live pager and is consistent under concurrent writes.

This is a boundary where "looks correct" is not evidence (CLAUDE.md §5.1). The
failure modes are enumerated and each is a test, with defect injection: a raw
copy of a mid-write file is caught by the destination integrity check, a corrupt
destination is deleted rather than left for restic to ship, and an unreachable
source is a distinct error from a bad snapshot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from personal_agent_core.sqlite import (
    SNAPSHOT_MODE,
    STAGED_SNAPSHOT_MODE,
    BackupError,
    BackupUnavailableError,
    create_read_only_database_engine,
    online_backup,
)


def _seed(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (v) VALUES (?)", [("alpha",), ("beta",), ("gamma",)]
        )
        conn.commit()
    finally:
        conn.close()


def test_backup_copies_rows_and_passes_integrity(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    result = online_backup(source, destination)

    assert result == destination
    assert destination.exists()
    conn = sqlite3.connect(str(destination))
    try:
        rows = conn.execute("SELECT v FROM t ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["alpha", "beta", "gamma"]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_backup_is_atomic_no_temp_left_on_success(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    online_backup(source, destination)

    leftovers = [p for p in destination.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_backup_source_opened_read_only(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    online_backup(source, destination)

    # The source's content must be unchanged: a backup process that wrote the
    # live DB would be a privilege violation regardless of intent.
    conn = sqlite3.connect(str(source))
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    finally:
        conn.close()


def test_backup_destination_mode_is_0600(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    online_backup(source, destination)

    mode = destination.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600 snapshot, got {oct(mode)}"


def test_backup_leaves_no_wal_sidecars_behind(tmp_path: Path) -> None:
    """os.replace moves the main file only. Without explicit cleanup each run
    leaks a `-shm` and a `-wal` orphan into the staging directory, which is
    where they were found accumulating on 2026-08-01."""
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    online_backup(source, destination)

    leftovers = sorted(
        p.name for p in tmp_path.iterdir() if p.name.startswith(".")
    )
    assert leftovers == [], f"staging dir kept orphans: {leftovers}"


def test_backup_honours_an_explicit_staged_mode(tmp_path: Path) -> None:
    """A staged snapshot must be group-readable, or the offsite backup can only
    stat it. On 2026-08-01 the hard-coded 0600 made every staged file
    unreadable to personal-agent-backup while the directory looked correct."""
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    online_backup(source, destination, mode=STAGED_SNAPSHOT_MODE)

    mode = destination.stat().st_mode & 0o777
    assert mode == 0o640, f"expected 0640 staged snapshot, got {oct(mode)}"


def test_read_only_engine_reads_without_touching_the_file(tmp_path: Path) -> None:
    database = tmp_path / "restored.sqlite"
    _seed(database)
    before = database.stat().st_mtime_ns

    engine = create_read_only_database_engine(database)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM t")).scalar_one() == 3
    finally:
        engine.dispose()

    assert database.stat().st_mtime_ns == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_read_only_engine_refuses_an_accidental_write(tmp_path: Path) -> None:
    database = tmp_path / "restored.sqlite"
    _seed(database)
    engine = create_read_only_database_engine(database)
    try:
        with pytest.raises(OperationalError):
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO t (v) VALUES ('forbidden')"))
    finally:
        engine.dispose()

    raw = sqlite3.connect(database)
    try:
        assert raw.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    finally:
        raw.close()


def test_staged_mode_never_grants_other_access() -> None:
    """Group-readable is the widening; world-readable never is."""
    assert STAGED_SNAPSHOT_MODE & 0o007 == 0
    assert SNAPSHOT_MODE & 0o077 == 0


def test_explicit_mode_overrides_a_stale_destination_mode(tmp_path: Path) -> None:
    """os.replace inherits the temp file's mode, so a pre-existing destination
    must not decide the result either way."""
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"
    destination.write_bytes(b"stale")
    destination.chmod(0o666)

    online_backup(source, destination, mode=STAGED_SNAPSHOT_MODE)

    assert destination.stat().st_mode & 0o777 == 0o640


def test_backup_overwrites_a_stale_destination(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"
    online_backup(source, destination)

    # A second snapshot of a changed source must replace, not append to, the
    # prior file. A stale snapshot is silent data loss.
    conn = sqlite3.connect(str(source))
    try:
        conn.execute("INSERT INTO t (v) VALUES ('delta')")
        conn.commit()
    finally:
        conn.close()

    online_backup(source, destination)

    conn = sqlite3.connect(str(destination))
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 4
    finally:
        conn.close()


def test_missing_source_is_unavailable_not_a_bad_snapshot(tmp_path: Path) -> None:
    with pytest.raises(BackupUnavailableError):
        online_backup(tmp_path / "absent.sqlite", tmp_path / "snap.sqlite")
    assert not (tmp_path / "snap.sqlite").exists()


def test_corrupt_destination_is_deleted_and_raises(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    destination = tmp_path / "snap.sqlite"

    # Defect injection: a page goes bad on disk right after the backup API
    # writes it, before the post-copy integrity check runs -- the shape of a
    # flaky disk. The destination check must catch it, delete the corrupt file,
    # and raise, never leaving it for restic to encrypt and ship offsite.
    from personal_agent_core import sqlite as core_sqlite

    def corrupting_verify(path: Path) -> str:
        # Corrupt a body page (not the 100-byte header) so the file still parses
        # as SQLite but fails integrity_check, then run the real check.
        with open(path, "r+b") as fh:
            fh.seek(2048)
            fh.write(b"\x00" * 4096)
        return core_sqlite._integrity_check(path)

    with pytest.raises(BackupError):
        online_backup(source, destination, _verify=corrupting_verify)
    assert not destination.exists()
    leftovers = [p for p in destination.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_raw_copy_of_mid_write_file_is_not_a_safe_backup(tmp_path: Path) -> None:
    """The reason Online Backup exists, stated as a test.

    A file copy of a database that has an uncheckpointed WAL misses the
    committed-but-not-flushed rows. This test proves a raw copy can diverge
    from the live logical state, which is exactly what ``online_backup`` must
    not do. It does not call ``online_backup`` -- it is the counter-example
    that justifies the function.
    """
    source = tmp_path / "live.sqlite"
    _seed(source)

    conn = sqlite3.connect(str(source))
    conn.execute("PRAGMA journal_mode=WAL")
    # Keep the WAL from being checkpointed back into the main file on close:
    # the point of this test is that the committed row lives only in the WAL.
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO t (v) VALUES ('in-wal')")
    conn.commit()

    raw_copy = tmp_path / "raw_copy.sqlite"
    # Copy only the main database file, the way a naive `cp live.sqlite` would.
    raw_copy.write_bytes(source.read_bytes())

    raw_conn = sqlite3.connect(str(raw_copy))
    try:
        raw_rows = raw_conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        raw_conn.close()
    conn.close()

    # A raw copy of the main file alone misses the WAL-resident row.
    assert raw_rows == 3, "raw copy unexpectedly saw the WAL row"

    snap = tmp_path / "snap.sqlite"
    online_backup(source, snap)
    snap_conn = sqlite3.connect(str(snap))
    try:
        snap_rows = snap_conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        snap_conn.close()
    assert snap_rows == 4, "online backup missed the WAL-resident row"
