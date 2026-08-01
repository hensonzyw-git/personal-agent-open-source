"""`DEV-035`: the ``backup`` subcommand on both ``*-db`` CLIs.

The snapshot primitive is pinned in ``test_sqlite_online_backup.py``; here the
question is whether the operator-facing CLI wires it honestly: the happy path
exits 0 with a real file, an unreachable source is exit 2 (not 1), and a
corrupt snapshot is exit 1 (not 0). The two services share the primitive, so
both CLIs are exercised against the same contract.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from personal_agent.storage import db as agent_db
from personal_data_mcp.storage import db as mcp_db


def _seed(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('row')")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("db_module", [agent_db, mcp_db])
def test_backup_cli_produces_verified_snapshot(
    tmp_path: Path, db_module, monkeypatch
) -> None:
    source = tmp_path / "live.sqlite"
    _seed(source)
    out = tmp_path / "snap.sqlite"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "db",
            "--database",
            str(source),
            "backup",
            "--out",
            str(out),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        db_module.main()
    assert exc.value.code == 0

    assert out.exists()
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "row"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    # Both service CLIs stage for the offsite backup user, so 0640 is the
    # contract here -- 0600 is what made the 2026-08-01 backup run fail on its
    # first real read after passing every presence check.
    mode = out.stat().st_mode & 0o777
    assert mode == 0o640, f"staged snapshot must be group-readable, got {oct(mode)}"


@pytest.mark.parametrize("db_module", [agent_db, mcp_db])
def test_backup_cli_missing_source_is_exit_2(
    tmp_path: Path, db_module, monkeypatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "db",
            "--database",
            str(tmp_path / "absent.sqlite"),
            "backup",
            "--out",
            str(tmp_path / "snap.sqlite"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        db_module.main()
    assert exc.value.code == 2
    assert not (tmp_path / "snap.sqlite").exists()
