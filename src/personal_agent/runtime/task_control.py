"""Transactional proposal/control authority used by the production durable Host.

Inputs here are Host-authorized references and sealed data, never raw model
arguments. The production Host validates semantic source references and consumes dispatch claims.
A submitted proposal is recovery-only; this module never repeats an external call.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json

from sqlalchemy import insert, select, update

from personal_agent.runtime.run_leases import RunLease, RunLeaseStore
from personal_agent.runtime.run_store import RunStateError
from personal_agent.storage.models import Operation


@dataclass(frozen=True)
class RunSubmission:
    lease: RunLease
    task_id: str
    task_revision: int
    action_id: str
    args_hash: str


def _now(now_ms):
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)


def guard_submission(session, operation_id, submission, now):
    """Called by the existing state transition, inside its short transaction."""
    store = TaskControlStore(None)
    run = session.execute(select(store.runs).where(store.runs.c.operation_id == operation_id)).mappings().one_or_none()
    if run is None:
        operation=session.execute(select(Operation.__table__).where(Operation.operation_id==operation_id)).mappings().one_or_none()
        if operation is not None and operation['plan_key']:
            parent=session.execute(select(Operation.__table__).where(Operation.idempotency_key==operation['plan_key'])).mappings().one_or_none()
            parent_run=None if parent is None else session.execute(select(store.runs).where(store.runs.c.operation_id==parent['operation_id'])).mappings().one_or_none()
            if parent_run is not None:
                # The complete immutable plan was frozen before the first item
                # claimed submission. Later items are recovery, not new intent.
                submitted=session.execute(select(store.steps.c.call_id).where(store.steps.c.operation_id==parent['operation_id'],
                    store.steps.c.call_id==operation['plan_key'],store.steps.c.kind=='write',store.steps.c.status.in_(['submitted','sent']))).first()
                if not submitted or parent_run['state'] not in {'handoff','parked','completed'} or operation['encrypted_request'] is None:
                    raise RunStateError('unclaimed_plan_item')
                return
        if submission is not None:
            raise RunStateError('unexpected_run_submission')
        return
    if not isinstance(submission, RunSubmission) or submission.lease.operation_id != operation_id:
        raise RunStateError('missing_run_submission')
    store._claim(session, submission, (now - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1))


class TaskControlStore(RunLeaseStore):
    def _proposal(self, session, submission):
        row = session.execute(select(self.steps).where(
            self.steps.c.operation_id == submission.lease.operation_id,
            self.steps.c.kind == 'write', self.steps.c.call_id == submission.action_id)).mappings().one_or_none()
        if row is None or row['args_hash'] != submission.args_hash:
            raise RunStateError('proposal_mismatch')
        return row

    def freeze(self, lease, *, now_ms, action_id, args_hash, sealed_args):
        if not action_id or len(args_hash) != 64 or any(c not in '0123456789abcdef' for c in args_hash):
            raise RunStateError('invalid_action_identity')
        def work(session):
            run = self._one(session, self.runs, self.runs.c.operation_id, lease.operation_id)
            if run['task_id'] is None:
                raise RunStateError('unbound_proposal')
            task = self._one(session, self.tasks, self.tasks.c.task_id, run['task_id'])
            self.check_in_transaction(session, lease, now_ms=now_ms,
                action_id=action_id if task['write_slot'] == action_id else None)
            proposal = RunSubmission(lease, task['task_id'], task['revision'], action_id, args_hash)
            if task['write_slot'] == action_id:
                if self._proposal(session, proposal)['status'] != 'proposed':
                    raise RunStateError('already_submitted_recovery_only')
                return proposal
            session.execute(insert(self.steps).values(operation_id=lease.operation_id,
                step_no=self._next_step(session, lease.operation_id), call_no=0, attempt_no=1,
                call_id=action_id, attempt_nonce='proposal:' + action_id, args_hash=args_hash,
                kind='write', status='proposed', sealed_args=sealed_args, started_ms=now_ms))
            session.execute(update(self.tasks).where(self.tasks.c.task_id == task['task_id']).values(write_slot=action_id))
            return proposal
        return self._write(work)

    def _claim(self, session, submission, now_ms):
        proposal = self._proposal(session, submission)
        if proposal['status'] != 'proposed':
            raise RunStateError('already_submitted_recovery_only')
        run = self.check_in_transaction(session, submission.lease, now_ms=now_ms, action_id=submission.action_id)
        if run['task_id'] != submission.task_id or run['expected_task_revision'] != submission.task_revision:
            raise RunStateError('submission_task_mismatch')
        operation = self._one(session, Operation.__table__, Operation.operation_id, submission.lease.operation_id)
        if operation['cancel_requested'] or operation['state'] != 'dispatching':
            raise RunStateError('operation_not_submittable')
        from personal_agent.runtime.run_repository import close_candidate
        close_candidate(session, run, now_ms=now_ms)
        session.execute(update(self.steps).where(self.steps.c.operation_id == run['operation_id'],
            self.steps.c.step_no == proposal['step_no'], self.steps.c.call_no == proposal['call_no']).values(
                status='submitted', ended_ms=now_ms))
        session.execute(update(self.runs).where(self.runs.c.operation_id == run['operation_id']).values(state='handoff'))

    def claim_submit(self, submission, *, now_ms):
        from personal_agent.api.operation_store import transition_operation
        def work(session):
            operation = self._one(session, Operation.__table__, Operation.operation_id, submission.lease.operation_id)
            transition_operation(session, operation_id=operation['operation_id'],
                current_state=operation['state'], current_version=operation['state_version'],
                target_state='source_in_progress', now=_now(now_ms), run_submission=submission)
        self._write(work)

    def mutate_task(self, lease, *, task_id, expected_revision, action, control_id,
                    sealed_change, now_ms, sealed_goal=None, sealed_constraints=None, _session=None):
        if action not in {'cancel', 'pause', 'amend'} or not control_id:
            raise RunStateError('invalid_task_control')
        if (action == 'amend') != (sealed_goal is not None and sealed_constraints is not None):
            raise RunStateError('invalid_task_delta')
        if action != 'amend' and (sealed_goal is not None or sealed_constraints is not None):
            raise RunStateError('unexpected_task_delta')
        fingerprint = hashlib.sha256(json.dumps([task_id, expected_revision, action,
            sealed_change, sealed_goal, sealed_constraints], sort_keys=True).encode()).hexdigest()
        def work(session):
            current = self.check_in_transaction(session, lease, now_ms=now_ms)
            if current['task_id'] is not None:
                raise RunStateError('control_requires_independent_message')
            prior = session.execute(select(self.steps).where(self.steps.c.operation_id == lease.operation_id,
                self.steps.c.attempt_nonce == 'control:' + control_id)).mappings().one_or_none()
            if prior:
                if prior['args_hash'] != fingerprint:
                    raise RunStateError('control_replay_conflict')
                return prior['status']
            task = self._one(session, self.tasks, self.tasks.c.task_id, task_id)
            if task['timeline_id'] != current['timeline_id'] or task['revision'] != expected_revision:
                raise RunStateError('stale_or_foreign_task')
            if task['status'] in {'completed', 'cancelled'}:
                raise RunStateError('terminal_task')
            result = 'applied'
            old = None
            active_id=task['active_operation_id']
            if active_id is None and task['write_slot'] is None:
                active_id=session.execute(select(self.runs.c.operation_id).join(Operation,Operation.operation_id==self.runs.c.operation_id).where(self.runs.c.task_id==task_id,Operation.state.in_(['waiting_for_clarification','waiting_for_duplicate_decision'])).order_by(self.runs.c.started_ms.desc()).limit(1)).scalar_one_or_none()
            if active_id is not None:
                old = self._one(session, self.runs, self.runs.c.operation_id, active_id)
                operation = self._one(session, Operation.__table__, Operation.operation_id, old['operation_id'])
                from personal_agent.api.operation_state import can_cancel_pre_submit
                submitted = session.execute(select(self.steps.c.call_id).where(
                    self.steps.c.operation_id == old['operation_id'], self.steps.c.kind == 'write',
                    self.steps.c.status.in_(['submitted','sent']))).first()
                if submitted or not can_cancel_pre_submit(operation['state']):
                    result = 'too_late'
            elif task['write_slot'] is not None:
                result = 'too_late'
            if result == 'applied':
                elapsed = None
                if old:
                    elapsed = min(now_ms, old['deadline_ms']) - old['started_ms'] if task['active_operation_id'] and old['state']!='parked' else old['active_ms']
                    if elapsed < old['active_ms']:
                        raise RunStateError('control_clock_rollback')
                    from personal_agent.api.operation_store import transition_operation
                    transition_operation(session, operation_id=old['operation_id'], current_state=operation['state'],
                        current_version=operation['state_version'], target_state='cancelled_pre_submit', now=_now(now_ms))
                    session.execute(update(self.runs).where(self.runs.c.operation_id == old['operation_id']).values(
                        state='cancelled', fence=old['fence'] + 1, active_ms=elapsed))
                    session.execute(update(self.steps).where(self.steps.c.operation_id == old['operation_id'],
                        self.steps.c.kind == 'write', self.steps.c.status == 'proposed').values(status='superseded', ended_ms=now_ms))
                values = dict(revision=expected_revision + 1, active_operation_id=None, write_slot=None,
                    status={'cancel': 'cancelled', 'pause': 'paused', 'amend': 'active'}[action])
                if old:
                    values['active_ms'] = task['active_ms'] + elapsed - old['active_ms']
                if action == 'amend':
                    values.update(sealed_goal=sealed_goal, sealed_constraints=sealed_constraints)
                session.execute(update(self.tasks).where(self.tasks.c.task_id == task_id).values(**values))
            session.execute(insert(self.steps).values(operation_id=lease.operation_id,
                step_no=self._next_step(session, lease.operation_id), call_no=0, attempt_no=1,
                call_id=control_id, attempt_nonce='control:' + control_id, args_hash=fingerprint,
                kind='control', status=result, sealed_args=sealed_change, started_ms=now_ms, ended_ms=now_ms))
            return result
        return work(_session) if _session is not None else self._write(work)
