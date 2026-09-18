"""Signed synthetic providers exercise the actual reducer from intake to acceptance.

This is offline workflow evidence, not native model or repository evidence.
"""
from datetime import timedelta
import pytest
from sqlalchemy import select
from personal_agent_dal.timeline.requests import digest,TimelineRefusal
from personal_agent_dal.timeline.decisions import DecisionService
from personal_agent_dal.timeline.operator import Authorization,register_authorization
from personal_agent_dal.storage.timeline_models import (
    DevelopmentDriverStep as Step,DevelopmentArtifact as Artifact,DevelopmentDecisionRequest as Decision,
    DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,DevelopmentWorkspace as Workspace,
)
from personal_agent_dal.storage.models import Feature
from tests.dal.test_timeline_requests import world
from tests.dal.test_timeline_driver import configured
from tests.dal.test_timeline_stages import plan


def launch(driver,wf):
    for _ in range(3):
        prepared=driver.tick(wf)
        if 'step_id' in prepared:break
    assert 'step_id' in prepared,prepared
    return driver.dispatch(prepared['step_id'],admission={'test':'signed-by-helper'})


def accept(driver,item,result):
    return driver.accept(item['step_id'],attempt_id=item['attempt_id'],result=result)


def decision(r,wf,kind,command,text):
    with r.sessions() as s:
        row=s.scalar(select(Decision).where(Decision.workflow_id==wf,Decision.kind==kind,Decision.status=='pending'))
        return dict(command_id=command,source_message_ref=command+'-source',subject='device:synthetic',
            decision_id=row.decision_id,binding_digest=row.binding_digest,text=text,expected_kind=kind)


def delivery_ready(world,*,before_commit=False,existing=False,before_delivery=False,before_code=False):
    r,driver,wf,item=configured(world)
    accept(driver,item,dict(kind='clarification',text='Synthetic',ready=True,questions=[],acceptance=['a','b']))
    grant=Authorization(grant_id='local-grant',request_id=wf,project_id='local-project',subject='device:synthetic',
        approval_evidence_ref='explicit-approval',root='/synthetic/root',kind='existing' if existing else 'local_new',display_name='Synthetic local project',
        actions=['read','write','push','pr'] if existing else ['read','write','create','local_init'],remote_repository='synthetic/project' if existing else None,budget_seconds=600,expires_at=r.now()+timedelta(hours=1),registration_policy='local_tracker')
    register_authorization(r,grant,actor='test-operator')
    item=launch(driver,wf)
    accept(driver,item,dict(kind='project_route',text='Synthetic routing',candidates=item['input']['project_catalog']))
    DecisionService(r).process(**decision(r,wf,'project_selection','select','选第一个项目'))
    item=launch(driver,wf)
    accept(driver,item,dict(kind='registration',text='Synthetic tracker',project_id='local-project',grant_digest=item['input']['project']['grant_digest'],policy='local_tracker',tracker_receipt='a'*64))
    item=launch(driver,wf)
    workspace=dict(kind='existing' if existing else 'local_new',project_id='local-project',reservation_id='reservation',generation=1,directory_digest='a'*64,
        base_sha='1'*40,toolchain_digest='b'*64,branch='refs/heads/codex/synthetic')
    accept(driver,item,dict(kind='workspace',text='Synthetic prepared workspace',project_id='local-project',grant_digest=item['input']['project']['grant_digest'],manifest=workspace))
    item=launch(driver,wf)
    accept(driver,item,dict(kind='research',text='Synthetic research',sources=[dict(ref='synthetic-source',digest='a'*64)],unknowns=[],workspace_receipt_digest=item['input']['workspace_receipt_digest']))
    item=launch(driver,wf);accept(driver,item,dict(kind='prd',text='Synthetic approved product scope'))
    DecisionService(r).process(**decision(r,wf,'prd','approve-prd','通过'))
    item=launch(driver,wf)
    prd=next(a for a in item['input']['artifacts'] if a['kind']=='prd')
    accept(driver,item,dict(kind='design',text='Synthetic design',prd_digest=prd['digest'],plan=plan()))
    item=launch(driver,wf)
    design=next(a for a in item['input']['artifacts'] if a['kind']=='design')
    accept(driver,item,dict(kind='review',text='Synthetic independent design review',reviewed_artifact_id=design['artifact_id'],reviewed_digest=design['digest'],verdict='PASS',findings=[]))
    commits=[]
    for n in (2,3):
        item=launch(driver,wf);assert item['input']['phase']=='coding'
        if before_code and (before_code is True or before_code==n-1):return r,driver,wf,item
        base=item['input']['stage']['candidate']['head_sha']
        candidate=dict(base_sha=base,head_sha=base,tree_sha=str(n+3)*40)
        accept(driver,item,dict(kind='candidate',text='Synthetic code',candidate=candidate))
        item=launch(driver,wf);assert item['input']['phase']=='verify'
        accept(driver,item,dict(kind='verification',text='Synthetic verifier',candidate=candidate,passed=True,
            commands=[dict(argv_digest='a'*64,output_digest='b'*64,exit_code=0)]))
        item=launch(driver,wf);assert item['input']['phase']=='code_review'
        accept(driver,item,dict(kind='code_review',text='Synthetic independent code review',candidate=candidate,passed=True,findings=[]))
        item=launch(driver,wf);assert item['input']['phase']=='stage_commit'
        if before_commit:return r,driver,wf,item,candidate
        sha=str(n)*40
        accept(driver,item,dict(kind='commit',text='Synthetic commit readback',candidate=candidate,committed=True,commit_sha=sha,parent_sha=base))
        commits.append(dict(sha=sha,parent=base,tree=candidate['tree_sha']))
    if before_delivery:return r,driver,wf,commits
    item=launch(driver,wf);assert item['input']['phase']=='delivery_prepare'
    manifest=dict(kind='local',stage_manifest=item['input']['delivery'],head_sha='3'*40,tree_sha='6'*40,commits=commits,clean=True,untracked_digest=digest([]))
    accept(driver,item,dict(kind='delivery',text='Synthetic local delivery evidence',manifest=manifest))
    return r,driver,wf,manifest


