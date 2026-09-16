"""DEV-036 deployment contracts that macOS cannot execute with systemd.

The real ECS run remains the acceptance evidence. These focused assertions stop
the unit/script wiring from drifting back to combinations that are guaranteed
to fail before that rollout, such as a marker path omitted from ReadWritePaths.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_backup_unit_can_write_only_cache_and_non_secret_state() -> None:
    unit = _read("deploy/systemd/personal-agent-backup.service")

    assert (
        "ReadWritePaths=/var/cache/restic /var/lib/personal-agent-backup" in unit
    )
    assert "ProtectSystem=strict" in unit
    assert "/var/lib/personal-agent-api" not in unit
    assert "/var/lib/personal-data-mcp" not in unit


def test_backup_success_marker_is_daily_guarded_and_atomically_replaced() -> None:
    script = _read("deploy/backup.sh")

    guard = script.index('if [ -f "$MARKER" ]')
    backup = script.index("restic backup")
    assert guard < backup
    assert "last_success_day" in script
    assert "duplicate trigger is a no-op" in script
    assert 'mv -f "$tmp" "$MARKER"' in script
    assert 'install -m 0644 "$tmp" "$MARKER"' not in script


def test_monitoring_baseline_is_created_once_not_refreshed_on_reinstall() -> None:
    install = _read("deploy/install.sh")

    assert 'if [ ! -e "$BACKUP_MONITOR_START" ]' in install
    assert 'mv -n "$monitor_tmp" "$BACKUP_MONITOR_START"' in install


def test_observer_receives_both_backup_age_inputs_read_only() -> None:
    unit = _read("deploy/systemd/personal-data-mcp-observe.service")

    assert "--backup-marker /var/lib/personal-agent-backup/" in unit
    assert "--backup-monitor-start /var/lib/personal-agent-backup/" in unit
    assert "ReadOnlyPaths=/var/lib/personal-agent-backup" in unit
    assert "ReadWritePaths=/var/lib/personal-agent-backup" not in unit


def test_cleanup_unit_has_no_api_environment_or_key_access() -> None:
    unit = _read("deploy/systemd/personal-agent-cleanup.service")

    assert "EnvironmentFile=" not in unit
    assert (
        "InaccessiblePaths=/etc/personal-agent/api.env "
        "/etc/personal-agent/keys/api" in unit
    )


def test_cleanup_timer_enforces_transcript_retention_independently() -> None:
    cleanup = _read("deploy/systemd/personal-agent-cleanup.service")
    api = _read("deploy/systemd/personal-agent-api.service")
    install = _read("deploy/install.sh")

    assert "--transcript-directory /var/lib/personal-agent-api/transcripts" in cleanup
    assert "--transcript-retention-days 14" in cleanup
    assert "PERSONAL_AGENT_TRANSCRIPT_RETENTION_DAYS=14" in api
    assert "/var/lib/personal-agent-api/transcripts" in install


def test_verify_requires_real_oneshot_runs_and_fresh_marker() -> None:
    verify = _read("deploy/verify.sh")

    for unit in (
        "personal-agent-review.service",
        "personal-agent-cleanup.service",
        "personal-agent-backup.service",
    ):
        assert f"expect_oneshot_succeeded {unit}" in verify
    assert "last-successful-backup\" 172800" in verify


def test_rollback_stops_and_removes_every_dev036_unit_without_deleting_state() -> None:
    rollback = _read("deploy/rollback.sh")

    for unit in (
        "personal-agent-review.service",
        "personal-agent-review.timer",
        "personal-agent-cleanup.service",
        "personal-agent-cleanup.timer",
    ):
        assert unit in rollback
    assert "rm -rf" not in rollback
    assert "/var/lib/personal-agent-backup" in rollback


def test_pa_process_stop_bounds_graceful_thread_drain() -> None:
    unit = _read('deploy/systemd/personal-agent-api.service')
    settings = dict(line.split('=', 1) for line in unit.splitlines()
                    if '=' in line and not line.startswith('#'))
    assert settings['TimeoutStopSec'] == '60s'
    assert settings['KillMode'] == 'control-group'
    assert settings['SendSIGKILL'] == 'yes'
    assert settings['Restart'] == 'on-failure'
    assert '--socket /run/personal-agent/api.sock' in unit
