from personal_agent_dal.timeline.stages import plan_stages
"""Stage transitions composed inside the owning workflow transaction."""
from sqlalchemy import select
from personal_agent_dal.storage.timeline_models import (
    DevelopmentStagePlan as Plan,DevelopmentStage as Stage,DevelopmentArtifact as Artifact,
    DevelopmentStageWriter as Writer,DevelopmentDriverStep as Step,DevelopmentRoleSnapshot as Snapshot,
)
from personal_agent_dal.timeline.stages import StageService
from personal_agent_dal.timeline.requests import digest
import json


def prepare_stage(driver,s,wf):
    stages=StageService(driver.r)
    plan=s.scalar(select(Plan).where(Plan.workflow_id==wf.workflow_id).order_by(Plan.revision.desc()))
    design=s.scalar(select(Artifact).where(Artifact.workflow_id==wf.workflow_id,Artifact.kind=='design').order_by(Artifact.revision.desc()))
    # A scope-changing revision returns through PRD and design review. The new
    # design cannot silently reuse the previous plan's completed stages.
    replacing=plan is not None and design is not None and design.body_sha256!=plan.design_digest and wf.phase=='stage_planning'
    if replacing:
        # Engineering revision plans have their own reviewed digest; their
        # earlier design artifact remains a valid historical input.
        source=s.scalar(select(Artifact).where(Artifact.workflow_id==wf.workflow_id,Artifact.body_sha256==plan.design_digest))
        replacing=source is not None and s.get(Step,design.source_step_id).expected_version>s.get(Step,source.source_step_id).expected_version
    if plan is None or replacing:
        review=s.scalar(select(Artifact).where(Artifact.workflow_id==wf.workflow_id,Artifact.kind=='review').order_by(Artifact.revision.desc()))
        if design is None or review is None:
            driver._block(s,wf,'REVIEW_SOURCE_REQUIRED');return None
        body=driver.r._open(Artifact,design.artifact_id,'sealed_body',design.sealed_body)
        previous=plan
        plan_id=stages.freeze(workflow_id=wf.workflow_id,revision=plan.revision+1 if plan else 1,design_digest=design.body_sha256,
            review_digest=review.source_receipt_digest,body=body['plan'],_session=s)
        if previous:
            for old in plan_stages(s,previous.plan_id):old.state='invalidated';old.state_version+=1
        s.flush();plan=s.get(Plan,plan_id)
    stages.ready(plan.plan_id,_session=s);s.flush()
    rows=plan_stages(s,plan.plan_id)
    if all(row.state=='committed' for row in rows):
        from personal_agent_dal.storage.timeline_models import DevelopmentWorkspace
        workspace=s.get(DevelopmentWorkspace,wf.workflow_id)
        manifest=driver.r._open(DevelopmentWorkspace,wf.workflow_id,'sealed_manifest',workspace.sealed_manifest)
        wf.phase='delivery_publication' if manifest['kind']=='existing' else 'delivery_prepare'
        wf.version+=1;return None
    if any(row.state=='blocked' for row in rows):
        driver._block(s,wf,'REVIEW_BUDGET_EXHAUSTED');return None
    writer=s.get(Writer,wf.workflow_id)
    if writer:
        row=s.get(Stage,(writer.stage_id,writer.stage_revision))
    else:
        row=next((r for r in rows if r.state=='ready'),None)
        if row is None:
            driver._block(s,wf,'STAGE_DEPENDENCIES_REQUIRED');return None
        stages.claim(wf.workflow_id,row.stage_id,row.revision,expected_version=row.state_version,_session=s)
    phases={'coding':'coding','fixing':'fix','verifying':'verify','reviewing':'code_review','commit_ready':'stage_commit'}
    if row.state not in phases:
        driver._block(s,wf,'REVIEW_BUDGET_EXHAUSTED');return None
    if wf.phase!=phases[row.state]:wf.phase=phases[row.state];wf.version+=1
    if wf.phase=='code_review':
        previous=s.scalar(select(Step).where(Step.workflow_id==wf.workflow_id,Step.stage_id==row.stage_id,
            Step.stage_revision==row.revision,Step.phase.in_(('coding','fix')),Step.status=='completed').order_by(Step.expected_version.desc()))
        current=driver.roles.snapshot(workflow_id=wf.workflow_id,_session=s)
        snapshot=s.get(Snapshot,previous.snapshot_id) if previous else None
        if snapshot is None or json.loads(snapshot.body)['roles']['coder']['model']==current['roles']['reviewer']['model']:
            driver._block(s,wf,'REVIEW_NOT_INDEPENDENT');return None
    if row.base_sha is None:
        from personal_agent_dal.storage.timeline_models import DevelopmentWorkspace
        workspace=s.get(DevelopmentWorkspace,wf.workflow_id)
        last=s.scalar(select(Step).where(Step.workflow_id==wf.workflow_id,Step.phase=='stage_commit',Step.status=='completed').order_by(Step.expected_version.desc()))
        result=driver.r._open(Step,last.step_id,'sealed_result',last.sealed_result) if last else None
        base=result['commit_sha'] if result else workspace.base_sha if workspace else None
        if base is None:raise ValueError('WORKSPACE_REQUIRED')
        row.base_sha=row.head_sha=base
    goal=driver.r._open(Stage,row.stage_id+':'+str(row.revision),'sealed_goal',row.sealed_goal)
    from personal_agent_dal.timeline.commit_reviews import review_context
    review=None
    if wf.phase in ('code_review','fix'):
        try:review=review_context(driver.r,s,wf.workflow_id,row,reject_unchanged=wf.phase=='code_review')
        except ValueError as exc:
            if str(exc)!='REVIEW_CANDIDATE_UNCHANGED':raise
            row.state='blocked';row.state_version+=1
            writer=s.get(Writer,wf.workflow_id)
            if writer is not None:s.delete(writer)
            driver._block(s,wf,'REVIEW_CANDIDATE_UNCHANGED');return None
    return dict(stage_id=row.stage_id,revision=row.revision,state_version=row.state_version,
        plan_digest=plan.dag_digest,dependency_digest=row.dependency_digest,goal=goal,
        candidate=dict(base_sha=row.base_sha,head_sha=row.head_sha,tree_sha=row.tree_sha),
        review_fix_cycle=row.review_fix_cycle,review=review)


