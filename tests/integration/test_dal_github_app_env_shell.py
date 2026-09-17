"""The github-app.env provisioning + start-gate shell contract, pinned offline.

The 2026-09-04 deployment gap: `personal-agent-dal-api.service` loads
`/etc/personal-agent/dal.env.d/github-app.env` unconditionally, so a fresh
machine following the docs could not boot the unit, and nothing verified the
template had been filled in before enabling it. These tests run the two
scripts as real subprocesses against a fixture root. The gate reads its two
paths from the environment (production defaults, fixture overrides), and its
root check is skipped under the same override — the ownership checks it
guards are still executed against the fixture files.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

GATE = PROJECT_ROOT / "deploy" / "verify_dal_github_app.sh"
PROVISION = PROJECT_ROOT / "deploy" / "provision_dal_keys.sh"

FOUR_VARS = (
    "PERSONAL_AGENT_DAL_GITHUB_APP_ID",
    "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID",
    "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY",
    "PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH",
)


def _pem(tmp_path: Path) -> Path:
    """A real 2048-bit RSA private key, so the openssl parse check passes."""
    key = tmp_path / "github-app.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
         "rsa_keygen_bits:2048", "-out", str(key)],
        check=True, capture_output=True,
    )
    return key


def _run_gate(tmp_path: Path, *, env_values: dict[str, str] | None = None,
              key: Path | None = None, skip_env: bool = False,
              skip_key: bool = False) -> subprocess.CompletedProcess[str]:
    """Build a fixture tree and run the gate with both paths overridden."""
    env_file = tmp_path / "github-app.env"
    if not skip_env:
        values = {
            "PERSONAL_AGENT_DAL_GITHUB_APP_ID": "4807112",
            "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID": "158537127",
            "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY": "example-owner/dal-sandbox",
            "PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH": str(
                key if key is not None else tmp_path / "github-app.pem"
            ),
            **(env_values or {}),
        }
        env_file.write_text(
            "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8"
        )
        # The gate asserts 0640; the fixture file must satisfy it.
        env_file.chmod(0o640)

    if key is not None and not skip_key:
        key.chmod(0o640)

    env = os.environ.copy()
    env["GITHUB_APP_ENV"] = str(env_file)
    env["VERIFY_DAL_GITHUB_APP_ALLOW_NON_ROOT"] = "1"
    return subprocess.run(
        ["bash", str(GATE)], env=env, capture_output=True, text=True, check=False
    )


def test_provision_script_creates_a_github_app_env_template() -> None:
    """The unit's EnvironmentFile must come into existence at provision time.

    Pins the gap fix: the provision script carries the template block naming
    all four variables the adapter actually reads (a variable renamed on one
    side only would pass a filename grep and still fail to boot).
    """
    text = PROVISION.read_text(encoding="utf-8")
    assert "github-app.env" in text, "provision must create the env file"
    for var in FOUR_VARS:
        assert var in text, f"template must carry {var}"
    # The template ships empty identifier values (operator-filled), and the
    # key path default lives inside dal.env.d, not a home directory.
    assert "PERSONAL_AGENT_DAL_GITHUB_APP_ID=\n" in text
    assert "PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH=/etc/personal-agent/dal.env.d/" in text


def test_unit_file_and_template_variable_sets_match() -> None:
    """The unit loads the file; the template must define what the adapter
    reads. Drift between the two sides is the silent-boot-failure shape."""
    unit = (PROJECT_ROOT / "deploy" / "systemd" /
            "personal-agent-dal-api.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/personal-agent/dal.env.d/github-app.env" in unit
    assert "--github-app-env-file /etc/personal-agent/dal.env.d/github-app.env" in unit
    provision_text = PROVISION.read_text(encoding="utf-8")
    for var in FOUR_VARS:
        assert var in provision_text


def test_reconciliation_timer_is_installed_and_calls_the_operator_console() -> None:
    """The persistence-driven sweep is a real timer, not only a CLI verb."""
    systemd = PROJECT_ROOT / "deploy" / "systemd"
    service = (systemd / "personal-agent-dal-reconcile.service").read_text(
        encoding="utf-8"
    )
    timer = (systemd / "personal-agent-dal-reconcile.timer").read_text(
        encoding="utf-8"
    )
    install = (PROJECT_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "personal-agent-dal-console" in service
    assert "--service-key-file /etc/personal-agent/dal.env.d/service-key" in service
    assert "reconcile-sweep" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "personal-agent-dal-reconcile.service" in install
    assert "personal-agent-dal-reconcile.timer" in install


def test_gate_passes_a_complete_configuration(tmp_path: Path) -> None:
    key = _pem(tmp_path)
    result = _run_gate(tmp_path, key=key)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_gate_refuses_an_empty_template(tmp_path: Path) -> None:
    key = _pem(tmp_path)
    result = _run_gate(
        tmp_path, key=key,
        env_values={
            "PERSONAL_AGENT_DAL_GITHUB_APP_ID": "",
            "PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID": "",
            "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY": "",
        },
    )
    assert result.returncode != 0, "an empty template must not pass"
    assert "is empty" in result.stderr


def test_gate_refuses_a_missing_key_file(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, key=None, skip_key=True)
    assert result.returncode != 0, "a missing key file must not pass"
    assert "private key" in result.stderr


def test_gate_refuses_a_non_pem_key_file(tmp_path: Path) -> None:
    junk = tmp_path / "github-app.pem"
    junk.write_bytes(b"definitely not a key" * 8)
    result = _run_gate(tmp_path, key=junk)
    assert result.returncode != 0, "a non-PEM key file must not pass"
    assert "private key" in result.stderr


@pytest.mark.parametrize('kind', ['ec', 'public', 'encrypted', 'inconsistent', 'truncated'])
def test_gate_refuses_invalid_rsa_private_keys(tmp_path: Path, kind: str) -> None:
    """Synthetic keys exercise parse, type, encryption and consistency failures."""
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == 'ec':
        key = ec.generate_private_key(ec.SECP256R1())
    if kind == 'public':
        content = key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
    elif kind == 'inconsistent':
        der = key.private_bytes(serialization.Encoding.DER,
            serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        # Corrupt only the CRT coefficient, leaving a parseable RSA structure.
        der = der[:-1] + bytes([der[-1] ^ 1])
        content = (b'-----BEGIN RSA PRIVATE KEY-----\n' + base64.encodebytes(der)
                   + b'-----END RSA PRIVATE KEY-----\n')
    else:
        encryption = (serialization.BestAvailableEncryption(b'synthetic-only')
                      if kind == 'encrypted' else serialization.NoEncryption())
        content = key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, encryption)
        if kind == 'truncated': content = content[:100]
    path = tmp_path / 'synthetic-invalid.pem'
    path.write_bytes(content)
    result = _run_gate(tmp_path, key=path)
    assert result.returncode != 0
    assert 'private key' in result.stderr


def test_gate_refuses_loose_permissions(tmp_path: Path) -> None:
    """A 0644 env file defeats the 0640 custody rule the trust domain runs on."""
    key = _pem(tmp_path)
    result = _run_gate(tmp_path, key=key)
    assert result.returncode == 0, result.stderr
    # Now break the env file's mode and re-run: the ownership/mode assert must
    # catch it.
    env_file = tmp_path / "github-app.env"
    env_file.chmod(0o644)
    env = os.environ.copy()
    env["GITHUB_APP_ENV"] = str(env_file)
    env["VERIFY_DAL_GITHUB_APP_ALLOW_NON_ROOT"] = "1"
    loose = subprocess.run(
        ["bash", str(GATE)], env=env, capture_output=True, text=True, check=False
    )
    assert loose.returncode != 0, "a 0644 env file must not pass"
    assert "640" in loose.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o644  # untouched by the gate
