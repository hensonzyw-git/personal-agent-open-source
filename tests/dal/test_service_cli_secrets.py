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


# ---------------------------------------------------------------------------
# R09-B F5: the GitHub App env file parser (executor composition input).
# ---------------------------------------------------------------------------

from personal_agent_dal.service.cli import _read_github_app_env

REQUIRED_KEYS = (
    "PERSONAL_AGENT_DAL_GITHUB_APP_ID",
    "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID",
    "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY",
    "PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH",
)


def _env_text(**overrides: str) -> str:
    values = {
        "PERSONAL_AGENT_DAL_GITHUB_APP_ID": "123456",
        "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID": "7891011",
        "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY": "example-owner/dal-sandbox",
        "PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH": "/etc/dal/github-app.pem",
    }
    values.update(overrides)
    return "".join(f"{k}={v}\n" for k, v in values.items())


def test_env_parses_all_four_values(tmp_path: Path) -> None:
    f = tmp_path / "github-app.env"
    f.write_text(_env_text(), encoding="utf-8")
    values = _read_github_app_env(f)
    assert values is not None
    assert values == dict(REQUIRED_KEYS and {
        "PERSONAL_AGENT_DAL_GITHUB_APP_ID": "123456",
        "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID": "7891011",
        "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY": "example-owner/dal-sandbox",
        "PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH": "/etc/dal/github-app.pem",
    })


def test_env_ignores_comments_blank_lines_and_extras(tmp_path: Path) -> None:
    f = tmp_path / "github-app.env"
    f.write_text(
        "# deployed by provision_dal_keys.sh\n"
        "\n"
        + _env_text()
        + "PERSONAL_AGENT_DAL_GITHUB_API_BASE=https://api.github.com\n",
        encoding="utf-8",
    )
    values = _read_github_app_env(f)
    assert values is not None
    assert set(REQUIRED_KEYS) <= set(values)
    # Extras pass through the dict but are not part of the composition
    # contract: cli.main consumes exactly the four pinned identifiers.
    assert values["PERSONAL_AGENT_DAL_GITHUB_API_BASE"] == "https://api.github.com"


def test_env_missing_file_refuses(tmp_path: Path) -> None:
    assert _read_github_app_env(tmp_path / "absent.env") is None


@pytest.mark.parametrize("missing_key", REQUIRED_KEYS)
def test_env_each_missing_value_refuses(tmp_path: Path, missing_key: str) -> None:
    """Half-composed executors are refused: every pinned value is required."""
    f = tmp_path / "github-app.env"
    f.write_text(_env_text(**{missing_key: ""}), encoding="utf-8")
    assert _read_github_app_env(f) is None


def test_env_refusal_names_the_missing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "github-app.env"
    f.write_text(
        "PERSONAL_AGENT_DAL_GITHUB_APP_ID=123456\n", encoding="utf-8"
    )
    assert _read_github_app_env(f) is None
    err = capsys.readouterr().err
    assert "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID" in err
    assert "Traceback" not in err
