"""`DEV-034`: the alert path, exercised end to end against real databases.

No stub reports and no stub filesystem: every test below builds a real SQLite
database with `create_all`, writes real `ToolExecution` and `AuditEvent` rows,
and runs the real `personal-data-mcp-observe` entry point. §5.1's rule about
fakes applies with force here, because a monitor is precisely the kind of code
that is only ever exercised by its own test doubles until the day it matters.

The property this file cares about most is not "does it find problems" but
**"can it ever be silent about one"**. Hence the exit-code tests, the
cannot-check tests, and `test_a_report_never_prints_anything_but_vocabulary`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from personal_agent_core.timeutil import utc_now
from personal_data_mcp.finance.reconciler import (
    SCHEMA_DRIFT_BLOCKED_EVENT,
    SCHEMA_DRIFT_RESUMED_EVENT,
)
from personal_data_mcp.observability import (
    STUCK_EXECUTION_MINUTES,
    DerivedFacts,
    collect,
    evaluate,
    missing_capabilities,
    worst_severity,
)
from personal_data_mcp.observe_cli import main
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import append_audit_event
from personal_data_mcp.storage.models import AuditEvent, ToolExecution


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    """Pin the disk numbers so these tests measure the code, not the host.

    Found the hard way: the first run of this file failed on a developer Mac
    that was genuinely at 90% full, so `disk_low` fired inside tests asserting
    "no findings". The monitor was right and the tests were wrong -- a test that
    reads the real filesystem is a test whose result depends on who runs it.
    The disk *rules* are covered separately against explicit `DerivedFacts`,
    which is where a threshold belongs anyway.
    """
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: usage(1024**4, 0, 900 * 1024**3)
    )


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "finance.sqlite"
    engine = create_database_engine(path)
    create_all(engine)
    engine.dispose()
    return path


def sessions_for(path: Path):
    engine = create_database_engine(path)
    return session_factory(engine), engine


def add_execution(path: Path, *, key: str, state: str, age_minutes: int = 0) -> None:
    factory, engine = sessions_for(path)
    moment = utc_now() - timedelta(minutes=age_minutes)
    try:
        with factory() as session:
            session.add(
                ToolExecution(
                    idempotency_key=key,
                    tool="finance.log_expense",
                    request_fingerprint=f"fp-{key}",
                    # NOT NULL and unique: the token that makes the source-side
                    # create idempotent.
                    client_token=f"ct-{key}",
                    state=state,
                    state_version=1,
                    created_at=moment,
                    updated_at=moment,
                    # `post_submit_states_record_submission_time`: anything
                    # except these three must record when it reached the source.
                    # Honouring the constraint rather than working around it
                    # matters -- a fixture that can build a row production
                    # cannot would test the monitor against a shape that never
                    # occurs.
                    submitted_at=(
                        None
                        if state
                        in ("prepared", "failed_safe", "cancelled_pre_submit")
                        else moment
                    ),
                )
            )
            session.commit()
    finally:
        engine.dispose()


def facts_for(path: Path) -> DerivedFacts:
    factory, engine = sessions_for(path)
    try:
        with factory() as session:
            return collect(
                finance_session=session,
                agent_session=None,
                databases={"finance": path},
                disk_path=path.parent,
                now=utc_now(),
            )
    finally:
        engine.dispose()


def codes(findings) -> set[str]:
    return {finding.code for finding in findings}


# --- the 立即告警 list ---------------------------------------------------------


def test_a_commit_unknown_write_is_critical(database) -> None:
    add_execution(database, key="k1", state="commit_unknown")
    findings = evaluate(facts_for(database))
    assert "write_commit_unknown" in codes(findings)
    assert worst_severity(findings) == "critical"


def test_a_manual_review_write_is_critical(database) -> None:
    """`needs_manual_review` is where a read-back mismatch lands."""
    add_execution(database, key="k1", state="needs_manual_review")
    findings = evaluate(facts_for(database))
    assert "write_needs_manual_review" in codes(findings)
    assert worst_severity(findings) == "critical"


def test_a_reviewed_manual_review_write_is_no_longer_critical(database) -> None:
    """A person has reported what they found, so the alert clears.

    `state` stays `needs_manual_review` forever -- a human observation must not
    overwrite what the system could prove -- but the alert counts only executions
    with no conclusion yet.
    """
    add_execution(database, key="k1", state="needs_manual_review")
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            execution = session.get(ToolExecution, "k1")
            execution.manual_resolution = "confirmed_written"
            execution.manual_resolved_at = utc_now()
            session.commit()
    finally:
        engine.dispose()

    findings = evaluate(facts_for(database))
    assert "write_needs_manual_review" not in codes(findings)


def test_a_tampered_audit_chain_is_critical(database) -> None:
    """Not a stubbed verifier: a real row is really edited underneath it."""
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            append_audit_event(
                session,
                event_id="e1",
                trace_id="t1",
                event_type="write_prepared",
                redacted_summary="prepared",
                now=utc_now(),
            )
            session.commit()
        with factory() as session:
            # `sequence` is the primary key; `event_id` is merely unique, so
            # this looks the row up the way an editor of the table would.
            event = session.scalars(
                select(AuditEvent).where(AuditEvent.event_id == "e1")
            ).one()
            event.redacted_summary = "something else entirely"
            session.commit()
    finally:
        engine.dispose()

    findings = evaluate(facts_for(database))
    assert "audit_chain_broken" in codes(findings)
    assert worst_severity(findings) == "critical"


def test_deleting_the_audit_tail_is_detected_by_the_anchor(database) -> None:
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            append_audit_event(
                session,
                event_id="e1",
                trace_id="t1",
                event_type="write_prepared",
                redacted_summary="prepared",
                now=utc_now(),
            )
            append_audit_event(
                session,
                event_id="e2",
                trace_id="t1",
                event_type="write_succeeded",
                redacted_summary="succeeded",
                now=utc_now(),
            )
            session.commit()
        with factory() as session:
            session.execute(delete(AuditEvent).where(AuditEvent.event_id == "e2"))
            session.commit()
    finally:
        engine.dispose()

    findings = evaluate(facts_for(database))
    assert "audit_chain_broken" in codes(findings)


def test_a_healthy_database_produces_no_alert(database) -> None:
    add_execution(database, key="k1", state="succeeded")
    findings = evaluate(facts_for(database))
    assert findings == []


def test_a_terminal_execution_is_never_stuck(database) -> None:
    # Age alone must not raise anything: a succeeded write from last year is
    # not a problem, and a rule that said so would train the reader to ignore it.
    add_execution(
        database, key="k1", state="succeeded", age_minutes=STUCK_EXECUTION_MINUTES * 100
    )
    assert evaluate(facts_for(database)) == []


def test_a_long_lived_unfinished_execution_is_a_warning(database) -> None:
    add_execution(
        database,
        key="k1",
        state="submitting",
        age_minutes=STUCK_EXECUTION_MINUTES + 1,
    )
    findings = evaluate(facts_for(database))
    assert "execution_stuck" in codes(findings)


def test_a_recent_unfinished_execution_is_not_yet_a_warning(database) -> None:
    add_execution(database, key="k1", state="submitting", age_minutes=1)
    assert codes(evaluate(facts_for(database))) == set()


@pytest.mark.parametrize(
    "state", ["prepared", "committed_unverified", "reconciling_same_client_token"]
)
def test_stuck_is_measured_by_age_not_by_a_state_allowlist(database, state) -> None:
    """Every unfinished state, not just the two the alert list names.

    `stuck_execution_count` excludes `TERMINAL_EXECUTION_STATES` rather than
    listing the unfinished ones, so a state added later is watched from the day
    it exists instead of from the day someone remembers this file. `prepared` is
    the case that makes the difference visible: it is not post-submit, so a rule
    written around `submitting`/`commit_unknown` would miss a write that stalled
    before it ever left.

    An invented state cannot be used to make this point -- `ck_tool_executions_state`
    rejects anything outside `EXECUTION_STATES` at the database level, which is
    a stronger guarantee than this rule and one level below it.
    """
    add_execution(
        database, key="k1", state=state, age_minutes=STUCK_EXECUTION_MINUTES + 1
    )
    assert "execution_stuck" in codes(evaluate(facts_for(database)))


# --- thresholds ---------------------------------------------------------------


def test_the_disk_floor_is_the_larger_of_the_ratio_and_the_fixed_size() -> None:
    # Large disk: the 10% ratio binds, because 1 GiB free on a 1 TiB disk is
    # minutes from full.
    big = DerivedFacts(disk_free_bytes=2 * 1024**3, disk_total_bytes=1024**4)
    assert "disk_low" in codes(evaluate(big))
    # Small disk: the fixed 1 GiB binds, because 10% of 4 GiB is not enough room
    # to checkpoint a WAL.
    small = DerivedFacts(disk_free_bytes=800 * 1024**2, disk_total_bytes=4 * 1024**3)
    assert "disk_low" in codes(evaluate(small))
    healthy = DerivedFacts(disk_free_bytes=200 * 1024**3, disk_total_bytes=1024**4)
    assert "disk_low" not in codes(evaluate(healthy))


def test_an_unreadable_disk_makes_the_cli_exit_cannot_check(
    database, tmp_path, capsys, monkeypatch
) -> None:
    import shutil

    def unreadable(_path):
        raise OSError("statvfs refused")

    monkeypatch.setattr(shutil, "disk_usage", unreadable)
    missing = tmp_path / "filesystem-that-does-not-exist"
    assert main(
        ["--database", str(database), "--disk-path", str(missing)]
    ) == 2
    assert "cannot check" in capsys.readouterr().err


def test_a_large_wal_is_a_warning() -> None:
    assert "wal_large" in codes(
        evaluate(DerivedFacts(wal_bytes={"finance": 200 * 1024**2}))
    )
    assert "wal_large" not in codes(
        evaluate(DerivedFacts(wal_bytes={"finance": 1024}))
    )


# --- the gaps must speak ------------------------------------------------------


def test_the_report_says_what_it_cannot_see() -> None:
    """Silence about an unmeasurable thing is indistinguishable from health."""
    gaps = codes(missing_capabilities())
    # DEV-036 closed backup age: it is now a real check, not a missing
    # capability. Only the push-metrics gap remains.
    assert "backup_age_unknown" not in gaps
    assert "push_metrics_unwired" in gaps
    # Info only: a known gap must not make a healthy box look unhealthy.
    assert worst_severity(missing_capabilities()) == "info"


def test_the_backup_gap_message_makes_no_claim_about_backups_existing() -> None:
    """A gap message ages with the project, and this one once aged into a lie.

    The historical `backup_age_unknown` missing-capability entry read "there
    are no backups to age" after DEV-035 had shipped real snapshots. DEV-036
    replaced it with a real check that reads the backup-success marker, so the
    message about a missing measurement is gone entirely. What remains is the
    `backup_never_succeeded` finding when the marker is configured but absent;
    it starts as info and becomes a warning after the startup grace.
    """
    # The missing-capability entry is gone.
    assert "backup_age_unknown" not in codes(missing_capabilities())


def test_the_backup_age_check_only_runs_when_a_marker_is_configured(
    tmp_path: Path,
) -> None:
    # Without a marker path, the check is silent: a caller that does not wire
    # the marker is not claiming backups are fresh, it is just not checking.
    facts = collect(
        finance_session=None,
        agent_session=None,
        databases={},
        disk_path=tmp_path,
        now=utc_now(),
        backup_marker=None,
    )
    assert "backup_never_succeeded" not in codes(evaluate(facts))
    assert "backup_stale" not in codes(evaluate(facts))


# --- the CLI contract ---------------------------------------------------------


def test_a_healthy_box_exits_zero(database, capsys) -> None:
    add_execution(database, key="k1", state="succeeded")
    assert main(["--database", str(database)]) == 0


def test_a_critical_finding_exits_one(database) -> None:
    add_execution(database, key="k1", state="commit_unknown")
    assert main(["--database", str(database)]) == 1


def test_a_warning_also_exits_one(database) -> None:
    add_execution(
        database, key="k1", state="submitting", age_minutes=STUCK_EXECUTION_MINUTES + 1
    )
    assert main(["--database", str(database)]) == 1


def test_a_missing_database_exits_two_not_zero(tmp_path, capsys) -> None:
    """The most important test in this file.

    A monitor that cannot read its database and exits 0 is worse than no
    monitor: the timer stays green forever and the box is unobserved. 2 says
    "could not check", which is a different state from "checked, all well".
    """
    assert main(["--database", str(tmp_path / "absent.sqlite")]) == 2
    assert "cannot check" in capsys.readouterr().err


def test_an_unreadable_database_exits_two(tmp_path, capsys) -> None:
    corrupt = tmp_path / "finance.sqlite"
    corrupt.write_bytes(b"this is not a database")
    assert main(["--database", str(corrupt)]) == 2
    assert "cannot check" in capsys.readouterr().err


def test_alerts_go_to_stderr_so_the_journal_ranks_them(database, capsys) -> None:
    add_execution(database, key="k1", state="commit_unknown")
    main(["--database", str(database)])
    captured = capsys.readouterr()
    assert "write_commit_unknown" in captured.err
    # Known gaps are info and belong on stdout, not mixed in with alerts.
    assert "push_metrics_unwired" in captured.out


def test_json_mode_is_machine_readable(database, capsys) -> None:
    add_execution(database, key="k1", state="commit_unknown")
    main(["--database", str(database), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "personal_agent_observe"
    assert report["worst_severity"] == "critical"
    assert any(f["code"] == "write_commit_unknown" for f in report["findings"])


# --- nothing printable may come from data ------------------------------------


def test_a_report_never_prints_anything_but_vocabulary(database, capsys) -> None:
    """The redaction claim, tested where it would actually break.

    A ledger name, a Feishu record id and an idempotency key all pass through
    the rows this report counts. None may appear in its output -- the report is
    counts and codes, never contents.
    """
    add_execution(database, key="recSECRET123456", state="commit_unknown")
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            execution = session.get(ToolExecution, "recSECRET123456")
            execution.tool = "finance.log_expense"
            session.commit()
    finally:
        engine.dispose()

    main(["--database", str(database), "--json"])
    output = capsys.readouterr()
    assert "recSECRET123456" not in output.out
    assert "recSECRET123456" not in output.err


# --- schema drift on the one path nobody watches ------------------------------


def test_a_drift_that_blocked_recovery_is_critical_and_names_where_to_look(
    database,
) -> None:
    """Diagnosis, not detection.

    A drift that blocks recovery already surfaces as `execution_stuck` within
    the hour, because executions stop reaching a terminal state. This finding
    exists because "something is stuck" points at the reconciler while the
    change actually happened in the Feishu Base, and the difference is an hour
    of looking in the wrong place.
    """
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            append_audit_event(
                session,
                event_id="d1",
                trace_id="t1",
                event_type=SCHEMA_DRIFT_BLOCKED_EVENT,
                redacted_summary="write recovery refused",
                now=utc_now(),
            )
            session.commit()
    finally:
        engine.dispose()

    findings = evaluate(facts_for(database))
    assert "schema_drift_blocked_recovery" in codes(findings)
    assert worst_severity(findings) == "critical"
    detail = next(
        f.detail for f in findings if f.code == "schema_drift_blocked_recovery"
    )
    assert "Feishu Base" in detail, "the finding must say where to look"


def test_a_resumed_schema_validation_clears_the_active_drift_alert(database) -> None:
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            append_audit_event(
                session,
                event_id="d1",
                trace_id="reconcile-k1",
                event_type=SCHEMA_DRIFT_BLOCKED_EVENT,
                redacted_summary="write recovery refused",
                now=utc_now(),
            )
            append_audit_event(
                session,
                event_id="d2",
                trace_id="reconcile-k1",
                event_type=SCHEMA_DRIFT_RESUMED_EVENT,
                redacted_summary="write recovery resumed",
                now=utc_now(),
            )
            session.commit()
    finally:
        engine.dispose()

    assert "schema_drift_blocked_recovery" not in codes(
        evaluate(facts_for(database))
    )


def test_ordinary_audit_events_are_not_counted_as_drift(database) -> None:
    # The count is a filtered query, not "audit events that are not writes".
    factory, engine = sessions_for(database)
    try:
        with factory() as session:
            append_audit_event(
                session,
                event_id="w1",
                trace_id="t1",
                event_type="write_succeeded",
                redacted_summary="ok",
                now=utc_now(),
            )
            session.commit()
    finally:
        engine.dispose()
    assert "schema_drift_blocked_recovery" not in codes(evaluate(facts_for(database)))


# --- DEV-036: backup age --------------------------------------------------
# The marker is written by deploy/backup.sh after `restic check` passes. These
# tests cover the states the observer must distinguish: no marker inside the
# startup grace (info), no marker after the grace (warning), a fresh marker (no
# finding), and a stale marker (warning). Corrupted, future or incompletely
# configured witnesses fail closed rather than reading as "no backup yet".


def _facts_with_marker(
    tmp_path: Path,
    marker_text: str | None,
    *,
    now=None,
    monitor_started_text: str | None = None,
) -> tuple[DerivedFacts, Path]:
    from personal_agent_core.timeutil import to_rfc3339

    now = now or utc_now()
    marker = tmp_path / "last-successful-backup"
    monitor_start = tmp_path / "monitoring-started-at"
    if marker_text is not None:
        marker.write_text(marker_text)
    monitor_start.write_text(
        monitor_started_text
        if monitor_started_text is not None
        else to_rfc3339(now - timedelta(hours=1))
    )
    return (
        collect(
            finance_session=None,
            agent_session=None,
            databases={},
            disk_path=tmp_path,
            now=now,
            backup_marker=marker,
            backup_monitor_start=monitor_start,
        ),
        marker,
    )


def test_no_marker_is_info_not_a_stale_warning(tmp_path: Path) -> None:
    facts, _ = _facts_with_marker(tmp_path, None)
    findings = evaluate(facts)
    assert "backup_never_succeeded" in codes(findings)
    assert "backup_stale" not in codes(findings)
    # And the missing-capabilities entry for backup age is gone (DEV-036).
    assert "backup_age_unknown" not in codes(missing_capabilities())


def test_no_success_marker_becomes_a_warning_after_the_startup_grace(
    tmp_path: Path,
) -> None:
    from personal_agent_core.timeutil import to_rfc3339

    now = utc_now()
    facts, _ = _facts_with_marker(
        tmp_path,
        None,
        now=now,
        monitor_started_text=to_rfc3339(now - timedelta(hours=72)),
    )
    finding = next(
        finding
        for finding in evaluate(facts)
        if finding.code == "backup_never_succeeded"
    )
    assert finding.severity == "warning"


def test_a_fresh_marker_produces_no_finding(tmp_path: Path) -> None:
    from personal_agent_core.timeutil import to_rfc3339

    facts, _ = _facts_with_marker(tmp_path, to_rfc3339(utc_now()))
    assert "backup_never_succeeded" not in codes(evaluate(facts))
    assert "backup_stale" not in codes(evaluate(facts))


def test_a_stale_marker_is_a_warning(tmp_path: Path) -> None:
    from personal_agent_core.timeutil import to_rfc3339

    old = utc_now() - timedelta(hours=72)
    facts, _ = _facts_with_marker(tmp_path, to_rfc3339(old))
    findings = evaluate(facts)
    assert "backup_stale" in codes(findings)
    assert "backup_never_succeeded" not in codes(findings)


def test_a_marker_just_inside_the_threshold_is_not_stale(tmp_path: Path) -> None:
    from personal_agent_core.timeutil import to_rfc3339

    from personal_data_mcp.observability import BACKUP_STALE_AFTER_HOURS

    age = timedelta(hours=BACKUP_STALE_AFTER_HOURS, seconds=-1)
    facts, _ = _facts_with_marker(tmp_path, to_rfc3339(utc_now() - age))
    assert "backup_stale" not in codes(evaluate(facts))


def test_a_corrupted_marker_fails_closed(tmp_path: Path) -> None:
    # A marker that exists but is not a timestamp must not be read as "no
    # backup yet". `collect` raises, which the CLI turns into exit 2
    # (cannot-check) -- the same state as a database it cannot open.
    import pytest

    with pytest.raises(Exception):
        _facts_with_marker(tmp_path, "not-a-timestamp")


def test_a_missing_monitoring_start_fails_closed(tmp_path: Path) -> None:
    marker = tmp_path / "last-successful-backup"
    monitor_start = tmp_path / "missing-monitoring-start"

    with pytest.raises(FileNotFoundError):
        collect(
            finance_session=None,
            agent_session=None,
            databases={},
            disk_path=tmp_path,
            now=utc_now(),
            backup_marker=marker,
            backup_monitor_start=monitor_start,
        )


def test_only_one_backup_path_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configured together"):
        collect(
            finance_session=None,
            agent_session=None,
            databases={},
            disk_path=tmp_path,
            now=utc_now(),
            backup_marker=tmp_path / "last-successful-backup",
        )


def test_a_future_success_marker_fails_closed(tmp_path: Path) -> None:
    from personal_agent_core.timeutil import to_rfc3339

    now = utc_now()
    with pytest.raises(ValueError, match="in the future"):
        _facts_with_marker(
            tmp_path,
            to_rfc3339(now + timedelta(hours=1)),
            now=now,
        )
