"""Local credential bootstrap must fail without ever exposing a private file."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


def test_an_openssl_failure_leaves_the_data_key_private(tmp_path: Path) -> None:
    """`umask 077` protects the first file before the final chmod can run."""
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[2] / "scripts" / "setup_local_agent_keys.sh"
    target = scripts / source.name
    shutil.copy2(source, target)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    failing_openssl = fake_bin / "openssl"
    failing_openssl.write_text("#!/usr/bin/env bash\nexit 23\n")
    failing_openssl.chmod(0o755)

    result = subprocess.run(
        ["bash", str(target)],
        cwd=project,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23
    data_key = project / "config" / "agent-data.key"
    assert data_key.is_file()
    assert stat.S_IMODE(data_key.stat().st_mode) == 0o600