def test_complete_signed_local_workflow_requires_fresh_probe_and_terminal_fence(world):
    r,driver,wf,manifest=delivery_ready(world)
    command=decision(r,wf,'delivery','accept-delivery','通过')
    with pytest.raises(TimelineRefusal,match='DELIVERY_PROBE_PENDING'):DecisionService(r).process(**command)
    with r.sessions() as s:
        assert s.get(Workflow,wf).status=='active'
        before=s.get(Feature,s.get(Workflow,wf).feature_id).version
    item=launch(driver,wf);assert item['input']['phase']=='delivery_probe'
    accept(driver,item,dict(kind='delivery_probe',text='Synthetic fresh observer',nonce=item['input']['probe']['nonce'],
        manifest_digest=digest(manifest),matches=True,observed_at=int(r.now().timestamp())))
    result=DecisionService(r).process(**command)
    assert result['workflow_status']=='completed'
    assert DecisionService(r).process(**command)==result
    with r.sessions() as s:
        workflow=s.get(Workflow,wf);feature=s.get(Feature,workflow.feature_id)
        assert (feature.state,feature.version)==('intake',before)
        assert s.get(Gate,wf).mode=='delivered'
        assert workflow.accepted_delivery_id==result['delivery_id']
    assert driver.tick(wf)['phase']=='accepted'


