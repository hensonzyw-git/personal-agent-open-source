"""Deterministic revision closure; model proposals never reopen the writer gate."""
from sqlalchemy import select,func
from personal_agent_core.ids import new_id
from personal_agent_dal.storage.timeline_models import (
    DevelopmentStagePlan as Plan, DevelopmentStage as Stage, DevelopmentStageMembership as Member,
    DevelopmentStageDependency as Edge, DevelopmentDependencySatisfaction as Satisfaction,
    DevelopmentArtifact as Artifact, DevelopmentGate as Gate, DevelopmentDelivery as Delivery,
    DevelopmentStageWriter as Writer, DevelopmentDriverStep as Step,
)
from personal_agent_dal.timeline.stages import plan_stages
from personal_agent_dal.timeline.requests import digest


def context(requests,session,wf,targets=None):
    plan=session.scalar(select(Plan).where(Plan.workflow_id==wf.workflow_id).order_by(Plan.revision.desc()))
    if plan is None:raise ValueError('REVISION_BASE_REQUIRED')
    rows=plan_stages(session,plan.plan_id)
    if not rows or any(row.state!='committed' for row in rows):raise ValueError('REVISION_BASE_REQUIRED')
    edges=list(session.scalars(select(Edge).where(Edge.plan_id==plan.plan_id)))
    versions={row.stage_id:row.revision for row in rows}
    affected=set()
    if targets is not None:
        for target in targets:
            if versions.get(target['stage_id'])!=target['revision']:raise ValueError('REVISION_TARGET_INVALID')
            affected.add(target['stage_id'])
        while True:
            enlarged=affected|{edge.downstream_id for edge in edges if edge.upstream_id in affected}
            if enlarged==affected:break
            affected=enlarged
    delivery=session.scalar(select(Delivery).where(Delivery.workflow_id==wf.workflow_id).join(Artifact,Delivery.artifact_id==Artifact.artifact_id).order_by(Artifact.revision.desc()))
    if delivery is None:raise ValueError('REVISION_BASE_REQUIRED')
    manifest=requests._open(Delivery,delivery.delivery_id,'sealed_manifest',delivery.sealed_manifest)
    return dict(plan_id=plan.plan_id,plan_digest=plan.dag_digest,baseline=manifest['head_sha'],
        delivery_digest=delivery.manifest_digest,affected=sorted(affected),
        stages=[dict(stage_id=row.stage_id,revision=row.revision,state_version=row.state_version,
            goal=requests._open(Stage,row.stage_id+':'+str(row.revision),'sealed_goal',row.sealed_goal),
            head_sha=row.head_sha,tree_sha=row.tree_sha,commit_digest=row.commit_digest,
            verification_digest=row.verification_digest,review_digest=row.review_digest) for row in rows],
        edges=[dict(upstream_id=e.upstream_id,downstream_id=e.downstream_id) for e in edges])


def accept_plan(driver,s,wf,result):
    context(driver.r,s,wf,result['stages'])
    if result['scope_changed']:
        # Scope changes return to the human PRD gate; no source execution is
        # permitted until a fresh design and independent review have completed.
        wf.phase='prd_authoring'
        gate=s.get(Gate,wf.workflow_id);gate.mode='open';gate.epoch+=1;gate.version+=1
    else:wf.phase='delivery_revision_review'
    wf.version+=1


def accept_review(driver,s,wf,step,result):
    if result['verdict']!='PASS':
        wf.phase='delivery_revision_planning';wf.version+=1;return
    proposal=s.get(Artifact,result['reviewed_artifact_id'])
    if proposal is None or proposal.workflow_id!=wf.workflow_id or proposal.kind!='revision_plan':raise ValueError('REVIEW_BINDING_INVALID')
    body=driver.r._open(Artifact,proposal.artifact_id,'sealed_body',proposal.sealed_body)
    current=context(driver.r,s,wf,body['stages'])
    inputs=driver.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
    if inputs['revision_context']!=current or body['scope_changed']:raise ValueError('STALE_BINDING')
    if s.get(Writer,wf.workflow_id) or s.scalar(select(Step.step_id).where(Step.workflow_id==wf.workflow_id,
        Step.status.in_(('dispatch_started','result_unknown'))).limit(1)):raise ValueError('RECONCILIATION_REQUIRED')
    old=s.get(Plan,current['plan_id']);rows=plan_stages(s,old.plan_id)
    plan_id=new_id();new_versions={}
    for row in rows:
        new_versions[row.stage_id]=(s.scalar(select(func.max(Stage.revision)).where(Stage.stage_id==row.stage_id))+1
            if row.stage_id in current['affected'] else row.revision)
    plan=Plan(plan_id=plan_id,workflow_id=wf.workflow_id,revision=old.revision+1,
        design_digest=proposal.body_sha256,review_digest=step.result_digest,
        dag_digest=digest(dict(previous=old.dag_digest,proposal=proposal.source_receipt_digest,context=current,versions=new_versions)))
    s.add(plan);s.flush()
    for ordinal,row in enumerate(rows):
        revision=new_versions[row.stage_id]
        if row.stage_id in current['affected']:
            goal=driver.r._open(Stage,row.stage_id+':'+str(row.revision),'sealed_goal',row.sealed_goal)
            goal=dict(goal,revision=revision)
            s.add(Stage(stage_id=row.stage_id,revision=revision,plan_id=plan_id,ordinal=ordinal,state='pending',state_version=1,
                review_fix_cycle=0,sealed_goal=driver.r._seal(Stage,row.stage_id+':'+str(revision),'sealed_goal',goal)))
            row.state='invalidated';row.state_version+=1
        s.flush()
        s.add(Member(plan_id=plan_id,stage_id=row.stage_id,stage_revision=revision,ordinal=ordinal))
    s.flush()
    for oldedge in s.scalars(select(Edge).where(Edge.plan_id==old.plan_id)):
        edge=Edge(edge_id=new_id(),plan_id=plan_id,upstream_id=oldedge.upstream_id,upstream_revision=new_versions[oldedge.upstream_id],
            downstream_id=oldedge.downstream_id,downstream_revision=new_versions[oldedge.downstream_id])
        s.add(edge);s.flush()
        upstream=s.get(Stage,(edge.upstream_id,edge.upstream_revision))
        if upstream.state=='committed':
            s.add(Satisfaction(satisfaction_id=new_id(),edge_id=edge.edge_id,commit_digest=upstream.commit_digest,
                commit_sha=upstream.head_sha,tree_sha=upstream.tree_sha,upstream_state_version=upstream.state_version,observed_at=driver.r.now()))
    gate=s.get(Gate,wf.workflow_id)
    gate.mode='open';gate.epoch+=1;gate.version+=1
    wf.phase='stage_planning';wf.version+=1
    driver._event(s,wf,'workflow.revision_approved','修订计划已独立审查，已建立新的阶段代次。',plan_id=plan_id)
