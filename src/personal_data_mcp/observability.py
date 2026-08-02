"""`DEV-034`: derived facts and the alert rules over them.

Design 10.4 splits into two halves. `metrics.py` holds the half a running
process knows -- latencies, refusals, provider status classes -- and loses on
restart by design. This file holds the other half: facts that are already
durable somewhere, read at report time.

**Why derive rather than count.** A counter for "how many executions are stuck
in `commit_unknown`" is wrong twice. It resets on restart, so the number is
lowest exactly when something has just crashed; and it is a second copy of a
truth the database already holds, so the two can disagree and the wrong one is
the one being alerted on. Reading `ToolExecution` answers the question the alert
is actually about -- *is there a stuck row right now* -- and it answers it the
same way after a restart as before.

**Why a separate process.** Henson chose journald plus systemd unit failure as
the alert channel (2026-08-01). That decision is what makes this file a library
for a CLI rather than a background task inside the API: a timer-driven one-shot
that exits non-zero cannot be prevented from reporting by the very outage it is
meant to report. An in-process alerter dies with its process.

**Backup age, and why it is read from a marker.** `deploy/backup.sh` writes a
marker file only after `restic check` passes, so the marker's presence means a
backup landed in OSS and verified there -- not merely that the script ran. The
observer cannot call restic itself: it runs as the Finance service user with no
network and no repository credentials, so the backup job (the one party with
both) is the witness, and the observer reads what it left. `missing_capabilities`
no longer names backup age. An immutable monitoring-start timestamp prevents a
backup that never succeeds from remaining a "fresh install" forever: after 48
hours without a success marker, the finding becomes a warning.

Nothing here decides *presentation*. Findings carry a severity and a
machine-readable code; the CLI decides what that means for an exit status.

It lives under `personal_data_mcp` rather than `personal_agent_core` for two
reasons that agree: `personal_agent_core` deliberately depends on neither
service, and the durable facts design 10.4 alerts on -- `commit_unknown`,
read-back mismatch, the audit chain -- are all Finance-side. The service users
also cannot read each other's 0700 data directories, so an observer that wanted
both databases would have to run as root and give up the isolation `DEV-032`
exists to provide.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from personal_agent_core.timeutil import parse_rfc3339, to_rfc3339
from personal_data_mcp.finance.reconciler import (
    SCHEMA_DRIFT_BLOCKED_EVENT,
    SCHEMA_DRIFT_RESUMED_EVENT,
)
from personal_data_mcp.storage.execution_store import verify_audit_chain
from personal_data_mcp.storage.models import (
    TERMINAL_EXECUTION_STATES,
    AuditEvent,
    ToolExecution,
)

Severity = Literal["critical", "warning", "info"]

#: Design 10.4 alerts on "磁盘低于阈值". 10% free, or 1 GiB, whichever is larger:
#: a percentage alone is useless on a large disk and a fixed size is useless on
#: a small one.
DISK_FREE_MIN_RATIO: Final[float] = 0.10
DISK_FREE_MIN_BYTES: Final[int] = 1024 * 1024 * 1024

#: A WAL this size means checkpointing has stopped, which is a slow-motion
#: outage: reads keep working while the file grows until the disk does not.
WAL_WARN_BYTES: Final[int] = 64 * 1024 * 1024

#: How long a non-terminal execution may sit before it is stuck rather than busy.
#: Generous: the reconciler's own deadline is far shorter, so anything still
#: here has already outlived its recovery path.
STUCK_EXECUTION_MINUTES: Final[int] = 60

#: A backup older than this is stale. The backup timer fires daily, so two days
#: means a run was missed *and* the catch-up (Persistent=true) did not happen --
#: the box was down through the window and is still down, or restic is failing.
BACKUP_STALE_AFTER_HOURS: Final[int] = 48

@dataclass(frozen=True)
class Finding:
    """One thing worth saying. `code` is stable; `detail` is safe to print."""

    code: str
    severity: Severity
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass(frozen=True)
class DerivedFacts:
    """What the databases and the filesystem say right now."""

    execution_states: dict[str, int] = field(default_factory=dict)
    stuck_executions: int = 0
    broken_audit_events: int = 0
    audit_event_count: int = 0
    schema_drift_events: int = 0
    wal_bytes: dict[str, int] = field(default_factory=dict)
    disk_free_bytes: int | None = None
    disk_total_bytes: int | None = None
    backup_last_success_at: datetime | None = None
    backup_age_hours: float | None = None
    backup_monitor_started_at: datetime | None = None
    backup_monitor_age_hours: float | None = None
    backup_marker_configured: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_states": dict(sorted(self.execution_states.items())),
            "stuck_executions": self.stuck_executions,
            "broken_audit_events": self.broken_audit_events,
            "audit_event_count": self.audit_event_count,
            "schema_drift_events": self.schema_drift_events,
            "wal_bytes": dict(sorted(self.wal_bytes.items())),
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "backup_last_success_at": (
                None
                if self.backup_last_success_at is None
                else to_rfc3339(self.backup_last_success_at)
            ),
            "backup_age_hours": self.backup_age_hours,
            "backup_monitor_started_at": (
                None
                if self.backup_monitor_started_at is None
                else to_rfc3339(self.backup_monitor_started_at)
            ),
            "backup_monitor_age_hours": self.backup_monitor_age_hours,
        }


def execution_state_counts(session: Session) -> dict[str, int]:
    """The tool state distribution design 10.4 asks for, straight from the row."""
    rows = session.execute(
        select(ToolExecution.state, func.count()).group_by(ToolExecution.state)
    ).all()
    return {state: count for state, count in rows}


def stuck_execution_count(
    session: Session, *, now, minutes: int = STUCK_EXECUTION_MINUTES
) -> int:
    """Non-terminal executions older than `minutes`.

    Deliberately counts by *age*, not by state name. A new unfinished state
    added later is automatically covered, which is the opposite of an
    allowlist that silently stops matching the thing it was written for.
    """
    from datetime import timedelta

    cutoff = now - timedelta(minutes=minutes)
    return int(
        session.execute(
            select(func.count())
            .select_from(ToolExecution)
            .where(
                ToolExecution.state.not_in(sorted(TERMINAL_EXECUTION_STATES)),
                ToolExecution.updated_at < cutoff,
            )
        ).scalar_one()
    )


def wal_size_bytes(database: Path) -> int:
    """Size of the `-wal` sidecar, or 0 when there is none."""
    wal = database.with_name(database.name + "-wal")
    try:
        return wal.stat().st_size
    except FileNotFoundError:
        return 0


def disk_free(path: Path) -> tuple[int, int]:
    """Return filesystem capacity, or raise so the monitor exits cannot-check."""
    usage = shutil.disk_usage(path)
    return usage.free, usage.total


def backup_last_success(marker: Path) -> datetime | None:
    """Read the backup-success marker, or return None when no backup ran.

    The marker is written by `deploy/backup.sh` only after `restic check`
    passes, so its presence means a backup landed in OSS and verified there --
    not merely that the script started. Its content is one RFC 3339 UTC
    timestamp; the file mtime is not used, because a copy or a restore drill
    would refresh the mtime and lie about when the last *real* backup happened.

    A missing marker is not an error here: the caller decides whether the
    absence is "never backed up" (info) or "stale" (warning) based on the age.
    A marker that exists but cannot be parsed is a real fault and raises, so the
    monitor exits cannot-check rather than silently treating a corrupted
    witness as "no backup yet".
    """
    try:
        text = marker.read_text().strip()
    except FileNotFoundError:
        return None
    return parse_rfc3339(text)


def backup_monitor_started_at(marker: Path) -> datetime:
    """Read the required monitoring baseline created once by install.sh.

    Unlike the success marker, absence is a deployment fault: without a stable
    start instant a backup that has never succeeded can remain "fresh install"
    forever. Let FileNotFoundError or a parse error propagate so the CLI exits
    cannot-check rather than silently disabling the age transition.
    """
    return parse_rfc3339(marker.read_text().strip())


def active_schema_drift_count(session: Session) -> int:
    """Count recovery traces whose latest schema state is still blocked."""
    active: set[str] = set()
    rows = session.execute(
        select(AuditEvent.trace_id, AuditEvent.event_type)
        .where(
            AuditEvent.event_type.in_(
                (SCHEMA_DRIFT_BLOCKED_EVENT, SCHEMA_DRIFT_RESUMED_EVENT)
            )
        )
        .order_by(AuditEvent.sequence)
    ).all()
    for trace_id, event_type in rows:
        if event_type == SCHEMA_DRIFT_BLOCKED_EVENT:
            active.add(trace_id)
        else:
            active.discard(trace_id)
    return len(active)


def collect(
    *,
    finance_session: Session | None,
    agent_session: Session | None,
    databases: dict[str, Path],
    disk_path: Path,
    now,
    backup_marker: Path | None = None,
    backup_monitor_start: Path | None = None,
) -> DerivedFacts:
    """Read every durable fact without writing.

    The two backup paths are one contract: callers either configure both, or
    neither. The success marker may be absent before the first backup; the
    monitoring-start marker is required so that absence can age into an alert.
    """
    if (backup_marker is None) != (backup_monitor_start is None):
        raise ValueError(
            "backup_marker and backup_monitor_start must be configured together"
        )
    states: dict[str, int] = {}
    stuck = 0
    broken = 0
    audit_total = 0
    drift = 0
    if finance_session is not None:
        states = execution_state_counts(finance_session)
        stuck = stuck_execution_count(finance_session, now=now)
        broken = len(verify_audit_chain(finance_session))
        audit_total = int(
            finance_session.execute(
                select(func.count()).select_from(AuditEvent)
            ).scalar_one()
        )
        drift = active_schema_drift_count(finance_session)
    free, total = disk_free(disk_path)
    last_backup = backup_last_success(backup_marker) if backup_marker else None
    monitor_started = (
        backup_monitor_started_at(backup_monitor_start)
        if backup_monitor_start
        else None
    )
    for label, moment in (
        ("backup-success marker", last_backup),
        ("backup monitoring-start marker", monitor_started),
    ):
        if moment is not None and moment > now:
            raise ValueError(f"{label} is in the future")
    age_hours: float | None = None
    if last_backup is not None:
        age_hours = (now - last_backup).total_seconds() / 3600.0
    monitor_age_hours: float | None = None
    if monitor_started is not None:
        monitor_age_hours = (now - monitor_started).total_seconds() / 3600.0
    return DerivedFacts(
        execution_states=states,
        stuck_executions=stuck,
        broken_audit_events=broken,
        audit_event_count=audit_total,
        schema_drift_events=drift,
        wal_bytes={name: wal_size_bytes(path) for name, path in databases.items()},
        disk_free_bytes=free,
        disk_total_bytes=total,
        backup_last_success_at=last_backup,
        backup_age_hours=age_hours,
        backup_monitor_started_at=monitor_started,
        backup_monitor_age_hours=monitor_age_hours,
        backup_marker_configured=backup_marker is not None,
    )


def evaluate(facts: DerivedFacts) -> list[Finding]:
    """Turn facts into findings. Order is severity first, then code.

    Every rule here maps to a line in design 10.4's 立即告警 list. Rules that
    cannot be evaluated yet are not silently skipped -- see
    `missing_capabilities`.
    """
    findings: list[Finding] = []

    unknown = facts.execution_states.get("commit_unknown", 0)
    if unknown:
        findings.append(
            Finding(
                "write_commit_unknown",
                "critical",
                f"{unknown} write(s) in commit_unknown: a row may exist in "
                "Feishu that this system cannot account for",
            )
        )

    review = facts.execution_states.get("needs_manual_review", 0)
    if review:
        findings.append(
            Finding(
                "write_needs_manual_review",
                "critical",
                f"{review} write(s) parked for manual review, including "
                "read-back mismatches",
            )
        )

    if facts.broken_audit_events:
        findings.append(
            Finding(
                "audit_chain_broken",
                "critical",
                f"{facts.broken_audit_events} audit event(s) no longer match "
                "the chain: the trail has been altered or truncated",
            )
        )

    if facts.schema_drift_events:
        findings.append(
            Finding(
                "schema_drift_blocked_recovery",
                "critical",
                f"{facts.schema_drift_events} recovery path(s) currently refused "
                "because the ledger schema or source no longer matches the "
                "validated config: look at the Feishu Base, not the reconciler",
            )
        )

    if facts.stuck_executions:
        findings.append(
            Finding(
                "execution_stuck",
                "warning",
                f"{facts.stuck_executions} execution(s) non-terminal for over "
                f"{STUCK_EXECUTION_MINUTES} minutes; the reconciler's own "
                "deadline is far shorter, so these outlived their recovery",
            )
        )

    if facts.disk_free_bytes is not None and facts.disk_total_bytes:
        floor = max(
            DISK_FREE_MIN_BYTES, int(facts.disk_total_bytes * DISK_FREE_MIN_RATIO)
        )
        if facts.disk_free_bytes < floor:
            findings.append(
                Finding(
                    "disk_low",
                    "critical",
                    f"{facts.disk_free_bytes // (1024 * 1024)} MiB free is below "
                    f"the {floor // (1024 * 1024)} MiB floor; SQLite writes fail "
                    "closed when the disk does",
                )
            )

    for name, size in sorted(facts.wal_bytes.items()):
        if size > WAL_WARN_BYTES:
            findings.append(
                Finding(
                    "wal_large",
                    "warning",
                    f"{name} WAL is {size // (1024 * 1024)} MiB; checkpointing "
                    "has probably stopped",
                )
            )

    if facts.backup_marker_configured and facts.backup_last_success_at is None:
        # The observer was pointed at a marker file, and that file does not
        # exist. The backup timer is wired (DEV-036), so this means no backup
        # has ever succeeded on this box -- not that the check is unwired.
        # The immutable monitoring-start timestamp distinguishes a fresh install
        # from a backup that has failed through multiple daily windows.
        overdue = (
            facts.backup_monitor_age_hours is not None
            and facts.backup_monitor_age_hours > BACKUP_STALE_AFTER_HOURS
        )
        findings.append(
            Finding(
                "backup_never_succeeded",
                "warning" if overdue else "info",
                (
                    "no backup-success marker after the 48-hour startup grace; "
                    "the daily backup has never landed a verified snapshot in OSS"
                    if overdue
                    else "no backup-success marker yet; the 48-hour startup grace "
                    "has not elapsed"
                ),
            )
        )
    elif facts.backup_marker_configured and facts.backup_age_hours is not None and (
        facts.backup_age_hours > BACKUP_STALE_AFTER_HOURS
    ):
        days = int(facts.backup_age_hours // 24)
        findings.append(
            Finding(
                "backup_stale",
                "warning",
                f"last verified backup was {days} day(s) ago; the daily timer "
                "missed a run and its Persistent catch-up did not fire -- the "
                "box was down through the window, or restic is failing",
            )
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.severity], f.code))
    return findings


def missing_capabilities() -> list[Finding]:
    """What this report structurally cannot see yet, said out loud.

    A monitor that is silent about the thing it cannot measure is worse than one
    that has no rule at all: the silence is indistinguishable from health. The
    entry below disappears when its blocking task lands.
    """
    return [
        Finding(
            "push_metrics_unwired",
            "info",
            "push_send_total is declared but nothing emits it: DEV-028's push "
            "half is unwritten. A zero here means 'nothing tried'",
        ),
    ]


def worst_severity(findings: Sequence[Finding]) -> Severity | None:
    for severity in ("critical", "warning", "info"):
        if any(f.severity == severity for f in findings):
            return severity  # type: ignore[return-value]
    return None
