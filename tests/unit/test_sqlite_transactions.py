"""What a transaction means on this engine, pinned.

Both services were built on the assumption that `Session.rollback()` undoes
everything the failed unit of work did, and that `begin_nested()` is a
savepoint. Neither was true: pysqlite opens a transaction before DML but not
before `SAVEPOINT`, so a released nested block committed on the spot and an
outer rollback could not reach it. `create_database_engine` now hands
transaction control to SQLAlchemy, and these tests are what keep that fixed --
the defect was invisible to every other test in the suite, because a single
session always sees its own writes.

The second half pins the cost of having real transactions: a session that has
read cannot write once another session has committed. SQLite reports that
immediately rather than waiting, so the unit of work has to run again against
fresh state, which is what `run_write_transaction` does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ApiRequest, Device, Operation
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.sqlite import is_snapshot_conflict, run_write_transaction


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "transactions.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    with factory() as setup:
        setup.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="T",
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        setup.commit()
    yield factory
    engine.dispose()


def _request(session, request_id: str) -> None:
    session.add(
        ApiRequest(
            request_id=request_id,
            device_id="dev-1",
            client_request_id=request_id,
            request_fingerprint="fp",
            received_at=NOW,
        )
    )


def _operation(session, operation_id: str, request_id: str) -> Operation:
    operation = Operation(
        operation_id=operation_id,
        request_id=request_id,
        trace_id="trace",
        idempotency_key=operation_id,
        state="accepted",
        state_version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(operation)
    return operation


# -- savepoints ------------------------------------------------------------


def test_a_released_savepoint_is_undone_by_an_outer_rollback(sessions) -> None:
    """The defect this engine setting exists to fix.

    Before it, the row below survived the rollback and was visible from a fresh
    session, so a failed request left rows behind that nothing accounted for.
    """
    with sessions() as session:
        with session.begin_nested():
            _request(session, "req-1")
        session.rollback()

    with sessions() as check:
        assert check.query(ApiRequest).count() == 0


def test_a_failure_inside_a_savepoint_discards_only_that_block(sessions) -> None:
    with sessions() as session:
        _request(session, "req-outer")
        session.flush()
        with pytest.raises(RuntimeError):
            with session.begin_nested():
                _request(session, "req-inner")
                session.flush()
                raise RuntimeError("boom")
        session.commit()

    with sessions() as check:
        assert {row.request_id for row in check.query(ApiRequest).all()} == {
            "req-outer"
        }


def test_a_committed_unit_of_work_is_durable(sessions) -> None:
    with sessions() as session:
        with session.begin_nested():
            _request(session, "req-kept")
        session.commit()

    with sessions() as check:
        assert check.query(ApiRequest).count() == 1


# -- the cost of real transactions ----------------------------------------


def test_a_read_then_write_session_loses_its_snapshot_to_a_committed_writer(
    sessions,
) -> None:
    """The failure `run_write_transaction` exists to absorb.

    This is not a lock that clears by waiting: SQLite refuses to upgrade a read
    snapshot that is no longer current, immediately.
    """
    with sessions() as setup:
        _request(setup, "req-1")
        _operation(setup, "op-1", "req-1")
        setup.commit()

    reader = sessions()
    try:
        reader.get(Operation, "op-1")  # takes the snapshot

        with sessions() as writer:
            writer.execute(
                text(
                    "UPDATE operations SET state_version = 2 "
                    "WHERE operation_id = 'op-1'"
                )
            )
            writer.commit()

        with pytest.raises(Exception) as raised:
            reader.execute(
                text(
                    "UPDATE operations SET state = 'interpreting' "
                    "WHERE operation_id = 'op-1'"
                )
            )
        assert is_snapshot_conflict(raised.value)
    finally:
        reader.rollback()
        reader.close()


def test_the_retry_reruns_the_unit_against_fresh_state(sessions) -> None:
    with sessions() as setup:
        _request(setup, "req-1")
        _operation(setup, "op-1", "req-1")
        setup.commit()

    attempts: list[int] = []
    reader = sessions()
    try:
        def work() -> int:
            attempts.append(len(attempts) + 1)
            observed = reader.execute(
                text(
                    "SELECT state_version FROM operations "
                    "WHERE operation_id = 'op-1'"
                )
            ).scalar_one()
            if len(attempts) == 1:
                # Only on the first attempt: another session commits between
                # this read and the write below.
                with sessions() as writer:
                    writer.execute(
                        text(
                            "UPDATE operations SET state_version = 7 "
                            "WHERE operation_id = 'op-1'"
                        )
                    )
                    writer.commit()
            reader.execute(
                text(
                    "UPDATE operations SET state = 'interpreting' "
                    "WHERE operation_id = 'op-1'"
                )
            )
            return observed

        observed = run_write_transaction(reader, work)
    finally:
        reader.close()

    assert attempts == [1, 2]
    # The second attempt read the winner's value, not the stale one.
    assert observed == 7
    with sessions() as check:
        row = check.get(Operation, "op-1")
        assert row.state == "interpreting"
        assert row.state_version == 7


def test_the_retry_does_not_swallow_an_ordinary_failure(sessions) -> None:
    calls: list[int] = []

    with sessions() as session:
        def work() -> None:
            calls.append(1)
            raise AppError(
                ErrorCode.INVALID_ARGUMENT, internal_detail="not a race"
            )

        with pytest.raises(AppError) as raised:
            run_write_transaction(session, work)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert calls == [1]


def test_the_retry_is_bounded_and_reports_the_conflict_it_cannot_clear(
    sessions,
) -> None:
    """A permanent conflict surfaces rather than looping."""
    with sessions() as setup:
        _request(setup, "req-1")
        _operation(setup, "op-1", "req-1")
        setup.commit()

    calls: list[int] = []
    reader = sessions()
    try:
        def work() -> None:
            calls.append(1)
            reader.get(Operation, "op-1")
            with sessions() as writer:
                writer.execute(
                    text(
                        "UPDATE operations SET state_version = state_version + 1 "
                        "WHERE operation_id = 'op-1'"
                    )
                )
                writer.commit()
            reader.execute(
                text(
                    "UPDATE operations SET state = 'interpreting' "
                    "WHERE operation_id = 'op-1'"
                )
            )

        with pytest.raises(Exception) as raised:
            run_write_transaction(reader, work, attempts=3)
    finally:
        reader.close()

    assert is_snapshot_conflict(raised.value)
    assert calls == [1, 1, 1]
