"""Actual Git/subprocess evidence on disposable synthetic repositories only.

The private test admission is a fixture, not production operator approval.
"""
import hashlib
import json
import shutil
import time
from pathlib import Path
import pytest
from personal_agent_core.crypto import KeyRing,generate_key
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.worker.supervisor import Supervisor,SupervisorRefusal
from personal_agent_dal.worker.workflow_inventory import WorkflowInventory
from personal_agent_dal.worker.workflow_executor import RepositoryExecutor
from personal_agent_dal.worker.runtime_admission import code_identity


def pin(path):
    path=Path(path).resolve()
    return dict(executable=str(path),executable_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),version='synthetic-test-pin')


@pytest.fixture
def repository(tmp_path):
    from personal_agent_dal.worker.runtime_process import os_boot_id
    root=(tmp_path/'owned').resolve()
    supervisor=Supervisor(root,boot_id=os_boot_id(),epoch=1)
    reservation=supervisor.reserve(attempt_id='workflow:synthetic',workspace_id='synthetic',generation=1,
        authority={'workflow_id':'synthetic'},read_roots=[])
    ring=KeyRing([generate_key('synthetic-worker')],service='dal-worker')
    inventory=WorkflowInventory(supervisor,ring)
    inventory.adopt(dict(attempt_id='attempt',owner={'kind':'workflow','workflow_id':'synthetic'}),reservation)
    for a,b in [('prepared','dispatch_requested'),('dispatch_requested','granted'),('granted','starting'),('starting','running')]:inventory.transition('attempt',a,b)
    commands=[dict(pin=pin('/usr/bin/grep'),arguments=['-q','synthetic acceptance','README.md'],timeout_seconds=10)]
    config=dict(git_pin=pin(shutil.which('git')),sandbox_pin=pin('/usr/bin/sandbox-exec'),
        projects={'project':dict(root=str(root),kind='local_new',verification_commands=commands)},executor_admission_file=str(tmp_path/'executor-proof.json'))
    proof=dict(schema='dal.workflow-executor-admission/1.0',provenance='operator-attested-external-native-executor',
        code_sha256=code_identity(),boot_id=supervisor.boot_id,supervisor_epoch=1,
        config_digest=digest({k:config[k] for k in ('git_pin','projects','sandbox_pin')}),issued_at=int(time.time())-1,
        expires_at=int(time.time())+1800,revoked=False,
        observations=['isolated-workspace','pinned-git','outside-write-denied','network-denied','bounded-stop','commit-readback'])
    path=Path(config['executor_admission_file']);path.write_text(json.dumps(proof));path.chmod(0o600)
    inputs=dict(prepared_at=int(time.time()),owner={'kind':'workflow','workflow_id':'synthetic'},request_revision=1,
        project={'project_id':'project','grant_digest':'a'*64},authorization=dict(project_id='project',root=str(root),kind='local_new',
            registration_policy='local_tracker',actions=['read','write','create','local_init']))
    executor=RepositoryExecutor(supervisor,reservation,config,inputs,inventory=inventory,attempt='attempt',heartbeat=lambda:True)
    return executor,inputs,inventory


def test_real_git_bootstrap_candidate_verify_commit_and_delivery_readback(repository):
    executor,inputs,inventory=repository
    prepared=executor.prepare();inputs['workspace']=prepared['manifest']
    base=prepared['manifest']['base_sha']
    assert base!='0'*40
    (executor.work/'README.md').write_text('synthetic acceptance\n')
    inputs['stage']=dict(stage_id='stage-one',revision=1,state_version=4,candidate={'head_sha':base})
    candidate=executor.candidate(stage_text='Synthetic code')['candidate']
    inputs['stage']['candidate']=candidate
    verified=executor.verify(heartbeat=lambda:True)
    assert verified['passed'] is True and verified['commands'][0]['exit_code']==0
    committed=executor.commit()
    assert committed['parent_sha']==base and committed['commit_sha']!=base
    assert executor.run(['rev-parse','HEAD'])==committed['commit_sha']
    stage=dict(stage_id='stage-one',revision=1,base_sha=base,head_sha=committed['commit_sha'],tree_sha=candidate['tree_sha'])
    inputs['delivery']=dict(workspace=prepared['manifest'],stages=[stage])
    delivery=executor.delivery()
    assert delivery['manifest']['commits']==[dict(sha=committed['commit_sha'],parent=base,tree=candidate['tree_sha'])]
    inputs['probe']=dict(nonce='fresh-nonce',manifest_digest=digest(delivery['manifest']))
    assert executor.probe()['matches'] is True
    (executor.work/'README.md').write_text('Changed after review\n')
    with pytest.raises(SupervisorRefusal,match='DIRTY'):executor.delivery()


