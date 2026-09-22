import subprocess
from pathlib import Path
import pytest
from personal_agent_dal.worker.verification_paths import runtime_read_roots
from personal_agent_dal.worker.supervisor import SupervisorRefusal
from personal_agent_dal.worker.toolchain import _sandboxed_argv, _child_environment


@pytest.mark.parametrize('value', [None, '', ['/'], ['relative'], [False], ['/no-such-dal-runtime']])
def test_invalid_runtime_paths(value):
    with pytest.raises(SupervisorRefusal):
        runtime_read_roots({'runtime_read_roots': value})


def test_native_runtime_dependency_is_read_only(tmp_path):
    root = tmp_path.resolve()
    work = root / 'work'; work.mkdir()
    temp = root / 'temp'; temp.mkdir()
    dependency = root / 'dependency'; dependency.mkdir()
    allowed = dependency / 'public.txt'; allowed.write_text('synthetic')
    outside = root / 'outside'; outside.write_text('synthetic private')
    roots = runtime_read_roots({'runtime_read_roots': [str(dependency)]})
    def run(argv):
        return subprocess.run(_sandboxed_argv(argv, work, temp, (), roots), cwd=work,
            env=_child_environment(temp), capture_output=True)
    assert run(('/bin/cat', str(allowed))).stdout == b'synthetic'
    assert run(('/bin/cat', str(outside))).returncode != 0
    assert run(('/usr/bin/touch', str(dependency / 'forbidden'))).returncode != 0
    assert not (dependency / 'forbidden').exists()


def test_backup_metadata_requires_explicit_boolean_opt_in(tmp_path):
    from personal_agent_dal.worker.toolchain import _sandbox_profile
    root=tmp_path.resolve()
    assert 'com.apple.backupd.sandbox.xpc' not in _sandbox_profile(root,root,(),())
    profile=_sandbox_profile(root,root,(),(),backup_exclusion_metadata=True)
    assert '(allow mach-lookup (global-name "com.apple.backupd.sandbox.xpc"))' in profile
    assert '(deny network*)' in profile and '(deny mach-lookup (global-name "com.apple.securityd"))' in profile
    for value in ('true',1,None):
        with pytest.raises(SupervisorRefusal):runtime_read_roots({'backup_exclusion_metadata':value})
