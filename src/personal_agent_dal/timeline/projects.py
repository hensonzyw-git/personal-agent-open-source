"""Reference-only project registration from authenticated executor evidence."""
from sqlalchemy import select
from personal_agent_dal.storage.timeline_models import (
    DevelopmentProjectAuthorization as Grant,DevelopmentProjectBinding as Binding,
    DevelopmentWorkspace as Workspace,DevelopmentDriverStep as Step,
)
from personal_agent_dal.storage.models import Feature
from personal_agent_dal.timeline.requests import digest


def catalog(requests,session,request_id):
    result=[]
    for grant in session.scalars(select(Grant).where(Grant.revoked==0,Grant.expires_at>requests.now()).order_by(Grant.grant_id)):
        value=requests._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
        if digest(value)!=grant.digest:raise ValueError('INPUT_INTEGRITY_FAILED')
        if value['request_id']==request_id:
            from personal_agent_dal.timeline.phone_authorization import execution_policy
            try:execution_policy(requests,session,grant)
            except ValueError:continue
            result.append(dict(candidate_key=grant.grant_id,project_id=grant.project_id,grant_id=grant.grant_id,
                display_name=value['display_name'],kind=value['kind']))
    return result


def accept_project(driver,s,wf,step,result,execution):
    binding=s.get(Binding,wf.workflow_id)
    grant=s.get(Grant,binding.grant_id)
    authorization=driver.r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
    if result['project_id']!=binding.project_id or result['grant_digest']!=grant.digest:
        raise ValueError('PROJECT_BINDING_MISMATCH')
    if step.phase=='project_registration':
        if result['policy']!=authorization['registration_policy']:raise ValueError('REGISTRATION_POLICY_MISMATCH')
        if result['policy']=='github_issue' and 'remote_issue' not in authorization['actions']:
            raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
        wf.phase='workspace_prepare';wf.version+=1
    else:
        manifest=result['manifest']
        if manifest['kind']!=authorization['kind'] or manifest['project_id']!=binding.project_id:
            raise ValueError('PROJECT_BINDING_MISMATCH')
        if manifest['kind']=='local_new' and not {'create','local_init'}<=set(authorization['actions']):
            raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
        if s.get(Workspace,wf.workflow_id):raise ValueError('WORKSPACE_ALREADY_BOUND')
        s.add(Workspace(workflow_id=wf.workflow_id,reservation_id=manifest['reservation_id'],generation=manifest['generation'],
            directory_digest=manifest['directory_digest'],base_sha=manifest['base_sha'],toolchain_digest=manifest['toolchain_digest'],
            receipt_digest=execution.receipt_digest,sealed_manifest=driver.r._seal(Workspace,wf.workflow_id,'sealed_manifest',manifest)))
        # Compatibility reference only: no plaintext intake and no legacy queue.
        feature_id='workflow:'+wf.workflow_id
        now=driver.r.now()
        s.add(Feature(feature_id=feature_id,schema_version='dal.feature/1.0',version=1,state='intake',
            repository_id=binding.project_id,base_sha=manifest['base_sha'],decision_frontier_version=1,
            policy_version='dal.timeline-workflow/1.0',capability_epoch=1,
            external_effect_inventory_sha256=digest([]),trace_id=wf.workflow_id,created_at=now,updated_at=now))
        s.flush();wf.feature_id=feature_id;wf.phase='researching';wf.version+=1
    driver._event(s,wf,'workflow.project_prepared','项目准备结果已校验并保存。')


def bind_phone_choice(requests,session,workflow,artifact_id,candidates):
    """Reuse the explicit phone project choice, never invent a new approval."""
    from personal_agent_dal.storage.timeline_models import DevelopmentAuthorizationPolicy as Policy,DevelopmentAuthorizationProposal as Proposal,DevelopmentGate as Gate,DevelopmentRequest as Request
    if len(candidates)!=1 or session.get(Binding,workflow.workflow_id) is not None:return False
    selected=candidates[0];grant=session.get(Grant,selected['grant_id'])
    policy=session.get(Policy,selected['grant_id'])
    gate=session.get(Gate,workflow.workflow_id)
    if (grant is None or policy is None or policy.workflow_id!=workflow.workflow_id
        or gate is None or gate.mode!='open' or workflow.status!='active'):return False
    proposal=session.get(Proposal,policy.proposal_id)
    if proposal is None or proposal.status!='granted':return False
    # Full catalog rechecks scope, revocation, template and request binding.
    if catalog(requests,session,workflow.request_id)!=candidates:return False
    session.add(Binding(workflow_id=workflow.workflow_id,project_id=grant.project_id,grant_id=grant.grant_id,
        grant_version=grant.version,route_artifact_id=artifact_id,candidate_digest=digest(candidates),
        sealed_binding=requests._seal(Binding,workflow.workflow_id,'sealed_binding',dict(candidate=selected,grant_digest=grant.digest))))
    workflow.phase='project_registration';workflow.version+=1
    requests._append_event(session,session.get(Request,workflow.request_id),'workflow.project_bound',
        dict(summary='继续使用你在手机授权的项目，正在准备开发工作区。',status=workflow.status,phase=workflow.phase))
    return True
