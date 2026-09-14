"""Lease authority failures across independent SQLite workers."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import update

from test_run_budget_store import setup, task
from personal_agent.runtime.run_store import RunStateError
from personal_agent.runtime.run_leases import RunLeaseStore


def test_only_one_worker_acquires_live_lease(setup):
    store, run, factory = setup
    op = run('race')
    barrier = Barrier(2)
    def acquire(owner):
        barrier.wait()
        try:
            return RunLeaseStore(factory).acquire(op, owner=owner, now_ms=100, ttl_ms=1000)
        except RunStateError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(acquire, ['a', 'b']))
    assert sum(x is not None for x in results) == 1


def test_expired_owner_and_replayed_fence_cannot_renew_or_accept(setup):
    store, run, factory = setup
    op = run('takeover')
    leases = RunLeaseStore(factory)
    old = leases.acquire(op, owner='a', now_ms=100, ttl_ms=1000)
    current = leases.acquire(op, owner='b', now_ms=1100, ttl_ms=1000)
    assert current.fence > old.fence
    for action in (leases.check, leases.renew):
        with pytest.raises(RunStateError):
            action(old, now_ms=1101, **({'ttl_ms': 1000} if action == leases.renew else {}))
    leases.check(current, now_ms=1101)


def test_lease_deadline_and_clock_floor_survive_reopen(setup):
    store, run, factory = setup
    op = run('clock')
    leases = RunLeaseStore(factory)
    lease = leases.acquire(op, owner='a', now_ms=59000, ttl_ms=5000)
    assert store.snapshot(op)['lease_until_ms'] == 60000
    for now in (1000, 60000):
        with pytest.raises(RunStateError):
            RunLeaseStore(factory).check(lease, now_ms=now)


def test_bound_task_revision_and_owner_are_part_of_authority(setup):
    store, run, factory = setup
    old = task(store)
    op = run('bound')
    store.reserve_prebind(op, [old], now_ms=1)
    store.bind(op, task_id=old, now_ms=2, sealed_goal=b'x', sealed_constraints=b'x')
    leases = RunLeaseStore(factory)
    lease = leases.acquire(op, owner='a', now_ms=3, ttl_ms=1000)
    leases.check(lease, now_ms=4)
    with factory() as session:
        session.execute(update(store.tasks).where(store.tasks.c.task_id == old).values(revision=2))
        session.commit()
    with pytest.raises(RunStateError):
        leases.check(lease, now_ms=5)


def test_same_owner_cannot_reacquire_unexpired_lease(setup):
    store, run, factory = setup
    op = run('same-owner')
    leases = RunLeaseStore(factory)
    lease = leases.acquire(op, owner='a', now_ms=0, ttl_ms=1000)
    with pytest.raises(RunStateError):
        leases.acquire(op, owner='a', now_ms=1, ttl_ms=1000)
    renewed = leases.renew(lease, now_ms=500, ttl_ms=1000)
    assert renewed == lease
    assert store.snapshot(op)['lease_until_ms'] == 1500


def test_budget_mutations_cannot_bypass_owned_lease(setup):
    store, run, factory = setup
    op = run('budget-guard')
    leases = RunLeaseStore(factory)
    old = leases.acquire(op, owner='a', now_ms=0, ttl_ms=10)
    current = leases.acquire(op, owner='b', now_ms=10, ttl_ms=100)
    for token in (None, old):
        with pytest.raises(RunStateError):
            store.reserve_prebind(op, [], now_ms=11, lease=token)
    store.reserve_prebind(op, [], now_ms=11, lease=current)
    with pytest.raises(RunStateError):
        store.recover_prebind(op)
    with pytest.raises(RunStateError):
        store.bind(op, task_id=None, new_task_id='new', now_ms=12,
            sealed_goal=b'x', sealed_constraints=b'x')
    from test_agent_storage import SEALED
    store.bind(op, task_id=None, new_task_id='new', now_ms=12,
        sealed_goal=SEALED, sealed_constraints=SEALED, lease=current)
    with pytest.raises(RunStateError):
        store.reserve_bound(op, now_ms=13, read_add=1, lease=old)
    store.reserve_bound(op, now_ms=13, read_add=1, lease=current)


def test_expired_prebind_recovery_fences_previous_worker(setup):
    store, run, factory = setup
    op = run('recover')
    leases = RunLeaseStore(factory)
    lease = leases.acquire(op, owner='a', now_ms=0, ttl_ms=10)
    store.reserve_prebind(op, [], now_ms=1, lease=lease)
    with pytest.raises(RunStateError):
        store.recover_prebind(op, now_ms=9)
    store.recover_prebind(op, now_ms=10)
    assert store.snapshot(op)['fence'] > lease.fence
    with pytest.raises(RunStateError):
        leases.check(lease, now_ms=11)