def accept_stage(driver,s,wf,step,result,execution):
    stages=StageService(driver.r)
    inputs=driver.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
    stage=inputs['stage']
    row=s.get(Stage,(step.stage_id,step.stage_revision))
    if row is None or row.state_version!=stage['state_version']:raise ValueError('STALE_BINDING')
    candidate=result['candidate']
    args=dict(expected_version=row.state_version,_session=s,**candidate)
    if step.phase in ('coding','fix'):
        if candidate['base_sha']!=row.head_sha or candidate['head_sha']!=row.head_sha:raise ValueError('CANDIDATE_BASE_CHANGED')
        stages.candidate(row.stage_id,row.revision,**args)
    elif step.phase=='verify':
        stages.verified(row.stage_id,row.revision,receipt_digest=execution.receipt_digest,passed=result['passed'],**args)
    elif step.phase=='code_review':
        stages.reviewed(row.stage_id,row.revision,receipt_digest=execution.receipt_digest,passed=result['passed'],**args)
    elif step.phase=='stage_commit':
        stages.committed(row.stage_id,row.revision,receipt_digest=execution.receipt_digest,**args)
    else:raise ValueError('RESULT_SOURCE_INVALID')
    wf.phase='stage_planning';wf.version+=1
    driver._event(s,wf,'workflow.stage_updated','阶段结果已校验并保存。',stage_id=row.stage_id,stage_revision=row.revision,stage_state=row.state)
