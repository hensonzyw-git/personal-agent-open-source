"""DAL-011: real dual-session/barrier race for one approval.

The sequential replay in ``test_approval_service`` drives the two
``concurrent_consume`` operations one after the other, so it cannot see the
snapshot-conflict path: the loser never contends for SQLite's write lock. This
test drives the two operations simultaneously across a thread barrier, each
through its own session, and asserts the loser reaches the approval CAS —
surfacing as ``APPROVAL_INVALID`` after the snapshot conflict is retried — not
``VERSION_CONFLICT`` and not an uncaught "database is locked".

The outcome is deterministic under every interleaving: the winner's token-gate
consume commits, and the loser's consume CAS finds the row consumed (either
immediately, or after ``run_write_transaction`` retries the snapshot conflict),
so exactly one ``APPLIED`` and one ``APPROVAL_INVALID`` always result.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy import text

import personal_agent_core.sqlite as sqlite_module
import personal_agent_dal.machine.engine as engine_module
from personal_agent_dal.machine.engine import TransitionCommand, apply_transition
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine

from tests.dal.approval_executor import _build_command, _seed
from tests.dal.contract_loader import FrozenContracts


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_concurrent_consume_real_race(
    contracts: FrozenContracts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions race the frozen concurrent_consume fixture across a barrier."""
    variant = next(
        v for v in contracts.variants("DAL-T-APP-001")
        if v.variant_id == "concurrent_consume"
    )
    fixture = variant.fixture.body
    ops = fixture["operation_sequence"]
    assert len(ops) == 2
    target = ops[0]["input"]["target"]

    database = tmp_path / "app-concurrent-race.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    server_now = _seed(engine, fixture)

    commands = [_build_command(op, target) for op in ops]
    gate_barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()
    gated_threads: set[int] = set()
    snapshot_conflicts = 0

    original_gate = engine_module._apply_approval_token_gate

    def synchronized_gate(ctx, registry):  # type: ignore[no-untyped-def]
        """Hold both transactions after their aggregate read, before the CAS."""
        thread_id = threading.get_ident()
        with lock:
            first_gate_entry = thread_id not in gated_threads
            gated_threads.add(thread_id)
        if first_gate_entry:
            gate_barrier.wait(timeout=10)
        return original_gate(ctx, registry)

    original_is_snapshot_conflict = sqlite_module.is_snapshot_conflict

    def record_snapshot_conflict(error: BaseException) -> bool:
        nonlocal snapshot_conflicts
        matched = original_is_snapshot_conflict(error)
        if matched:
            with lock:
                snapshot_conflicts += 1
        return matched

    monkeypatch.setattr(engine_module, "_apply_approval_token_gate", synchronized_gate)
    monkeypatch.setattr(
        sqlite_module, "is_snapshot_conflict", record_snapshot_conflict
    )

    def worker(command: TransitionCommand) -> None:
        try:
            outcome = apply_transition(
                engine, command, facts=GuardFacts({}), now=server_now
            )
            with lock:
                results.append(outcome.receipt_code)
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(f"EXC:{type(exc).__name__}:{exc}")

    threads = [threading.Thread(target=worker, args=(cmd,)) for cmd in commands]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads), "race threads hung"

    # Bound to the frozen oracle's receipt multiset, not a hand-written guess.
    expected_codes: list[str] = []
    for receipt in variant.oracle.body["expected_receipts"]:
        expected_codes.extend([receipt["code"]] * receipt["count"])
    assert sorted(results) == sorted(expected_codes), results
    assert snapshot_conflicts >= 1, "the test did not exercise snapshot retry"

    # Persisted state matches the frozen oracle: feature moved to its final
    # snapshot with a +1 version, and exactly one approval was consumed.
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT state, version FROM features WHERE feature_id = :id"
            ).bindparams(id=target["entity_id"])
        ).first()
        consume_count = conn.execute(
            text(
                "SELECT count(*) FROM approvals "
                "WHERE consumed_by_command_id IS NOT NULL "
                "AND consumed_by_command_id NOT LIKE 'revoked:%'"
            )
        ).scalar_one()
    engine.dispose()

    assert row is not None, "feature row missing after race"
    assert row == (
        variant.oracle.body["expected_final_snapshot"]["state"],
        target["version"] + 1,
    ), row
    assert consume_count == 1, consume_count
