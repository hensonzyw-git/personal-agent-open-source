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


def test_the_write_open_probe_has_a_positive_control() -> None:
    """A refusal-only assertion passes for any reason the command fails.

    Measured on the ECS 2026-08-03: a wrong interpreter path and a wrong target
    path both exit non-zero, which the refusal checks would have read as
    "correctly refused". The control proves the probe can succeed where writing
    is allowed, so its refusal means permission rather than breakage.
    """
    verify = (ROOT / "deploy/verify.sh").read_text()

    assert "the write-open probe can actually open a writable file" in verify
    # The control must run the same probe string, or it proves nothing about
    # the checks it is supposed to arm.
    control = verify.split(
        "the write-open probe can actually open a writable file"
    )[1].split("expect_refused")[0]
    assert '"$WRITE_OPEN_PROBE"' in control
    assert "mktemp" in control
    # And it must come *before* the refusals it arms.
    assert verify.index(
        "the write-open probe can actually open a writable file"
    ) < verify.index("cannot open the write switch for writing")
