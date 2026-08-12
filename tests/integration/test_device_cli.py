"""DEV-029: the operator device CLI (`personal-agent-device`).

Design 5.1 keeps device administration off the public surface, so this CLI is
the only way a code is minted or a scope is changed. The tests that matter are
the ones where a careless operator command would leave a device in a state that
looks like something else: an unknown scope silently granting nothing, an empty
scope set leaving a device enrolled with no authority, a revoke against an id
that does not exist.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent import device_cli
from personal_agent.auth.enrollment import (
    DEFAULT_DEVICE_SCOPES,
    DEVICE_MANAGE_SCOPE,
    decode_device_scopes,
    encode_device_scopes,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device, EnrollmentCode
from personal_agent_core.manifest import load_manifest


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "agent.sqlite"
    engine = create_database_engine(path)
    create_all(engine)
    engine.dispose()
    return path


def run(database: Path, *args: str) -> None:
    import sys

    argv = ["personal-agent-device", "--database", str(database), *args]
    original = sys.argv
    sys.argv = argv
    try:
        device_cli.main()
    finally:
        sys.argv = original


def seed_device(database: Path, *, device_id: str = "dev-1", scopes=None) -> None:
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id=device_id,
                display_name="iPhone",
                public_key="unused-in-this-test",
                device_key_thumbprint="THUMB",
                status="active",
                scopes=encode_device_scopes(
                    scopes if scopes is not None else list(DEFAULT_DEVICE_SCOPES)
                ),
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.commit()
    engine.dispose()


def read_device(database: Path, device_id: str = "dev-1") -> Device:
    engine = create_database_engine(database)
    try:
        with session_factory(engine)() as session:
            device = session.get(Device, device_id)
            session.expunge(device)
            return device
    finally:
        engine.dispose()


def test_issue_code_prints_a_code_and_stores_only_its_hash(
    database: Path, capsys
) -> None:
    run(database, "issue-code")
    printed = capsys.readouterr().out
    code = re.search(r"enrollment code: (\S+)", printed).group(1)

    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        rows = session.query(EnrollmentCode).all()
        assert len(rows) == 1
        assert rows[0].grants_device_manage is False
        assert code not in rows[0].code_hash
    engine.dispose()


def test_issue_code_grants_manage_only_when_asked(database: Path, capsys) -> None:
    run(database, "issue-code", "--grants-device-manage")
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        assert session.query(EnrollmentCode).one().grants_device_manage is True
    engine.dispose()


def test_list_prints_no_key_material(database: Path, capsys) -> None:
    seed_device(database)
    run(database, "list")
    printed = capsys.readouterr().out
    assert "dev-1" in printed
    assert "unused-in-this-test" not in printed
    assert "THUMB" not in printed


def test_revoke_marks_the_device_and_is_idempotent(database: Path, capsys) -> None:
    seed_device(database)
    run(database, "revoke", "--device-id", "dev-1")
    assert read_device(database).status == "revoked"
    run(database, "revoke", "--device-id", "dev-1")
    assert "already revoked" in capsys.readouterr().out


def test_revoking_an_unknown_device_fails_loudly(database: Path) -> None:
    with pytest.raises(SystemExit):
        run(database, "revoke", "--device-id", "nope")


def test_set_scopes_replaces_the_set(database: Path, capsys) -> None:
    seed_device(database)
    run(
        database,
        "set-scopes",
        "--device-id",
        "dev-1",
        "--scope",
        "device.self.read",
        "--scope",
        DEVICE_MANAGE_SCOPE,
    )
    assert set(decode_device_scopes(read_device(database).scopes)) == {
        "device.self.read",
        DEVICE_MANAGE_SCOPE,
    }


def test_rebind_tools_updates_only_an_active_device_manifest(
    database: Path, capsys
) -> None:
    seed_device(database)
    before_scopes = read_device(database).scopes

    run(database, "rebind-tools", "--device-id", "dev-1")

    rebound = read_device(database)
    assert rebound.allowed_tools_version == load_manifest()["allowed_tools_version"]
    assert rebound.scopes == before_scopes
    assert rebound.status == "active"
    assert "tools_version: v1 ->" in capsys.readouterr().out


def test_rebind_tools_refuses_a_revoked_device(database: Path) -> None:
    seed_device(database)
    run(database, "revoke", "--device-id", "dev-1")

    with pytest.raises(SystemExit, match="non-active"):
        run(database, "rebind-tools", "--device-id", "dev-1")


def test_an_unknown_scope_is_refused_rather_than_granted(database: Path) -> None:
    """A typo that is accepted produces a device that holds nothing and looks
    like a permissions bug in the API instead of a bad command."""
    seed_device(database)
    with pytest.raises(SystemExit):
        run(database, "set-scopes", "--device-id", "dev-1", "--scope", "finance.writ")
    assert set(decode_device_scopes(read_device(database).scopes)) == set(
        DEFAULT_DEVICE_SCOPES
    )


def test_an_empty_scope_set_is_refused(database: Path) -> None:
    seed_device(database)
    with pytest.raises(SystemExit):
        run(database, "set-scopes", "--device-id", "dev-1")
    assert set(decode_device_scopes(read_device(database).scopes)) == set(
        DEFAULT_DEVICE_SCOPES
    )


def test_a_missing_database_is_refused_rather_than_created(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(SystemExit):
        run(missing, "list")
    assert not missing.exists()


def test_every_declared_tool_scope_is_grantable(database: Path) -> None:
    """The CLI's scope allowlist is derived from the manifest, so a new tool's
    scope becomes grantable without editing this file."""
    known = device_cli.known_scopes()
    for scope in DEFAULT_DEVICE_SCOPES:
        assert scope in known
    assert "finance.expense.write" in known
    assert DEVICE_MANAGE_SCOPE in known