def test_repository_pointer_tamper_cannot_redirect_git_writes(repository,tmp_path):
    executor,inputs,_=repository
    inputs['workspace']=executor.prepare()['manifest']
    (executor.work/'.git').write_text('gitdir: '+str(tmp_path/'foreign')+'\n')
    with pytest.raises(SupervisorRefusal,match='GIT_DIRECTORY_SUBSTITUTED'):
        executor.run(['add','--all'])


def test_executor_revocation_prevents_next_command(repository):
    executor,_,_=repository
    path=Path(executor.config['executor_admission_file'])
    proof=json.loads(path.read_text());proof['revoked']=True;path.write_text(json.dumps(proof))
    with pytest.raises(SupervisorRefusal,match='ADMISSION_REQUIRED'):executor.prepare()


@pytest.mark.parametrize('name,content,reason',[
    ('.env.local','SYNTHETIC=placeholder','SENSITIVE_FILE'),
    ('notes.txt','ghp_'+'x'*32,'SECRET'),
    ('key.txt','-----BEGIN PRIVATE KEY-----','SECRET'),
])
def test_candidate_scan_rejects_leaks_before_acceptance(repository,name,content,reason):
    executor,inputs,_=repository
    inputs['workspace']=executor.prepare()['manifest']
    inputs['stage']={'candidate':{'head_sha':inputs['workspace']['base_sha']}}
    (executor.work/name).write_text(content)
    with pytest.raises(SupervisorRefusal,match=reason):executor.candidate(stage_text='synthetic')


def test_native_git_objects_validate_and_preserve_exact_hashes(repository):
    import base64
    from personal_agent_dal.github.workflow_objects import decode_bundle,object_sha
    executor,inputs,_=repository
    inputs['workspace']=executor.prepare()['manifest'];base=inputs['workspace']['base_sha']
    (executor.work/'README.md').write_text('synthetic acceptance\n')
    inputs['stage']=dict(stage_id='stage-one',revision=1,state_version=4,candidate={'head_sha':base})
    candidate=executor.candidate(stage_text='Synthetic')['candidate'];inputs['stage']['candidate']=candidate
    committed=executor.commit()
    commits=[dict(sha=committed['commit_sha'],parent=base,tree=candidate['tree_sha'])]
    objects=[]
    for sha in executor.run(['rev-list','--objects','--no-object-names',base+'..HEAD']).splitlines():
        kind=executor.run(['cat-file','-t',sha]);raw=executor.run(['cat-file',kind,sha],raw=True)
        objects.append(dict(sha=sha,kind=kind,data=base64.b64encode(raw).decode()))
    ordered=decode_bundle(objects,commits)
    assert [kind for _,kind,_ in ordered]==['blob','tree','commit']
    assert ordered[-1][0]==committed['commit_sha']
    blob=next(obj for obj in objects if obj['kind']=='blob')
    blob['data']=base64.b64encode(b'tampered').decode()
    with pytest.raises(ValueError,match='GIT_OBJECT_INVALID'):decode_bundle(objects,commits)


def test_native_stopped_source_observer_reads_without_mutating_git(repository):
    from types import SimpleNamespace
    from personal_agent_dal.worker.workflow_observer import observe
    executor,inputs,inventory=repository
    inputs['workspace']=executor.prepare()['manifest']
    before=executor.run(['rev-parse','HEAD'])
    inventory.transition('attempt','running','unknown')
    row=inventory.get('attempt')
    row['binding']['execution_input']={'phase':'stage_commit'}
    worker=SimpleNamespace(config=executor.config,supervisor=executor.supervisor,inventory=inventory)
    observed=observe(worker,row)
    assert observed['process_exited'] and observed['head_sha']==before
    assert executor.run(['rev-parse','HEAD'])==before