def test_delivery_changes_keep_gate_closed_until_independent_revision_review(world):
    from personal_agent_dal.storage.timeline_models import DevelopmentStagePlan as Plan,DevelopmentStage as Stage
    from personal_agent_dal.timeline.stages import plan_stages
    r,driver,wf,manifest=delivery_ready(world)
    old=decision(r,wf,'delivery','change-delivery','修改：补充第一阶段的边界处理')
    DecisionService(r).process(**old)
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_revision_planning'
    targets=[dict(stage_id=manifest['stage_manifest']['stages'][0]['stage_id'],revision=1)]
    accept(driver,item,dict(kind='revision_plan',text='Synthetic repair within approved scope',stages=targets,scope_changed=False))
    with r.sessions() as s:assert s.get(Gate,wf).mode=='paused'
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_revision_review'
    assert len(item['input']['revision_context']['affected'])==2
    proposal=next(a for a in item['input']['artifacts'] if a['kind']=='revision_plan')
    accept(driver,item,dict(kind='review',text='Synthetic independent revision review',reviewed_artifact_id=proposal['artifact_id'],reviewed_digest=proposal['digest'],verdict='PASS',findings=[]))
    with r.sessions() as s:
        plan=s.scalar(select(Plan).where(Plan.workflow_id==wf).order_by(Plan.revision.desc()))
        assert plan.revision==2
        assert [row.revision for row in plan_stages(s,plan.plan_id)]==[2,2]
        assert s.get(Gate,wf).mode=='open'
        assert s.get(Stage,(targets[0]['stage_id'],1)).state=='invalidated'
    item=launch(driver,wf)
    assert item['input']['phase']=='coding'
    assert item['input']['stage']['candidate']['head_sha']==manifest['head_sha']
    stale=dict(old,command_id='stale-approval',source_message_ref='stale-source',text='通过')
    with pytest.raises(ValueError,match='STALE_BINDING'):DecisionService(r).process(**stale)


def test_rejected_delivery_resume_requires_new_readback_proposal(world):
    from personal_agent_dal.timeline.recovery import RecoveryService
    r,driver,wf,manifest=delivery_ready(world)
    old=decision(r,wf,'delivery','reject-delivery','拒绝')
    DecisionService(r).process(**old)
    with r.sessions() as s:
        workflow=s.get(Workflow,wf)
        assert workflow.status=='paused'
        version=workflow.version
    result=RecoveryService(r).process(command_id='resume',source_message_ref='resume-source',subject='device:synthetic',
        workflow_id=wf,expected_version=version,action='resume',text='继续开发')
    assert result['phase']=='delivery_prepare'
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_prepare'
    with r.sessions() as s:
        assert s.get(Gate,wf).mode=='paused'
        assert not list(s.scalars(select(Decision).where(Decision.workflow_id==wf,Decision.status=='pending')))


def test_lost_commit_response_is_reconciled_by_exact_object_without_reexecution(world):
    from personal_agent_dal.timeline.stop_observation import apply
    from personal_agent_dal.github.workflow_objects import object_sha
    from personal_agent_dal.storage.timeline_models import DevelopmentExecution,DevelopmentStage
    r,driver,wf,item,candidate=delivery_ready(world,before_commit=True)
    stage=item['input']['stage'];stamp=item['input']['prepared_at']
    raw=(f"tree {candidate['tree_sha']}\nparent {candidate['head_sha']}\n"
        f"author DAL <dal@localhost> {stamp} +0000\ncommitter DAL <dal@localhost> {stamp} +0000\n\n"
        f"DAL stage {stage['stage_id']}/{stage['revision']}\n").encode()
    observation=dict(process_exited=True,head_sha=object_sha('commit',raw),tree_sha=candidate['tree_sha'])
    proof=driver.proof(item['step_id'],'dal.workflow-stop/1.0',observation)
    def recover(s):
        step=s.get(Step,item['step_id'])
        execution=driver.authority.verify(s,step,proof,domain='dal.workflow-stop/1.0',payload=observation,observation_only=True)
        apply(driver,s,step,execution,observation,proof)
    driver._write(recover)
    with r.sessions() as s:
        assert s.get(Step,item['step_id']).status=='completed'
        assert s.get(DevelopmentStage,(stage['stage_id'],stage['revision'])).head_sha==observation['head_sha']
        assert s.get(Workflow,wf).phase=='stage_planning'


