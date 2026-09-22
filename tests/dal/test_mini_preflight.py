"""Offline CLI refusal tests; no mini command or sandbox process is executed."""
import importlib.util
from itertools import count
from pathlib import Path
from types import SimpleNamespace
import json
import time
import pytest

spec=importlib.util.spec_from_file_location('dal_mini_preflight',Path(__file__).parents[2]/'scripts/dal_mini_preflight.py')
preflight=importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


CANARY_KEYS = (
    'replacement_work', 'replacement_git', 'replacement_temp', 'replacement_parent',
    'supervisor_parent', 'signing', 'environment', 'descriptor', 'own_workspace',
)


@pytest.fixture
def isolation_stub(tmp_path, monkeypatch):
    config = dict(boot_id='boot', supervisor_epoch=1, read_roots=[], sandbox_pin={},
                  python_pin={'executable':'synthetic-python', 'executable_sha256':'synthetic'})
    s = preflight.Supervisor(tmp_path/'supervisor', boot_id='boot', epoch=1)
    results = {kind:dict.fromkeys(CANARY_KEYS, True) for kind in ('direct', 'detached')}
    calls = []
    def process(reservation_id, *, sandbox_pin, argv, executable_sha256):
        calls.append(reservation_id)
        assert argv[:3] == ['synthetic-python', '-c', preflight.CANARY_PROGRAM]
        body = json.loads(argv[3])
        assert set(body['protected']) == set(CANARY_KEYS)-{'environment', 'descriptor', 'own_workspace'}
        assert body['own'] == s.validate(reservation_id)['workspace']
        if results['detached'] is not None:
            Path(body['descendant_result']).write_text(json.dumps(results['detached']))
        return 0, json.dumps(results['direct'])
    monkeypatch.setattr(s, 'synthetic_process', process)
    # Advance past the fixed deadline without a real wait when evidence is absent.
    ticks = count(start=0, step=11)
    monkeypatch.setattr(preflight, 'time', SimpleNamespace(
        monotonic=lambda:next(ticks),
        sleep=lambda _:pytest.fail('unexpected real wait'),
    ))
    return config, s, results, calls


def test_isolation_clock_is_local_and_supports_extra_calls(isolation_stub):
    config, s, results, calls = isolation_stub
    assert preflight.time is not time
    assert preflight.time.monotonic() == 0
    before = time.monotonic()
    time.sleep(0)
    assert time.monotonic() >= before
    assert [preflight.time.monotonic() for _ in range(4)] == [11, 22, 33, 44]
    results['detached'] = None
    with pytest.raises(preflight.SupervisorRefusal, match='^DETACHED_DESCENDANT_UNOBSERVED$'):
        preflight.isolation(config, s)
    assert len(calls) == 1
    assert preflight.time.monotonic() >= 77


def test_isolation_accepts_complete_direct_and_detached_results(isolation_stub):
    config, s, results, calls = isolation_stub
    assert preflight.isolation(config, s) == {
        'direct_canaries':True, 'detached_descendant_canaries':True,
        'restart':True, 'directory_substitution':True,
    }
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['direct', 'detached'])
@pytest.mark.parametrize('key', CANARY_KEYS)
@pytest.mark.parametrize('change', ['false', 'missing', 'truthy'])
def test_isolation_requires_every_canary_to_be_true(isolation_stub, kind, key, change):
    config, s, results, calls = isolation_stub
    if change == 'missing': del results[kind][key]
    else: results[kind][key] = 1 if change == 'truthy' else False
    with pytest.raises(preflight.SupervisorRefusal, match='^ISOLATION_CANARY_FAILED$'):
        preflight.isolation(config, s)
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['direct', 'detached'])
def test_isolation_rejects_extra_canary_results(isolation_stub, kind):
    config, s, results, calls = isolation_stub
    results[kind]['unexpected'] = True
    with pytest.raises(preflight.SupervisorRefusal, match='^ISOLATION_CANARY_FAILED$'):
        preflight.isolation(config, s)
    assert len(calls) == 1


def test_isolation_requires_detached_evidence(isolation_stub):
    config, s, results, calls = isolation_stub
    results['detached'] = None
    with pytest.raises(preflight.SupervisorRefusal, match='^DETACHED_DESCENDANT_UNOBSERVED$'):
        preflight.isolation(config, s)
    assert len(calls) == 1


def test_config_errors_redacted(tmp_path,capsys):
    config=tmp_path/'config.json';config.write_text('{"SECRET_CANARY":"do-not-print"}')
    assert preflight.main(['inventory','--config',str(config)])==1
    output=capsys.readouterr().out
    assert 'do-not-print' not in output and 'SECRET_CANARY' not in output
    assert json.loads(output)['code']=='PREFLIGHT_CONFIG_INVALID'