def test_reservation_boot_renewal_requires_stopped_inventory_and_preserves_directories(repository):
    from datetime import datetime,timezone,timedelta
    from personal_agent_dal.worker.supervisor import Supervisor
    executor,inputs,inventory=repository
    inputs['workspace']=executor.prepare()['manifest']
    binding=dict(boot_id='new-test-boot',supervisor_epoch=2,owner={'kind':'workflow','workflow_id':'synthetic'},
        lease_until=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
    newer=WorkflowInventory(Supervisor(executor.supervisor.root,boot_id='new-test-boot',epoch=2),inventory.keyring)
    with pytest.raises(SupervisorRefusal,match='UNRESOLVED'):newer.renew_reservation('synthetic',binding)
    inventory.transition('attempt','running','unknown')
    inventory.transition('attempt','unknown','refused')
    newer.renew_reservation('synthetic',binding)
    renewed=newer.supervisor.validate(executor.reservation['reservation_id'])
    assert renewed['identities']==executor.reservation['identities']
    assert renewed['boot_id']=='new-test-boot'
    assert renewed['generation']==executor.reservation['generation']


def test_phone_local_project_has_frozen_bootstrap_and_attested_policy(repository):
    from datetime import datetime,timezone
    from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate,LOCAL_BOOTSTRAP_SHA
    from personal_agent_dal.worker.project_policy import directory_identity
    from personal_agent_dal.worker.project_registration import attest
    executor,inputs,_=repository;config=executor.config
    config.update(schema='dal.workflow-worker/1.1',identity=dict(worker_id='worker',boot_id=executor.supervisor.boot_id,supervisor_epoch=1))
    template=ProjectTemplate(project_id='project',revision=1,display_name='Synthetic new project',kind='local_new',
        root=str(executor.supervisor.root),remote_repository=None,allowed_actions=['read','write','create','local_init'],
        registration_policies=['local_tracker'],max_budget_seconds=600,max_validity_seconds=3600,
        allow_subjects=['device:synthetic'],worker_id='worker',
        worker_configuration_digest=digest({k:config[k] for k in ('git_pin','projects','sandbox_pin')}),
        directory_identity_digest=directory_identity(str(executor.supervisor.root)),base_sha=LOCAL_BOOTSTRAP_SHA,
        base_branch='main',budget_policy_ref='budget')
    policy=dict(template_digest=digest(template.model_dump(mode='json')),directory_identity_digest=template.directory_identity_digest,
        base_sha=template.base_sha,base_branch='main')
    config['project_policies']={'project':dict(policy)}
    policy.update(project_id='project',root=template.root,kind='local_new',worker_id='worker',
        worker_configuration_digest=template.worker_configuration_digest,branch='refs/heads/codex/dal-synthetic')
    inputs['project_policy']=policy
    proof_path=Path(config['executor_admission_file']);proof=json.loads(proof_path.read_text())
    proof['config_digest']=digest({k:config[k] for k in ('git_pin','projects','sandbox_pin','project_policies')})
    proof_path.write_text(json.dumps(proof))
    evidence=attest(config,template,now=datetime.now(timezone.utc))
    assert evidence['template']['base_sha']==LOCAL_BOOTSTRAP_SHA
    assert executor.prepare()['manifest']['base_sha']==LOCAL_BOOTSTRAP_SHA


def test_large_unchanged_baseline_does_not_consume_candidate_scan_budget(repository):
    """A large existing tree is not an all-files-added candidate."""
    executor,inputs,_=repository
    inputs['workspace']=executor.prepare()['manifest']
    (executor.work/'large-baseline.txt').write_text('synthetic baseline\n' * 550000)
    executor.run(['add','--all'])
    executor.run(['commit','-m','Synthetic existing baseline'])
    base=executor.run(['rev-parse','HEAD'])
    inputs['stage']={'candidate':{'head_sha':base}}
    (executor.work/'small-change.txt').write_text('synthetic small change\n')
    assert executor.candidate(stage_text='synthetic')['candidate']['base_sha']==base
    (executor.work/'large-baseline.txt').write_text('synthetic changed baseline\n' * 550000)
    with pytest.raises(SupervisorRefusal,match='CANDIDATE_SCAN_LIMIT'):
        executor.candidate(stage_text='synthetic oversized changed file')


@pytest.mark.parametrize('content,expected', [
    ('synthetic assertion failed', 'synthetic assertion failed'),
    ('ghp_'+'x'*32, '验证输出含凭据模式'),
    ('x'*65537, '验证输出超过 65536 字节'),
], ids=['failure','secret','overflow'])
def test_native_failed_verification_diagnostics_are_safe(repository,content,expected):
    executor,inputs,_=repository
    commands=executor.config['projects']['project']['verification_commands']
    commands[0]=dict(pin=pin('/bin/sh'), arguments=['-c','cat failure.txt; exit 1'],timeout_seconds=10)
    path=Path(executor.config['executor_admission_file'])
    proof=json.loads(path.read_text())
    proof['config_digest']=digest({k:executor.config[k] for k in ('git_pin','projects','sandbox_pin')})
    path.write_text(json.dumps(proof))
    inputs['workspace']=executor.prepare()['manifest']
    (executor.work/'failure.txt').write_text(content)
    inputs['stage']=dict(stage_id='synthetic-stage',revision=1,state_version=4,candidate={'head_sha':inputs['workspace']['base_sha']})
    executor.run(['add','--all'])
    inputs['stage']['candidate']['tree_sha']=executor.run(['write-tree'])
    result=executor.verify(heartbeat=lambda:True)
    assert not result['passed'] and expected in result['text']
    if content.startswith('ghp_'):assert content not in result['text']
