"""DAL-R08 ECS baseline: the service CLI's secret-file reader.

The deployment convention (DEV-032, carried into the DAL baseline) is
root-owned, group-readable-by-exactly-one-service-user secret files
(0640 root:<service>), so a service cannot rewrite its own configuration.
The reader must accept that shape AND the historical 0600 self-owned shape,
and refuse everything else fail-closed. These tests cover the shape matrix;
the real composition (systemd unit reading the actual files) is asserted by
deploy/dal-verify.sh on the ECS.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from personal_agent_dal.service.cli import _read_secret_file


def _write(path: Path, mode: int, uid: int) -> Path:
    path.write_bytes(b"secret-value")
    os.chmod(path, mode)
    if uid != os.getuid():
        try:
            os.chown(path, uid, -1)
        except PermissionError:
            pytest.skip("chown to another uid requires privileges")
    return path


@pytest.fixture()
def secret_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    d.mkdir()
    return d


def test_accepts_0600_owned_by_current_user(secret_dir: Path) -> None:
    f = _write(secret_dir / "key", 0o600, os.getuid())
    assert _read_secret_file(f, "service key file") == b"secret-value"


def test_accepts_0640_owned_by_root(secret_dir: Path) -> None:
    f = _write(secret_dir / "key", 0o640, 0)
    assert _read_secret_file(f, "service key file") == b"secret-value"


def test_refuses_0644_world_readable(secret_dir: Path) -> None:
    f = _write(secret_dir / "key", 0o644, os.getuid())
    assert _read_secret_file(f, "service key file") is None


def test_refuses_0600_owned_by_root(secret_dir: Path) -> None:
    # 0600 root-owned is the pre-sudo-created shape: the service user cannot
    # even read it, and claiming it would be a lie about who can.
    f = _write(secret_dir / "key", 0o600, 0)
    assert _read_secret_file(f, "service key file") is None


def test_refuses_0640_owned_by_current_user(secret_dir: Path) -> None:
    # 0640 with the service user as *owner* is not a deployed shape: a
    # group-readable file must be root-owned so the service cannot re-grant
    # its own group. Accepting it would widen the rule silently.
    f = _write(secret_dir / "key", 0o640, os.getuid())
    assert _read_secret_file(f, "service key file") is None


def test_refuses_missing_file(secret_dir: Path) -> None:
    assert _read_secret_file(secret_dir / "absent", "service key file") is None


def test_refuses_empty_file(secret_dir: Path) -> None:
    f = secret_dir / "key"
    f.write_bytes(b"   \n")
    os.chmod(f, 0o600)
    assert _read_secret_file(f, "service key file") is None


def test_error_goes_to_stderr_not_traceback(secret_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = _write(secret_dir / "key", 0o644, os.getuid())
    assert _read_secret_file(f, "service key file") is None
    err = capsys.readouterr().err
    assert "service key file" in err
    assert "Traceback" not in err
