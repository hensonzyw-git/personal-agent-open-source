"""T2 durable budget ledger. All units are pure DB work with fresh SELECTs.

Prebinding admission conservatively charges an attempt before handing control
to its caller. This is an upper-bound account, never a provider invoice. A
stable attempt_key makes replay idempotent. No method calls an external tool.
Lease/submit authority and API result projection are separate follow-on units.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from personal_agent.storage.models import Base, Operation
from personal_agent_core.sqlite import run_write_transaction


class RunStateError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateBudget:
    task_id: str
    revision: int
    resumable: bool
    control_available: bool = True
    reason: str | None = None
    amendable: bool = False


@dataclass(frozen=True)
class BoundBudget:
    accepted: bool
    task_id: str
    deadline_ms: int


TASK_LIMITS = {"llm": 12, "read": 9, "web": 6, "time_ms": 180000}
RUN_LIMITS = {"llm": 4, "read": 3, "web": 2, "time_ms": 60000}
USAGE_COLUMNS = {"llm": "llm_used", "read": "read_used", "web": "web_used", "time_ms": "active_ms"}


class RunBudgetStore:
    def __init__(self, session_factory):
        self.sessions = session_factory
        self.tasks = Base.metadata.tables["agent_tasks"]
        self.runs = Base.metadata.tables["agent_runs"]
        self.reservations = Base.metadata.tables["agent_budget_reservations"]
        self.steps = Base.metadata.tables["agent_run_steps"]

    def _write(self, work):
        with self.sessions() as session:
            return run_write_transaction(session, lambda: work(session))

    def _one(self, session, table, column, identity):
        row = session.execute(select(table).where(column == identity)).mappings().one_or_none()
        if row is None:
            raise RunStateError("missing_run_or_task")
        return dict(row)

    def snapshot(self, operation_id):
        with self.sessions() as session:
            return self._one(session, self.runs, self.runs.c.operation_id, operation_id)

    def task_snapshot(self, task_id):
        with self.sessions() as session:
            return self._one(session, self.tasks, self.tasks.c.task_id, task_id)

    def _held(self, session, task_id, *, exclude_run=None):
        r = self.reservations
        query = select(*(func.coalesce(func.sum(r.c[key]), 0).label(key) for key in TASK_LIMITS)).where(
            r.c.task_id == task_id, r.c.state == "held")
        if exclude_run is not None:
            query = query.where(r.c.operation_id != exclude_run)
        return dict(session.execute(query).mappings().one())

    def held(self, task_id):
        with self.sessions() as session:
            return self._held(session, task_id)

    def _live(self, run, now_ms):
        if run["state"] not in {"accepted", "thinking", "reading"} or not run["started_ms"] + run["active_ms"] <= now_ms < run["deadline_ms"]:
            raise RunStateError("run_expired_or_stopped")

    def _next_step(self, session, operation_id):
        return session.execute(select(func.coalesce(func.max(self.steps.c.step_no), 0) + 1).where(
            self.steps.c.operation_id == operation_id)).scalar_one()

    def _guard_lease(self, run, lease, now_ms):
        # Unleased rows support offline construction; once acquired, every
        # budget mutation requires current durable ownership, even after expiry.
        if run["lease_owner"] is None and lease is None:
            return
        if (lease is None or lease.operation_id != run["operation_id"] or
                lease.owner != run["lease_owner"] or lease.fence != run["fence"] or
                run["lease_until_ms"] is None or now_ms >= run["lease_until_ms"]):
            raise RunStateError("stale_run_lease")

    def reserve_prebind(self, operation_id, task_ids, *, now_ms, llm_add=1, read_add=0,
                        attempt_key=None, lease=None):
        if (llm_add, read_add) not in {(1, 0), (0, 1)} or len(set(task_ids)) != len(task_ids):
            raise RunStateError("invalid_prebind_attempt")
        attempt_key = attempt_key or uuid4().hex
        fingerprint = hashlib.sha256(json.dumps([task_ids, llm_add, read_add]).encode()).hexdigest()
        def work(session):
            run = self._one(session, self.runs, self.runs.c.operation_id, operation_id)
            self._guard_lease(run, lease, now_ms)
            self._live(run, now_ms)
            if run["task_id"] is not None:
                raise RunStateError("already_bound")
            prior = session.execute(select(self.steps).where(self.steps.c.operation_id == operation_id,
                self.steps.c.attempt_nonce == attempt_key)).mappings().one_or_none()
            if prior and prior["args_hash"] != fingerprint:
                raise RunStateError("attempt_key_conflict")
            if not prior:
                run["llm_used"] += llm_add
                run["read_used"] += read_add
            if run["llm_used"] > 4 or run["read_used"] > 3:
                raise RunStateError("message_budget")
            desired = {"llm": run["llm_used"], "read": run["read_used"], "web": 0,
                       "time_ms": run["deadline_ms"] - run["started_ms"]}
            candidates = []
            for task_id in task_ids:
                task = self._one(session, self.tasks, self.tasks.c.task_id, task_id)
                if task["timeline_id"] != run["timeline_id"]:
                    raise RunStateError("candidate_not_available")
                held = self._held(session, task_id, exclude_run=operation_id)
                amendable = False
                if task['active_operation_id'] is not None:
                    active = self._one(session, self.runs, self.runs.c.operation_id, task['active_operation_id'])
                    op = self._one(session, Operation.__table__, Operation.operation_id, active['operation_id'])
                    submitted = session.execute(select(self.steps.c.call_id).where(
                        self.steps.c.operation_id == active['operation_id'], self.steps.c.kind == 'write',
                        self.steps.c.status.in_(['submitted', 'sent']))).first()
                    from personal_agent.api.operation_state import can_cancel_pre_submit
                    amendable = can_cancel_pre_submit(op['state']) and not submitted and active['superseded_by_operation_id'] is None
                eligible = (task["status"] in {"active", "waiting", "paused"}
                    and ((task["active_operation_id"] is None and task['write_slot'] is None) or amendable)
                    and all(task[USAGE_COLUMNS[key]] + held[key] + desired[key] <= TASK_LIMITS[key] for key in TASK_LIMITS))
                r = self.reservations
                old = session.execute(select(r).where(r.c.operation_id == operation_id,
                    r.c.task_id == task_id, r.c.reservation_no == 0)).mappings().one_or_none()
                if old and (old["task_revision"] != task["revision"] or old["state"] != "held"):
                    eligible = False
                if eligible:
                    values = dict(desired, task_revision=task["revision"], state="held")
                    if old:
                        session.execute(update(r).where(r.c.operation_id == operation_id,
                            r.c.task_id == task_id, r.c.reservation_no == 0).values(**values))
                    else:
                        session.execute(insert(r).values(operation_id=operation_id, task_id=task_id,
                            reservation_no=0, **values))
                # Do not release an earlier hold on degradation: an already
                # dispatched attempt must still be charged after a crash.
                candidates.append(CandidateBudget(task_id, task["revision"], eligible and not amendable,
                    reason=None if eligible else "candidate_not_resumable", amendable=eligible and amendable))
            if not prior:
                step_no = self._next_step(session, operation_id)
                session.execute(insert(self.steps).values(operation_id=operation_id, step_no=step_no,
                    call_no=0, attempt_no=1, call_id=attempt_key, attempt_nonce=attempt_key,
                    args_hash=fingerprint, kind="model" if llm_add else "discovery", status="in_flight", started_ms=now_ms))
            # Persist the observed elapsed time even for an idempotent replay.
            # Before binding this is a clock floor, not a charge to any Task;
            # bind still transfers the full elapsed cost exactly once.
            session.execute(update(self.runs).where(self.runs.c.operation_id == operation_id).values(
                llm_used=run["llm_used"], read_used=run["read_used"],
                active_ms=now_ms-run["started_ms"], state="thinking"))
            return candidates
        return self._write(work)

    def bind(self, operation_id, *, task_id, now_ms, sealed_goal, sealed_constraints,
             new_task_id=None, lease=None, _session=None, _reject=False):
        def work(session):
            run = self._one(session, self.runs, self.runs.c.operation_id, operation_id)
            self._guard_lease(run, lease, now_ms)
            chosen = task_id or new_task_id
            if not chosen:
                raise RunStateError("missing_new_task_id")
            if run["task_id"] is not None:
                if run["task_id"] != chosen:
                    raise RunStateError("cannot_rebind")
                return BoundBudget(run["state"] != "failed", chosen, run["deadline_ms"])
            self._live(run, now_ms)
            elapsed = now_ms - run["started_ms"]
            costs = {"llm": run["llm_used"], "read": run["read_used"], "web": run["web_used"], "time_ms": elapsed}
            accepted = True
            if task_id:
                task = self._one(session, self.tasks, self.tasks.c.task_id, task_id)
                r = self.reservations
                reservation = session.execute(select(r).where(r.c.operation_id == operation_id,
                    r.c.task_id == task_id, r.c.reservation_no == 0, r.c.state == "held")).mappings().one_or_none()
                if reservation is None or any(reservation[key] < costs[key] for key in costs):
                    raise RunStateError("candidate_not_reserved")
                accepted = (not _reject and task["revision"] == reservation["task_revision"]
                    and task["active_operation_id"] is None and task['write_slot'] is None and task["status"] in {"active", "waiting", "paused"})
                updates = {USAGE_COLUMNS[k]: task[USAGE_COLUMNS[k]] + costs[k] for k in costs}
                if accepted:
                    updates.update(active_operation_id=operation_id, status="active")
                session.execute(update(self.tasks).where(self.tasks.c.task_id == task_id).values(**updates))
                session.execute(update(r).where(r.c.operation_id == operation_id, r.c.task_id == task_id,
                    r.c.reservation_no == 0).values(state="charged", **{f"charged_{k}": costs[k] for k in costs}))
                task.update(updates)
                held = self._held(session, task_id, exclude_run=operation_id)
                deadline = min(run["deadline_ms"], now_ms + max(0, 180000 - task["active_ms"] - held["time_ms"]))
            else:
                session.execute(insert(self.tasks).values(task_id=chosen, timeline_id=run["timeline_id"],
                    sealed_goal=sealed_goal, sealed_constraints=sealed_constraints,
                    active_operation_id=operation_id, **{USAGE_COLUMNS[k]: costs[k] for k in costs}))
                task = {"revision": 1}
                deadline = run["deadline_ms"]
            # Conflicts are a committed result, not an exception rolling back
            # the already incurred attempt costs.
            session.execute(update(self.reservations).where(self.reservations.c.operation_id == operation_id,
                self.reservations.c.state == "held").values(state="released"))
            session.execute(update(self.runs).where(self.runs.c.operation_id == operation_id).values(
                task_id=chosen, expected_task_revision=task["revision"], active_ms=elapsed,
                deadline_ms=deadline, state="thinking" if accepted else "failed"))
            return BoundBudget(accepted, chosen, deadline)
        return work(_session) if _session is not None else self._write(work)

    def reserve_bound(self, operation_id, *, now_ms, llm_add=0, read_add=0, web_add=0,
                      attempt_key=None, lease=None, _session=None):
        if any(type(x) is not int or x < 0 for x in (llm_add, read_add, web_add)) or web_add > read_add or llm_add + read_add != 1:
            raise RunStateError("invalid_attempt")
        attempt_key = attempt_key or uuid4().hex
        def work(session):
            run = self._one(session, self.runs, self.runs.c.operation_id, operation_id)
            self._guard_lease(run, lease, now_ms)
            self._live(run, now_ms)
            task = self._one(session, self.tasks, self.tasks.c.task_id, run["task_id"])
            if task["active_operation_id"] != operation_id or task["revision"] != run["expected_task_revision"] or task["status"] != "active" or task["write_slot"] is not None:
                raise RunStateError("task_execution_stale")
            prior = session.execute(select(self.steps).where(self.steps.c.operation_id == operation_id,
                self.steps.c.attempt_nonce == attempt_key)).mappings().one_or_none()
            fingerprint = f"bound:{llm_add}:{read_add}:{web_add}"
            if prior:
                if prior["args_hash"] != fingerprint:
                    raise RunStateError("attempt_key_conflict")
                return
            elapsed = now_ms - run["started_ms"]
            costs = {"llm": llm_add, "read": read_add, "web": web_add, "time_ms": elapsed-run["active_ms"]}
            held = self._held(session, task["task_id"])
            if any(run[USAGE_COLUMNS[k]] + costs[k] > RUN_LIMITS[k] or
                   task[USAGE_COLUMNS[k]] + held[k] + costs[k] > TASK_LIMITS[k] for k in costs):
                raise RunStateError("bound_budget")
            session.execute(update(self.tasks).where(self.tasks.c.task_id == task["task_id"]).values(
                **{USAGE_COLUMNS[k]: task[USAGE_COLUMNS[k]] + costs[k] for k in costs}))
            session.execute(update(self.runs).where(self.runs.c.operation_id == operation_id).values(
                **{USAGE_COLUMNS[k]: run[USAGE_COLUMNS[k]] + costs[k] for k in costs}))
            session.execute(insert(self.steps).values(operation_id=operation_id,
                step_no=self._next_step(session, operation_id), call_no=0, attempt_no=1,
                call_id=attempt_key, attempt_nonce=attempt_key, args_hash=fingerprint,
                kind="model" if llm_add else "read", status="in_flight", started_ms=now_ms))
        return work(_session) if _session is not None else self._write(work)

    def recover_prebind(self, operation_id, *, now_ms=None):
        def work(session):
            run = self._one(session, self.runs, self.runs.c.operation_id, operation_id)
            if run["task_id"] is not None or run["state"] == "failed":
                return
            if run["lease_owner"] is not None and (now_ms is None or
                    now_ms < max(run["lease_until_ms"], run["started_ms"] + run["active_ms"])):
                raise RunStateError("live_or_unchecked_lease")
            r = self.reservations
            for reservation in session.execute(select(r).where(r.c.operation_id == operation_id,
                r.c.state == "held")).mappings().all():
                task = self._one(session, self.tasks, self.tasks.c.task_id, reservation["task_id"])
                session.execute(update(self.tasks).where(self.tasks.c.task_id == task["task_id"]).values(
                    **{USAGE_COLUMNS[k]: task[USAGE_COLUMNS[k]] + reservation[k] for k in TASK_LIMITS}))
                session.execute(update(r).where(r.c.operation_id == operation_id, r.c.task_id == task["task_id"],
                    r.c.reservation_no == reservation["reservation_no"]).values(state="orphan_charge",
                    **{f"charged_{k}": reservation[k] for k in TASK_LIMITS}))
            session.execute(update(self.runs).where(self.runs.c.operation_id == operation_id).values(
                state="failed", active_ms=60000, fence=run["fence"] + 1))
        self._write(work)
