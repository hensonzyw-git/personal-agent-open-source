"""DEV-039 deployment contracts that protect the write-switch boundary.

The real ownership/open checks run on Linux during ECS acceptance. These static
assertions keep the verifier from drifting back to an access-query-only check
that can pass without exercising the actual inode.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_verify_uses_a_real_read_only_write_open_probe() -> None:
    verify = (ROOT / "deploy/verify.sh").read_text()

    assert "os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW" in verify
    assert 'test -w "$SWITCH_FILE"' not in verify
    assert 'sw_dir_owner="$(stat -c %U:%G "$sw_dir")"' in verify
    assert 'sw_dir_mode="$(stat -c %a "$sw_dir")"' in verify
    assert '"$sw_dir_owner" = "root:root"' in verify
    assert '"$sw_dir_mode" = "755"' in verify
