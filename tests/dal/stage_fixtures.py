"""Synthetic persisted counterparts for Stage unit tests, with real signatures."""
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.storage.timeline_models import (
    DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,DevelopmentDriverStep as Step,
    DevelopmentStage as Stage,DevelopmentStagePlan as Plan,DevelopmentWorkspace as Workspace,
)
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.timeline.artifacts import ArtifactService
from personal_agent_dal.timeline.roles import RoleService
from tests.dal.test_timeline_roles import config,registry
from tests.dal.workflow_signer import SignedDriver


def workspace(r,wf):
    with r.sessions() as s,s.begin():
        if s.get(Workspace,wf):return
        body=dict(kind='existing',project_id='project',reservation_id='workspace-'+wf,generation=1,
            directory_digest='a'*64,base_sha='b'*40,toolchain_digest='c'*64,branch='refs/heads/synthetic')
        s.add(Workspace(workflow_id=wf,reservation_id=body['reservation_id'],generation=1,directory_digest='a'*64,
            base_sha='b'*40,toolchain_digest='c'*64,receipt_digest='d'*64,sealed_manifest=r._seal(Workspace,wf,'sealed_manifest',body)))


def evidence(r,wf,phase,result,*,stage=None):
    roles=RoleService(r,registry(config()))
    revision=roles.register(config())
    with r.sessions() as s:
        from personal_agent_dal.storage.timeline_models import RoleConfigurationBinding
        has=s.get(RoleConfigurationBinding,('system','default')) is not None
    if not has:roles.bind(scope='system',scope_id='default',revision_id=revision,expected_version=0)
    snapshot=roles.snapshot(workflow_id=wf)
    driver=SignedDriver(r,roles=roles)
    ident=new_id()
    with r.sessions() as s,s.begin():
        workflow=s.get(Workflow,wf)
        gate=s.get(Gate,wf)
        if gate is None:gate=Gate(workflow_id=wf,mode='open',epoch=1,version=1);s.add(gate)
        inputs={}
        if stage:
            row=s.get(Stage,stage);plan=s.get(Plan,row.plan_id)
            inputs['stage']=dict(state_version=row.state_version,dependency_digest=row.dependency_digest,plan_digest=plan.dag_digest)
        step=Step(step_id=ident,workflow_id=wf,phase=phase,input_digest=digest(inputs),sealed_input=r._seal(Step,ident,'sealed_input',inputs),
            snapshot_id=snapshot['snapshot_id'],expected_version=workflow.version,gate_epoch=gate.epoch,cycle=1,status='prepared',
            stage_id=stage[0] if stage else None,stage_revision=stage[1] if stage else None)
        s.add(step)
    now=int(r.now().timestamp())
    driver.test_registry['synthetic']=dict(keys={'test':driver.test_key.public_key()},admission=dict(
        schema='dal.workflow-admission/1.0',worker_id='synthetic',boot_id='test-boot',supervisor_epoch=1,
        snapshot_digest=snapshot['snapshot_digest'],issued_at=now-1,expires_at=now+1800,revoked=False,evidence_digest='a'*64))
    with r.sessions() as s,s.begin():
        step=s.get(Step,ident)
        binding=driver.authority.reserve(s,step,'synthetic');s.flush()
        step.attempt_id=binding['execution_id'];step.status='dispatch_started'
    proof=driver.proof(ident,'dal.workflow-result/1.0',result)
    with r.sessions() as s,s.begin():
        step=s.get(Step,ident)
        execution=driver.authority.record_result(s,step,proof,result)
        step.status='completed';step.result_digest=digest(result);step.sealed_result=r._seal(Step,ident,'sealed_result',result)
        receipt=execution.receipt_digest
    return ident,receipt


def reviewed_design(r,wf,body):
    import hashlib
    design=dict(kind='design',text='Synthetic design',prd_digest='a'*64,plan=body)
    step,_=evidence(r,wf,'design_authoring',design)
    artifact=ArtifactService(r).record(step_id=step,expected_result_digest=digest(design))
    design_digest=hashlib.sha256(design['text'].encode()).hexdigest()
    review=dict(kind='review',text='Synthetic independent review',reviewed_artifact_id=artifact,reviewed_digest=design_digest,verdict='PASS',findings=[])
    step,_=evidence(r,wf,'design_review',review)
    ArtifactService(r).record(step_id=step,expected_result_digest=digest(review))
    return dict(design_digest=design_digest,review_digest=digest(review))
