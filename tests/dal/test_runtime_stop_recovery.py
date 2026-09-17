"""Deterministic process-group observations; no host process is signalled."""
import signal

import pytest

from personal_agent_dal.worker import runtime_process as process
from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
from personal_agent_dal.worker.trusted_runtime import prepare_runtime, reconcile_runtime
from tests.dal.test_trusted_runtime import runtime, world


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(process.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(process.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(process.os, 'waitpid', lambda *args: (0, 0))
    monkeypatch.setattr(process.os, 'getpgid', lambda pid: pid)
    return now


def observation():
    return dict(boot_id='boot', pid=12345, pgid=12345, process_start='original')


def test_delayed_group_disappearance_after_leader_loss(monkeypatch, clock):
    signals = []
    monkeypatch.setattr(process, 'process_identity', lambda pid: 'original' if not signals else None)
    def killpg(pid, sig):
        if sig:
            signals.append(sig)
        elif clock[0] >= .12:
            raise ProcessLookupError
    monkeypatch.setattr(process.os, 'killpg', killpg)
    stop = process.stop_registered(observation(), boot_id='boot')
    assert stop['process_exited'] and stop['requested'] and not stop['forced']
    assert signals == [signal.SIGTERM]
    assert .12 <= clock[0] < 2.1


@pytest.mark.parametrize('identity', [None, 'reused'])
def test_surviving_or_reused_group_is_never_signalled(monkeypatch, clock, identity):
    monkeypatch.setattr(process, 'process_identity', lambda pid: identity)
    def killpg(pid, sig):
        assert sig == 0, 'unowned process group was signalled'
    monkeypatch.setattr(process.os, 'killpg', killpg)
    stop = process.stop_registered(observation(), boot_id='boot')
    assert not any(stop[key] for key in ('requested', 'forced', 'process_exited'))
    assert 2 <= clock[0] < 2.1


def test_reused_pid_without_original_group_is_not_absence(monkeypatch, clock):
    monkeypatch.setattr(process, 'process_identity', lambda pid: 'reused')
    def killpg(pid, sig):
        assert sig == 0
        raise ProcessLookupError
    monkeypatch.setattr(process.os, 'killpg', killpg)
    assert not process.stop_registered(observation(), boot_id='boot')['process_exited']


def test_owned_group_timeout_is_bounded(monkeypatch, clock):
    signals = []
    monkeypatch.setattr(process, 'process_identity', lambda pid: 'original')
    monkeypatch.setattr(process.os, 'killpg', lambda pid, sig: signals.append(sig) if sig else None)
    stop = process.stop_registered(observation(), boot_id='boot')
    assert stop['requested'] and stop['forced'] and not stop['process_exited']
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert 4 <= clock[0] < 4.1


def test_false_stop_then_absent_refresh_preserves_evidence(runtime, monkeypatch, clock):
    t, lease, context, supervisor, kwargs = runtime
    prepare_runtime(t, lease, context, **kwargs)
    inv = RuntimeInventory(supervisor)
    attempt = context['attempt_id']
    inv.transition(attempt, 'prepared', 'dispatch_requested')
    inv.transition(attempt, 'dispatch_requested', 'granted')
    inv.transition(attempt, 'granted', 'starting', observation=observation())
    signals = []
    monkeypatch.setattr(process, 'process_identity', lambda pid: 'original' if not signals else None)
    absent = [False]
    def killpg(pid, sig):
        if sig:
            signals.append(sig)
        elif absent[0]:
            raise ProcessLookupError
    monkeypatch.setattr(process.os, 'killpg', killpg)
    reconcile_runtime(supervisor)
    before = inv.get(attempt)['observation']['reconciliation_stop']
    assert before['requested'] and not before['process_exited']
    absent[0] = True
    reconcile_runtime(supervisor)
    row = inv.get(attempt)
    assert row['state'] == 'unknown'
    assert row['observation']['reconciliation_stop']['process_exited']
    assert row['observation']['reconciliation_stop']['requested']
    assert row['observation']['reconciliation_stop_history'] == [before]
    assert signals == [signal.SIGTERM]
    reconcile_runtime(supervisor)
    assert inv.get(attempt)['observation'] == row['observation']


@pytest.mark.parametrize('loss_at', ['before_signal', 'signal_lookup'])
def test_leader_loss_at_signal_boundary_still_observes(monkeypatch, clock, loss_at):
    reads = [0]
    def identity(pid):
        reads[0] += 1
        return 'original' if reads[0] <= (1 if loss_at == 'before_signal' else 2) else None
    monkeypatch.setattr(process, 'process_identity', identity)
    def killpg(pid, sig):
        if sig:
            assert loss_at == 'signal_lookup' and sig == signal.SIGTERM
            raise ProcessLookupError
        if clock[0] >= .12:
            raise ProcessLookupError
    monkeypatch.setattr(process.os, 'killpg', killpg)
    stop = process.stop_registered(observation(), boot_id='boot')
    assert stop['process_exited'] and not stop['requested'] and not stop['forced']
    assert .12 <= clock[0] < 2.1
