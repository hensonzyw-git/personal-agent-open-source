"""One review unit per persisted stage revision; no provider or new dispatch."""
from sqlalchemy import select
from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step
from personal_agent_dal.timeline.requests import digest


def records(requests, session, workflow_id, stage):
    steps=list(session.scalars(select(Step).where(Step.workflow_id==workflow_id,
        Step.stage_id==stage.stage_id,Step.stage_revision==stage.revision,Step.phase=='code_review')
        .order_by(Step.expected_version,Step.step_id)))
    completed=[]
    for step in steps:
        if step.status!='completed':continue
        result=requests._open(Step,step.step_id,'sealed_result',step.sealed_result)
        if digest(result)!=step.result_digest:raise ValueError('INPUT_INTEGRITY_FAILED')
        from personal_agent_dal.timeline.stages import StageService
        StageService(requests)._execution_receipt(session,step)
        completed.append((step,result))
    return steps,completed


def review_context(requests,session,workflow_id,stage,*,reject_unchanged=True):
    _,completed=records(requests,session,workflow_id,stage)
    previous=None
    if completed:
        step,result=completed[-1]
        if reject_unchanged and any(r['candidate']['tree_sha']==stage.tree_sha for _,r in completed):
            raise ValueError('REVIEW_CANDIDATE_UNCHANGED')
        from personal_agent_dal.timeline.stages import StageService
        execution=StageService(requests)._execution_receipt(session,step)
        previous=dict(candidate=result['candidate'],findings=result['findings'],receipt_digest=execution.receipt_digest)
    return dict(unit_id=stage.stage_id+':'+str(stage.revision),
        mode='incremental' if completed else 'initial',previous=previous)


def review_summary(requests,session,workflow_id,stage):
    steps,completed=records(requests,session,workflow_id,stage)
    counts=[r.get('runtime_usage',{}).get('provider_requests') for _,r in completed]
    provider_requests=sum(counts) if len(steps)==len(completed) and all(n is not None for n in counts) else None
    modes=[];incremental_candidates=set()
    for step in steps:
        value=requests._open(Step,step.step_id,'sealed_input',step.sealed_input)
        if digest(value)!=step.input_digest:raise ValueError('INPUT_INTEGRITY_FAILED')
        mode=(value.get('stage',{}).get('review') or {}).get('mode')
        modes.append(mode)
        if mode=='incremental':incremental_candidates.add(value['stage']['candidate']['tree_sha'])
    return dict(unit_id=stage.stage_id+':'+str(stage.revision),initial_reviews=int('initial' in modes),
        incremental_reviews=len(incremental_candidates),legacy_reviews=sum(m is None for m in modes),
        execution_attempts=sum(step.attempt_id is not None for step in steps),
        incomplete_attempts=sum(step.attempt_id is not None and step.status!='completed' for step in steps),provider_requests=provider_requests)
