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

**What is deliberately absent.** Backup age. `DEV-035` has no backups, so an
age check would either be dead code or -- worse -- report "no backup overdue"
because it found no backups at all. `missing_capabilities()` names it instead,
so the report says what it cannot see rather than staying silent and reading as
health.

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
from pathlib import Path
from typing import Any, Final, Literal, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    wal_bytes: dict[str, int] = field(default_factory=dict)
    disk_free_bytes: int | None = None
    disk_total_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_states": dict(sorted(self.execution_states.items())),
            "stuck_executions": self.stuck_executions,
            "broken_audit_events": self.broken_audit_events,
            "audit_event_count": self.audit_event_count,
            "wal_bytes": dict(sorted(self.wal_bytes.items())),
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
        }


def execution_state_counts(session: Session) -> dict[str, int]:
    """The tool state distribution design 10.4 asks for, straight from the row."""
    rows = session.execute(
        select(ToolExecution.state, func.count()).group_by(ToolExecution.state)
    ).all()
    return {state: count for state, count in rows}


def stuck_execution_count(session: Session, *, now, minutes: int = STUCK_EXECUTION_MINUTES) -> int:
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


def disk_free(path: Path) -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None
    return usage.free, usage.total


def collect(
    *,
    finance_session: Session | None,
    agent_session: Session | None,
    databases: dict[str, Path],
    disk_path: Path,
    now,
) -> DerivedFacts:
    """Read every durable fact. Never writes, never raises on a missing file."""
    states: dict[str, int] = {}
    stuck = 0
    broken = 0
    audit_total = 0
    if finance_session is not None:
        states = execution_state_counts(finance_session)
        stuck = stuck_execution_count(finance_session, now=now)
        broken = len(verify_audit_chain(finance_session))
        audit_total = int(
            finance_session.execute(
                select(func.count()).select_from(AuditEvent)
            ).scalar_one()
        )
    free, total = disk_free(disk_path)
    return DerivedFacts(
        execution_states=states,
        stuck_executions=stuck,
        broken_audit_events=broken,
        audit_event_count=audit_total,
        wal_bytes={name: wal_size_bytes(path) for name, path in databases.items()},
        disk_free_bytes=free,
        disk_total_bytes=total,
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

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.severity], f.code))
    return findings


def missing_capabilities() -> list[Finding]:
    """What this report structurally cannot see yet, said out loud.

    A monitor that is silent about the thing it cannot measure is worse than one
    that has no rule at all: the silence is indistinguishable from health. Both
    entries below disappear when their blocking task lands.
    """
    return [
        Finding(
            "backup_age_unknown",
            "info",
            "no backup age check: DEV-035 is not built, so there are no backups "
            "to age. This is not 'backups are fresh'",
        ),
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