def test_unknown_source_write_cannot_be_resumed_from_stop_without_readback(world):
    from personal_agent_dal.timeline.stop_observation import apply
    from personal_agent_dal.timeline.recovery import RecoveryService
    from personal_agent_dal.storage.timeline_models import DevelopmentExecution
    r,driver,wf,item,candidate=delivery_ready(world,before_commit=True)
    observation=dict(process_exited=True,head_sha=None,tree_sha=None)
    def stop(s):
        step=s.get(Step,item['step_id'])
        execution=s.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step.step_id))
        apply(driver,s,step,execution,observation,'synthetic-stop')
    driver._write(stop)
    with r.sessions() as s:
        assert s.get(Step,item['step_id']).status=='result_unknown'
        version=s.get(Workflow,wf).version
    result=RecoveryService(r).process(command_id='cannot-replay',source_message_ref='cannot-replay-source',subject='device:synthetic',workflow_id=wf,
        expected_version=version,action='resume',text='继续开发')
    assert result['reason']=='RECONCILIATION_REQUIRED'


@pytest.mark.parametrize('failed_phase',['verify','code_review'])
@pytest.mark.parametrize('stage_ordinal',[1,2])
def test_exhausted_stage_releases_writer_and_resume_requires_review(world,failed_phase,stage_ordinal):
    from personal_agent_dal.storage.timeline_models import DevelopmentStage as Stage, DevelopmentStageWriter as Writer
    from personal_agent_dal.timeline.recovery import RecoveryService
    r,driver,wf,item=delivery_ready(world,before_code=stage_ordinal)
    stage=item['input']['stage'];base=stage['candidate']['head_sha']
    candidate=dict(base_sha=base,head_sha=base,tree_sha='5'*40)
    for cycle in range(3):
        accept(driver,item,dict(kind='candidate',text='Synthetic code',candidate=candidate))
        item=launch(driver,wf)
        accept(driver,item,dict(kind='verification',text='Synthetic verifier',candidate=candidate,passed=failed_phase!='verify',
            commands=[dict(argv_digest='a'*64,output_digest='b'*64,exit_code=1 if failed_phase=='verify' else 0)]))
        if failed_phase=='code_review':
            item=launch(driver,wf)
            accept(driver,item,dict(kind='code_review',text='Synthetic failed review',candidate=candidate,passed=False,findings=['Synthetic defect']))
        if cycle<2:item=launch(driver,wf)
    assert driver.tick(wf)['status']=='blocked'
    with r.sessions() as s:
        assert s.get(Writer,wf) is None
        row=s.get(Stage,(stage['stage_id'],1))
        assert row.state=='blocked' and row.review_fix_cycle==3
        version=s.get(Workflow,wf).version
    RecoveryService(r).process(command_id='resume-budget',source_message_ref='resume-source',subject='device:synthetic',
        workflow_id=wf,expected_version=version,action='resume',text='继续开发')
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_revision_planning'
    assert item['input']['revision_context']['baseline']==base
    with r.sessions() as s:assert s.get(Gate,wf).mode=='paused'
    accept(driver,item,dict(kind='revision_plan',text='Synthetic reviewed repair',stages=[dict(stage_id=stage['stage_id'],revision=1)],scope_changed=False))
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_revision_review'
    proposal=next(a for a in item['input']['artifacts'] if a['kind']=='revision_plan')
    accept(driver,item,dict(kind='review',text='Synthetic independent review',reviewed_artifact_id=proposal['artifact_id'],reviewed_digest=proposal['digest'],verdict='PASS',findings=[]))
    item=launch(driver,wf)
    assert item['input']['phase']=='coding' and item['input']['stage']['revision']==2
    with r.sessions() as s:
        assert s.get(Stage,(stage['stage_id'],1)).review_fix_cycle==3
        assert s.get(Stage,(stage['stage_id'],1)).state=='invalidated'
