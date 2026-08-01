"""`DEV-034`: a write must fail closed when its audit event cannot be persisted.

Technical design 10.4 ends with one sentence that this file exists to hold up:
"审计或幂等状态无法持久化时，写工具 fail closed；只读可明确降级."

Every `_audit(...)` in `write_path` sits in the same transaction as the state
transition it describes, with the `commit()` after both, so the property looks
true by construction. `CLAUDE.md` §5.1 is explicit that "looks true by
construction" is not evidence at a boundary where the failure modes have not
been enumerated -- and until now nothing anywhere injected an audit failure. A
regression that moved one `_audit` call after its `commit()`, or wrapped it in a
`try/except` to "avoid failing a write over logging", would have been invisible:
every existing test would stay green while the ledger quietly gained rows with
no audit trail.

Two counterparties, deliberately, per the same rule:

- `failing_audit` replaces the `append_audit_event` seam, which buys precision
  -- it can fail exactly one event type and leave the rest working, which is how
  each stage is isolated.
- `test_a_write_cannot_outlive_the_audit_table` removes the real table instead,
  so the failure comes from SQLAlchemy and SQLite rather than from a stub built
  on the same assumptions as the code. If only the seam test existed, a change
  that stopped calling `append_audit_event` at all would pass it.

What "fail closed" means here is specific, and weaker than it first sounds. Once
Feishu has accepted a row, no local failure can un-write it; the guarantee is
that the operation is never *reported* as success and never persists a state
that claims one. Recovering the row is the reconciler's job, and the state it
reads must be an unfinished one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import (
    TERMINAL_EXECUTION_STATES,
    verify_audit_chain,
)
from personal_data_mcp.storage.models import AuditEvent, ToolExecution

from integration.test_finance_write_path import (
    FakeFeishu,
    do_write,
    receipts_of,
    state_of,
)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


class AuditWriteFailed(RuntimeError):
    """Stands in for anything that makes the audit insert impossible."""


@pytest.fixture()
def failing_audit(monkeypatch):
    """Fail `append_audit_event` for one chosen event type, pass the rest through.

    Patching the name *as imported into `write_path`* is deliberate: that is the
    call the write path actually makes, so a refactor that stopped routing
    through it would make these tests fail rather than silently pass.
    """
    from personal_data_mcp.finance import write_path
    from personal_data_mcp.storage import execution_store

    real = execution_store.append_audit_event

    def install(failing_type: str) -> None:
        def append(session, **kwargs):
            if kwargs.get("event_type") == failing_type:
                raise AuditWriteFailed(f"audit insert refused for {failing_type}")
            return real(session, **kwargs)

        monkeypatch.setattr(write_path, "append_audit_event", append)

    return install


def audit_types(sessions) -> list[str]:
    with sessions() as session:
        return [
            event.event_type
            for event in session.query(AuditEvent).order_by(AuditEvent.sequence)
        ]


def execution_row(sessions, key: str = "idem-1") -> ToolExecution | None:
    with sessions() as session:
        return session.get(ToolExecution, key)


# --- before the network: nothing may leave ------------------------------------


def test_a_failed_audit_at_prepared_never_reaches_feishu(
    sessions, failing_audit
) -> None:
    """The strongest case: the write is refused before anything is external.

    `prepared` is the last point at which failing closed is free -- no row
    exists anywhere yet -- so this is the one stage where "fail closed" can mean
    what it sounds like.
    """
    failing_audit("write_prepared")
    fake = FakeFeishu()

    with pytest.raises(AuditWriteFailed):
        asyncio.run(do_write(fake, sessions))

    assert fake.creates == [], "a write went to Feishu without an audit record"
    # The execution row was created in the same transaction as the audit event,
    # so it must be gone too -- otherwise a retry with the same key would be
    # refused as a duplicate of a write that never happened.
    assert execution_row(sessions) is None
    assert audit_types(sessions) == []


# --- after the network: never reported as success -----------------------------


def test_a_failed_audit_after_the_create_is_not_reported_as_success(
    sessions, failing_audit
) -> None:
    """Feishu has the row; the operation must still not claim success.

    The receipt is written in the same transaction as this audit event, so it
    rolls back with it. That is the point: a receipt is evidence a write was
    verified, and evidence that outlived its own audit record would be worth
    less than none.
    """
    failing_audit("write_committed_unverified")
    fake = FakeFeishu()

    with pytest.raises(AuditWriteFailed):
        asyncio.run(do_write(fake, sessions))

    assert len(fake.creates) == 1, "the create should have happened exactly once"
    assert receipts_of(sessions) == []
    # Non-terminal is the property that matters, not the specific name:
    # `reconcile_write` refuses a terminal execution and accepts any other, so a
    # terminal state here would strand a real Feishu row with nothing left to
    # look at it. Asserting the set rather than the string also means renaming a
    # state does not silently weaken this.
    assert state_of(sessions) not in TERMINAL_EXECUTION_STATES


def test_a_failed_audit_at_verification_does_not_report_success(
    sessions, failing_audit
) -> None:
    failing_audit("write_succeeded")
    fake = FakeFeishu()

    with pytest.raises(AuditWriteFailed):
        asyncio.run(do_write(fake, sessions))

    assert state_of(sessions) != "succeeded"
    assert "write_succeeded" not in audit_types(sessions)


# --- the error paths must not lose their own audit either ---------------------


def test_a_failed_audit_on_the_commit_unknown_path_still_refuses(
    sessions, failing_audit
) -> None:
    """Two failures at once: the create fails, then recording that fails.

    Written first as "some exception is raised and the state is not succeeded",
    which defect injection then showed was worth nothing: with `_audit` wrapped
    in `try/except`, the create still fails, an error is still raised, and that
    version passed while the audit record was being silently dropped. The
    assertion has to be about *atomicity* — the state transition and its audit
    event travel together or neither lands — because that is the property the
    swallow-the-error regression actually breaks.
    """
    failing_audit("write_commit_unknown")
    fake = FakeFeishu()
    fake.create_fails_with = httpx.ConnectError("network gone")

    with pytest.raises(Exception):
        asyncio.run(do_write(fake, sessions))

    assert receipts_of(sessions) == []
    assert "write_commit_unknown" not in audit_types(sessions)
    # The transition shares the audit event's transaction, so losing the event
    # must lose the transition too. Reaching `commit_unknown` with no audit
    # record of how it got there is exactly the state design 10.4 forbids.
    assert state_of(sessions) != "commit_unknown"
    assert state_of(sessions) not in TERMINAL_EXECUTION_STATES


# --- the real counterparty ----------------------------------------------------


def test_a_write_cannot_outlive_the_audit_table(sessions) -> None:
    """No stub: the audit table is really gone, so SQLAlchemy really fails.

    This is the test that survives a refactor the seam test would miss -- code
    that stopped calling `append_audit_event` at all would satisfy
    `failing_audit` vacuously and fail here.
    """
    with sessions() as session:
        session.execute(text("DROP TABLE audit_events"))
        session.commit()

    fake = FakeFeishu()
    with pytest.raises(Exception):
        asyncio.run(do_write(fake, sessions))

    assert fake.creates == [], "a write reached Feishu with no audit table at all"
    assert state_of(sessions) != "succeeded"


# --- a rolled-back audit event must not leave a hole in the chain -------------


def test_a_refused_write_leaves_the_chain_intact(
    sessions, failing_audit, monkeypatch
) -> None:
    """The chain must stay verifiable across a failure, not just across success.

    `append_audit_event` reads the current tail to compute `prev_hash`. If a
    failed write committed some events and rolled back others, the next real
    event would chain onto a hash that no longer exists and
    `verify_audit_chain` would report a break for a write that never happened.
    """
    fake = FakeFeishu()
    asyncio.run(do_write(fake, sessions, key="idem-ok", fingerprint="fp-ok"))

    failing_audit("write_committed_unverified")
    with pytest.raises(AuditWriteFailed):
        asyncio.run(do_write(fake, sessions, key="idem-bad", fingerprint="fp-bad"))

    # Put the real writer back, so the next event chains onto whatever the
    # failed write actually left behind rather than onto a patched no-op.
    monkeypatch.undo()
    asyncio.run(do_write(fake, sessions, key="idem-ok2", fingerprint="fp-ok2"))

    with sessions() as session:
        assert verify_audit_chain(session) == []
