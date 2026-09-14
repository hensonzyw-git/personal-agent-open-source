"""Cancellation and submit ordering against the real operation state machine."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from test_run_budget_store import setup
from test_agent_storage import SEALED, NOW
from personal_agent.runtime.run_leases import RunLeaseStore
from personal_agent.runtime.run_store import RunStateError
from personal_agent.runtime.task_control import TaskControlStore
from personal_agent.api.operation_store import transition_operation
from personal_agent.storage.models import Operation


def prepared(setup):
    budgets, new_run, factory = setup
    op = new_run('write')
    leases = RunLeaseStore(factory)
    lease = leases.acquire(op, owner='writer', now_ms=0, ttl_ms=50000)
    budgets.reserve_prebind(op, [], now_ms=1, lease=lease)
    budgets.bind(op, task_id=None, new_task_id='task', now_ms=2,
        sealed_goal=SEALED, sealed_constraints=SEALED, lease=lease)
    with factory() as session:
        for state, version, target in [('accepted', 1, 'interpreting'), ('interpreting', 2, 'dispatching')]:
            transition_operation(session, operation_id=op, current_state=state,
                current_version=version, target_state=target, now=NOW)
        session.commit()
    controls = TaskControlStore(factory)
    proposal = controls.freeze(lease, now_ms=3, action_id='action', args_hash='a'*64, sealed_args=SEALED)
    control_op = new_run('control')
    control_lease = leases.acquire(control_op, owner='controller', now_ms=0, ttl_ms=50000)
    return controls, proposal, control_lease, factory


def control(controls, lease, action='cancel', now=4):
    return controls.mutate_task(lease, task_id='task', expected_revision=1,
        action=action, control_id='control', sealed_change=SEALED, now_ms=now,
        sealed_goal=SEALED if action == 'amend' else None,
        sealed_constraints=SEALED if action == 'amend' else None)


def state(factory, op):
    with factory() as session:
        return session.execute(select(Operation.state).where(Operation.operation_id == op)).scalar_one()


@pytest.mark.parametrize('action', ['cancel', 'pause', 'amend'])
def test_control_first_supersedes_proposal_and_prevents_submit(setup, action):
    controls, proposal, lease, factory = prepared(setup)
    before = controls.task_snapshot('task')
    result = control(controls, lease, action)
    assert result == 'applied'
    after = controls.task_snapshot('task')
    assert after['revision'] == 2 and after['write_slot'] is None
    assert after['llm_used'] == before['llm_used']
    assert state(factory, proposal.lease.operation_id) == 'cancelled_pre_submit'
    with pytest.raises(RunStateError):
        controls.claim_submit(proposal, now_ms=5)
    assert control(controls, lease, action, now=6) == 'applied'
    assert controls.task_snapshot('task') == after


def test_submit_first_keeps_unknown_write_and_blocks_cancel_after_restart(setup):
    controls, proposal, lease, factory = prepared(setup)
    controls.claim_submit(proposal, now_ms=4)
    reopened = TaskControlStore(factory)
    before = reopened.task_snapshot('task')
    assert control(reopened, lease, now=5) == 'too_late'
    assert reopened.task_snapshot('task') == before
    assert state(factory, proposal.lease.operation_id) == 'source_in_progress'
    with pytest.raises(RunStateError):
        reopened.claim_submit(proposal, now_ms=6)


def test_two_workers_cancel_vs_submit_only_one_wins(setup):
    controls, proposal, lease, factory = prepared(setup)
    barrier = Barrier(2)
    def submit():
        barrier.wait()
        try:
            TaskControlStore(factory).claim_submit(proposal, now_ms=4)
            return 'submitted'
        except RunStateError:
            return 'refused'
    def cancel():
        barrier.wait()
        return control(TaskControlStore(factory), lease)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(submit), pool.submit(cancel)
        actual = a.result(), b.result()
    assert actual in {('submitted', 'too_late'), ('refused', 'applied')}


def test_legacy_transition_cannot_bypass_v2_submission_guard(setup):
    controls, proposal, lease, factory = prepared(setup)
    with factory() as session, pytest.raises(RunStateError):
        transition_operation(session, operation_id=proposal.lease.operation_id,
            current_state='dispatching', current_version=3, target_state='source_in_progress', now=NOW)
    assert state(factory, proposal.lease.operation_id) == 'dispatching'


def test_bad_operation_transition_rolls_back_submission_claim(setup):
    controls, proposal, lease, factory = prepared(setup)
    with factory() as session:
        transition_operation(session, operation_id=proposal.lease.operation_id,
            current_state='dispatching', current_version=3, target_state='failed_safe', now=NOW)
        session.commit()
    with pytest.raises(RunStateError):
        controls.claim_submit(proposal, now_ms=4)
    with factory() as session:
        assert session.execute(select(controls.steps.c.status).where(
            controls.steps.c.call_id == 'action')).scalar_one() == 'proposed'


def test_failed_submit_cas_cannot_be_committed_as_a_partial_claim(setup):
    controls, proposal, lease, factory = prepared(setup)
    from personal_agent.api.operation_state import StaleOperationVersionError
    from datetime import datetime, timezone
    with factory() as session:
        with pytest.raises(StaleOperationVersionError):
            transition_operation(session, operation_id=proposal.lease.operation_id,
                current_state='dispatching', current_version=1, target_state='source_in_progress',
                now=datetime.fromtimestamp(.004, tz=timezone.utc), run_submission=proposal)
        session.commit()
    with factory() as session:
        assert session.execute(select(controls.steps.c.status).where(
            controls.steps.c.call_id == 'action')).scalar_one() == 'proposed'
    assert controls.snapshot(proposal.lease.operation_id)['state'] == 'thinking'


def test_cancel_settles_elapsed_time_without_using_old_budget_for_control(setup):
    controls, proposal, lease, factory = prepared(setup)
    before = controls.task_snapshot('task')['active_ms']
    assert control(controls, lease, now=49000) == 'applied'
    assert controls.task_snapshot('task')['active_ms'] == 49000 > before


def test_frozen_write_blocks_new_budget_attempt(setup):
    controls, proposal, lease, factory = prepared(setup)
    with pytest.raises(RunStateError):
        controls.reserve_bound(proposal.lease.operation_id, now_ms=4, read_add=1, lease=proposal.lease)


def test_control_step_does_not_collide_with_next_model_attempt(setup):
    controls, proposal, lease, factory = prepared(setup)
    control(controls, lease)
    controls.reserve_prebind(lease.operation_id, [], now_ms=5, lease=lease)
    controls.reserve_prebind(lease.operation_id, [], now_ms=6, lease=lease)


def test_real_orchestrator_commit_stops_before_dispatch_without_v2_claim(setup):
    controls, proposal, lease, factory = prepared(setup)
    from personal_agent.api.orchestrator import _commit
    from personal_agent.api.intent import WriteIntent
    class Dispatcher:
        called = False
        def commit(self, **kwargs):
            self.called = True
            raise AssertionError('must not dispatch')
    dispatcher = Dispatcher()
    with factory() as session, pytest.raises(RunStateError, match='missing_run_submission'):
        op = session.get(Operation, proposal.lease.operation_id)
        _commit(session, op, intent=WriteIntent('finance.log_expense', {}),
            dispatcher=dispatcher, duplicate_override=None, keyring=None, now=NOW)
    assert not dispatcher.called


@pytest.mark.parametrize('field,value', [('args_hash', 'b'*64), ('action_id', 'another'),
    ('task_id', 'another'), ('task_revision', 99)])
def test_submission_must_match_frozen_action_and_task(setup, field, value):
    from dataclasses import replace
    controls, proposal, lease, factory = prepared(setup)
    with pytest.raises(RunStateError):
        controls.claim_submit(replace(proposal, **{field: value}), now_ms=4)
    assert state(factory, proposal.lease.operation_id) == 'dispatching'


def test_cancel_exhausted_task_uses_current_message_budget(setup):
    from sqlalchemy import update
    controls, proposal, lease, factory = prepared(setup)
    with factory() as session:
        session.execute(update(controls.tasks).where(controls.tasks.c.task_id == 'task').values(active_ms=180000, llm_used=12))
        session.execute(update(controls.runs).where(controls.runs.c.operation_id == proposal.lease.operation_id).values(deadline_ms=3))
        session.commit()
    assert control(controls, lease, now=4) == 'applied'
    assert controls.task_snapshot('task')['active_ms'] == 180000
    assert controls.snapshot(lease.operation_id)['deadline_ms'] == 60000


def test_control_replay_changed_action_is_rejected(setup):
    controls, proposal, lease, factory = prepared(setup)
    assert control(controls, lease) == 'applied'
    with pytest.raises(RunStateError, match='control_replay_conflict'):
        control(controls, lease, action='pause', now=5)
