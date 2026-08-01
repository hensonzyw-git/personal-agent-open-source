from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_restore_drill_never_deletes_caller_owned_directory(tmp_path: Path) -> None:
    restore_parent = tmp_path / "restore-parent"
    restore_parent.mkdir()
    sentinel = restore_parent / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    restic = bin_dir / "restic"
    restic.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    restic.chmod(0o755)

    data_key = tmp_path / "data.key"
    data_key.write_bytes(b"test-data-key")
    service_public_key = tmp_path / "service.pub.pem"
    service_public_key.write_text("test-public-key", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "RESTORE_PARENT": str(restore_parent),
            # The vulnerable revision treated this caller-owned directory as
            # disposable. It is now intentionally ignored.
            "RESTORE_DIR": str(restore_parent),
            "RESTIC_REPOSITORY": "test:repository",
            "PERSONAL_AGENT_DATA_ACTIVE_KID": "test-data-key",
            "PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH": str(data_key),
            "PERSONAL_DATA_MCP_SERVICE_ACTIVE_KID": "test-service-key",
            "PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH": str(
                service_public_key
            ),
        }
    )

    result = subprocess.run(
        ["bash", str(project_root / "scripts" / "restore_drill.sh")],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(restore_parent.glob("personal-agent-drill.*")) == []
