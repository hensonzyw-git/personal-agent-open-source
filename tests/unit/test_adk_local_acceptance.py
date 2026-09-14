"""Authorization guard tests; never opens a provider connection."""
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import pytest

spec=importlib.util.spec_from_file_location('adk_local_acceptance',Path(__file__).resolve().parents[2]/'scripts/adk_local_acceptance.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_total_budget_persists_and_does_not_oversubscribe(tmp_path):
    path=tmp_path/'budget.json'
    path.write_text(json.dumps({'model':0,'anysearch':0}));path.chmod(0o600)
    def attempt(_):
        try:return m.reserve('model',path)
        except RuntimeError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(attempt,range(110)))
    assert sorted(x for x in results if x is not None)==list(range(1,101))
    assert json.loads(path.read_text())=={'model':100,'anysearch':0}
    assert m.reserve('anysearch',path)==1


def test_expiry_prevents_even_first_request(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    monkeypatch.setattr(m,'EXPIRES',datetime(2000,1,1,tzinfo=timezone.utc))
    with pytest.raises(RuntimeError,match='authorization_expired'):m.reserve('model',tmp_path/'budget.json')


def test_wrong_endpoint_zero_network_and_zero_charge(monkeypatch):
    import httpx
    called=[]
    monkeypatch.setattr(m,'reserve',lambda p:called.append(p))
    transport=m.CountedTransport('model',{'https://api.example.org/chat'},[],inner=httpx.MockTransport(lambda r:called.append('network')))
    with pytest.raises(RuntimeError,match='unpinned_request'):
        asyncio.run(transport.handle_async_request(httpx.Request('POST','https://attacker.example/chat')))
    assert called==[]


def test_provider_failure_still_charged(monkeypatch):
    import httpx
    called=[]
    monkeypatch.setattr(m,'reserve',lambda p:called.append(p) or len(called))
    def fail(request):raise httpx.ConnectError('synthetic')
    trace=[]
    transport=m.CountedTransport('model',{'https://api.example.org/chat'},trace,inner=httpx.MockTransport(fail))
    with pytest.raises(httpx.ConnectError):asyncio.run(transport.handle_async_request(httpx.Request('POST','https://api.example.org/chat',content=b'{}')))
    assert called==['model'] and trace[0]['request_number']==1

@pytest.mark.parametrize('content', ['', '{}', '{"model": -1, "anysearch": 19}', '{"model": true, "anysearch": 19}'])
def test_invalid_ledger_never_resets(tmp_path, content):
    path=tmp_path/'budget.json';path.write_text(content);path.chmod(0o600)
    with pytest.raises(RuntimeError, match='authorization_ledger_invalid'):m.reserve('model',path)
    assert path.read_text()==content


def test_missing_or_symlink_ledger_refused(tmp_path):
    path=tmp_path/'missing'
    with pytest.raises(FileNotFoundError):m.reserve('model',path)
    assert not path.exists()
    target=tmp_path/'target';target.write_text('{"model":84,"anysearch":19}');target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(OSError):m.reserve('model',path)
    assert json.loads(target.read_text())['model']==84


def test_migrated_counter_continues_from_history(tmp_path):
    path=tmp_path/'budget.json';path.write_text('{"model":84,"anysearch":19}');path.chmod(0o600)
    assert m.reserve('model',path)==85
    assert json.loads(path.read_text())=={'model':85,'anysearch':19}
