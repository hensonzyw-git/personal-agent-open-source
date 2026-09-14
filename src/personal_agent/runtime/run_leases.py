"""Persistent execution leases; no external effects inside these transactions.

The Host must use check_in_transaction in the same transaction as accepting a
result. A standalone check is diagnostic, never a reusable submit permission.
"""
from dataclasses import dataclass

from sqlalchemy import update

from personal_agent.runtime.run_store import RunBudgetStore, RunStateError, TASK_LIMITS


@dataclass(frozen=True)
class RunLease:
    operation_id: str
    owner: str
    fence: int


class RunLeaseStore(RunBudgetStore):
    def _task_authority(self, session, run):
        if run['task_id'] is None:
            return None
        task = self._one(session, self.tasks, self.tasks.c.task_id, run['task_id'])
        if (task['revision'] != run['expected_task_revision'] or
                task['active_operation_id'] != run['operation_id'] or
                task['status'] != 'active' or task['write_slot'] is not None):
            raise RunStateError('stale_task_authority')
        return task

    def _observe(self, session, run, task, now_ms):
        elapsed = now_ms - run['started_ms']
        if task is not None:
            total = task['active_ms'] + elapsed - run['active_ms']
            if total + self._held(session, task['task_id'])['time_ms'] > TASK_LIMITS['time_ms']:
                raise RunStateError('bound_budget')
            session.execute(update(self.tasks).where(self.tasks.c.task_id == task['task_id']).values(active_ms=total))
        session.execute(update(self.runs).where(self.runs.c.operation_id == run['operation_id']).values(active_ms=elapsed))

    @staticmethod
    def _ttl(ttl_ms):
        if type(ttl_ms) is not int or ttl_ms <= 0:
            raise RunStateError('invalid_lease_duration')

    def acquire(self, operation_id, *, owner, now_ms, ttl_ms):
        self._ttl(ttl_ms)
        if not isinstance(owner, str) or not owner.strip():
            raise RunStateError('invalid_lease_owner')
        def work(session):
            run = self._one(session, self.runs, self.runs.c.operation_id, operation_id)
            self._live(run, now_ms)
            if run['lease_until_ms'] is not None and run['lease_until_ms'] > now_ms:
                raise RunStateError('lease_busy')
            task = self._task_authority(session, run)
            self._observe(session, run, task, now_ms)
            fence = run['fence'] + 1
            session.execute(update(self.runs).where(self.runs.c.operation_id == operation_id).values(
                lease_owner=owner, lease_until_ms=min(now_ms + ttl_ms, run['deadline_ms']), fence=fence))
            return RunLease(operation_id, owner, fence)
        return self._write(work)

    def check_in_transaction(self, session, lease, *, now_ms):
        run = self._one(session, self.runs, self.runs.c.operation_id, lease.operation_id)
        self._live(run, now_ms)
        if (run['lease_owner'] != lease.owner or run['fence'] != lease.fence or
                run['lease_until_ms'] is None or now_ms >= run['lease_until_ms']):
            raise RunStateError('stale_run_lease')
        task = self._task_authority(session, run)
        self._observe(session, run, task, now_ms)
        return run

    def check(self, lease, *, now_ms):
        self._write(lambda session: self.check_in_transaction(session, lease, now_ms=now_ms))

    def renew(self, lease, *, now_ms, ttl_ms):
        self._ttl(ttl_ms)
        def work(session):
            run = self.check_in_transaction(session, lease, now_ms=now_ms)
            session.execute(update(self.runs).where(self.runs.c.operation_id == lease.operation_id).values(
                lease_until_ms=min(now_ms + ttl_ms, run['deadline_ms'])))
            return lease
        return self._write(work)
