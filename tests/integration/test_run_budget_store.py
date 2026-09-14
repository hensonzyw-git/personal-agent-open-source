"""T2 prebinding budget failures on real SQLite transactions and workers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import update

from personal_agent.storage.engine import create_all, create_database_engine
from personal_agent.storage.models import Base, Conversation
from personal_agent.runtime.run_store import RunBudgetStore, RunStateError
from test_agent_storage import make_device, NOW, SEALED
from personal_agent.storage.engine import session_factory
from personal_agent.api.operation_store import open_operation


@pytest.fixture()
def setup(tmp_path):
    engine = create_database_engine(tmp_path / "run.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    with factory() as session:
        session.add(make_device())
        session.add(Conversation(conversation_id="timeline", created_at=NOW))
        session.commit()
    store = RunBudgetStore(factory)
    def run(name, now=0):
        with factory() as session:
            opened = open_operation(session, device_id="dev-1", client_request_id=name,
                request_fingerprint=name, now=NOW)
            session.commit()
            op = opened.operation.operation_id
        store.start_message(op, timeline_id="timeline", now_ms=now, sealed_input=SEALED)
        return op
    yield store, run, factory
    engine.dispose()


def task(store, task_id="old", **usage):
    store.create_task(task_id, timeline_id="timeline", sealed_goal=SEALED,
        sealed_constraints=SEALED, **usage)
    return task_id


@pytest.mark.parametrize("remaining", [0, 1000])
def test_low_time_old_task_never_shortens_message_deadline(setup, remaining):
    store, run, factory = setup
    old = task(store, active_ms=180000-remaining)
    op = run("new-message")
    candidates = store.reserve_prebind(op, [old], now_ms=500)
    assert candidates[0].resumable is False
    assert candidates[0].control_available is True
    assert store.snapshot(op)["deadline_ms"] == 60000
    bound = store.bind(op, task_id=None, new_task_id="new", now_ms=5000,
        sealed_goal=SEALED, sealed_constraints=SEALED)
    assert bound.accepted and bound.deadline_ms == 60000
    assert store.task_snapshot(old)["active_ms"] == 180000-remaining
    assert store.task_snapshot("new")["llm_used"] == 1


def test_two_workers_last_model_slot_only_one_candidate_can_resume(setup):
    store, run, _ = setup
    old = task(store, llm_used=11)
    a, b = run("a"), run("b")
    barrier = Barrier(2)
    def reserve(op):
        barrier.wait()
        return store.reserve_prebind(op, [old], now_ms=0)[0].resumable
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [a, b]))
    assert sorted(results) == [False, True]
    assert store.task_snapshot(old)["llm_used"] == 11
    assert store.held(old)["llm"] == 1
    assert store.snapshot(a)["deadline_ms"] == store.snapshot(b)["deadline_ms"] == 60000


def test_late_discovery_must_cover_all_prior_costs(setup):
    store, run, _ = setup
    old = task(store, llm_used=11)
    op = run("discovery")
    store.reserve_prebind(op, [], now_ms=100)
    store.reserve_prebind(op, [], now_ms=1000, llm_add=0, read_add=1)
    found = store.reserve_prebind(op, [old], now_ms=2000)
    assert not found[0].resumable and found[0].control_available
    assert store.snapshot(op)["llm_used"] == 2
    assert store.snapshot(op)["read_used"] == 1
    assert store.snapshot(op)["deadline_ms"] == 60000


def test_selected_old_task_is_charged_once_and_unselected_hold_released(setup):
    store, run, _ = setup
    chosen = task(store, "chosen", llm_used=11)
    other = task(store, "other")
    op = run("choose")
    store.reserve_prebind(op, [chosen, other], now_ms=0)
    args = dict(task_id=chosen, now_ms=2000, sealed_goal=SEALED, sealed_constraints=SEALED)
    bound = store.bind(op, **args)
    assert bound.accepted and bound.deadline_ms == 60000
    assert store.task_snapshot(chosen)["llm_used"] == 12
    assert store.task_snapshot(chosen)["active_ms"] == 2000
    assert store.held(other)["llm"] == 0
    assert store.bind(op, **args) == bound
    assert store.task_snapshot(chosen)["llm_used"] == 12
    with pytest.raises(RunStateError):
        store.reserve_bound(op, now_ms=2500, llm_add=1)


def test_binding_cannot_switch_to_another_task_or_reset_budget(setup):
    store, run, _ = setup
    a, b = task(store, "a"), task(store, "b")
    op = run("bind")
    store.reserve_prebind(op, [a, b], now_ms=0)
    store.bind(op, task_id=a, now_ms=1, sealed_goal=SEALED, sealed_constraints=SEALED)
    with pytest.raises(RunStateError):
        store.bind(op, task_id=b, now_ms=2, sealed_goal=SEALED, sealed_constraints=SEALED)


def test_revision_conflict_still_charges_actual_attempt(setup):
    store, run, factory = setup
    old = task(store)
    op = run("conflict")
    store.reserve_prebind(op, [old], now_ms=0)
    tasks = Base.metadata.tables["agent_tasks"]
    with factory() as session:
        session.execute(update(tasks).where(tasks.c.task_id == old).values(revision=2))
        session.commit()
    result = store.bind(op, task_id=old, now_ms=1000, sealed_goal=SEALED, sealed_constraints=SEALED)
    assert not result.accepted
    assert store.task_snapshot(old)["llm_used"] == 1
    assert store.snapshot(op)["state"] == "failed"


def test_expired_prebinding_is_not_a_fresh_sixty_seconds(setup):
    store, run, _ = setup
    op = run("expired")
    with pytest.raises(RunStateError):
        store.reserve_prebind(op, [], now_ms=60000)


def test_orphan_recovery_charges_once_and_keeps_deadline(setup):
    store, run, _ = setup
    old = task(store)
    op = run("orphan")
    store.reserve_prebind(op, [old], now_ms=10)
    store.recover_prebind(op)
    store.recover_prebind(op)
    assert store.task_snapshot(old)["llm_used"] == 1
    assert store.task_snapshot(old)["active_ms"] == 60000
    assert store.held(old)["llm"] == 0
    assert store.snapshot(op)["deadline_ms"] == 60000
    assert store.snapshot(op)["state"] == "failed"


def test_attempt_key_replay_does_not_charge_twice_and_conflicting_key_refuses(setup):
    store, run, _ = setup
    old = task(store)
    op = run("retry")
    for now in (1, 2):
        store.reserve_prebind(op, [old], now_ms=now, attempt_key="same")
    assert store.snapshot(op)["llm_used"] == 1
    assert store.held(old)["llm"] == 1
    with pytest.raises(RunStateError):
        store.reserve_prebind(op, [], now_ms=3, attempt_key="same")


def test_degraded_candidate_retains_prior_hold_for_crash_accounting(setup):
    store, run, _ = setup
    old = task(store, llm_used=11)
    op = run("degrade")
    assert store.reserve_prebind(op, [old], now_ms=0)[0].resumable
    assert not store.reserve_prebind(op, [old], now_ms=1)[0].resumable
    assert store.held(old)["llm"] == 1
    store.recover_prebind(op)
    assert store.task_snapshot(old)["llm_used"] == 12
    assert store.snapshot(op)["llm_used"] == 2


def test_clock_cannot_move_backward_to_refund_charged_activity(setup):
    store, run, _ = setup
    op = run("clock")
    store.reserve_prebind(op, [], now_ms=0)
    store.bind(op, task_id=None, new_task_id="new", now_ms=1000,
        sealed_goal=SEALED, sealed_constraints=SEALED)
    with pytest.raises(RunStateError):
        store.reserve_bound(op, now_ms=900, read_add=1)
    assert store.task_snapshot("new")["active_ms"] == 1000


def test_run_replay_keeps_original_deadline(setup):
    store, run, _ = setup
    op = run("accept")
    store.start_message(op, timeline_id="timeline", now_ms=40000, sealed_input=SEALED)
    assert store.snapshot(op)["deadline_ms"] == 60000