@pytest.mark.parametrize('extra,code',[([], 'EXPLICIT_RUNTIME_AUTHORIZATION_REQUIRED'),
    (['--allow-real-runtime','--instruction','explicit synthetic instruction'],'CLI_ZERO_TOOL_AND_CREDENTIAL_SEPARATION_UNIMPLEMENTED')])
def test_runtime_flags_cannot_enable_provider(tmp_path,monkeypatch,capsys,extra,code):
    calls=[]
    monkeypatch.setattr(preflight,'load_config',lambda _:dict(supervisor_root=str(tmp_path/'s'),boot_id='boot',supervisor_epoch=1))
    monkeypatch.setattr(preflight,'inventory',lambda *a:{'offline_stub':True})
    monkeypatch.setattr(preflight.subprocess,'Popen',lambda *a,**kw:calls.append(1))
    assert preflight.main(['runtime','--config','synthetic',*extra])==1
    assert json.loads(capsys.readouterr().out)['code']==code
    assert calls==[]


@pytest.mark.parametrize('attack', ['hidden', 'ignored', 'assume', 'skip', 'mode', 'fsmonitor', 'include', 'clean_filter'])
def test_source_inventory_uses_actual_reviewed_bytes(tmp_path, monkeypatch, attack):
    import hashlib
    import subprocess
    import shutil
    executable = shutil.which('git')
    assert executable is not None, 'Git is required for source inventory boundary tests'
    git = Path(executable).resolve()
    repo = tmp_path/'repo'; repo.mkdir()
    env = {'PATH': str(git.parent)+':/usr/bin:/bin', 'HOME': str(tmp_path),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    def run(*args):
        return subprocess.run([str(git), '-C', str(repo), *args], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()
    run('init')
    (repo/'src').mkdir(); source = repo/'src/main.py'; source.write_text('reviewed = True\n')
    (repo/'.gitignore').write_text('src/ignored.py\n')
    (repo/'.gitattributes').write_text('src/main.py filter=synthetic\n')
    run('add', '.'); run('-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture')
    sha = run('rev-parse', 'HEAD')
    marker = tmp_path/'fsmonitor-marker'
    hook = tmp_path/'monitor'; hook.write_text('#!/bin/sh\n/usr/bin/touch "'+str(marker)+'"\n'); hook.chmod(0o700)
    if attack == 'include':
        included = tmp_path/'included'; included.write_text('[core]\n\tfsmonitor = '+str(hook)+'\n')
        run('config', 'include.path', str(included))
    else: run('config', 'core.fsmonitor', str(hook))
    run('config', 'status.showUntrackedFiles', 'no')
    if attack in ('hidden', 'ignored'): (repo/'src'/('ignored.py' if attack=='ignored' else 'hidden.py')).write_text('unreviewed = True\n')
    elif attack in ('assume', 'skip'):
        run('-c', 'core.fsmonitor=false', 'update-index', '--assume-unchanged' if attack=='assume' else '--skip-worktree', 'src/main.py')
        source.write_text('reviewed = False\n')
    elif attack == 'mode':
        run('config', 'core.filemode', 'false'); source.chmod(0o755)
    clean_marker = tmp_path/'clean-filter-marker'
    if attack == 'clean_filter':
        clean_filter = tmp_path/'clean-filter'
        clean_filter.write_text('#!/bin/sh\n/usr/bin/touch \"'+str(clean_marker)+'\"\n/bin/cat\n')
        clean_filter.chmod(0o700)
        run('config', 'filter.synthetic.clean', str(clean_filter))
        source.write_text('dirty tracked bytes with a different size = True\n')
    # Inherited Git configuration must never override the pinned command options.
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.fsmonitor')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(hook))
    monkeypatch.setattr(preflight.platform, 'system', lambda:'Darwin')
    monkeypatch.setattr(preflight.platform, 'machine', lambda:'synthetic')
    monkeypatch.setattr(preflight, 'current_boot_id', lambda:'boot')
    pin = dict(executable=str(git), executable_sha256=hashlib.sha256(git.read_bytes()).hexdigest(), version='fixture')
    config = dict(repository=str(repo), reviewed_sha=sha, architecture='synthetic', boot_id='boot',
                  git_pin=pin, python_pin=pin, sandbox_pin=pin, runtime_pins=[], read_roots=[])
    class NoSandbox:
        def reserve(self, **kw): return {'reservation_id':'fixture'}
        def synthetic_process(self, *a, **kw): return 0, 'fixture'
    try:
        if attack in ('fsmonitor', 'include'): assert preflight.inventory(config, NoSandbox())['reviewed_revision']
        else:
            with pytest.raises(preflight.SupervisorRefusal, match='UNREVIEWED_IMPLEMENTATION_CHANGES'):
                preflight.inventory(config, NoSandbox())
    finally:
        assert not marker.exists()
        assert not clean_marker.exists()
