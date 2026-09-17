"""Durable missing-authority state, never an implicit grant from project selection."""
from datetime import timedelta
from personal_agent_dal.storage.timeline_models import DevelopmentAuthorizationRequest as Pending,DevelopmentRequest as Request
from personal_agent_dal.timeline.projects import catalog


def require_catalog(driver,session,wf):
    candidates=catalog(driver.r,session,wf.request_id)
    if candidates:return candidates
    pending=session.get(Pending,wf.workflow_id)
    if pending is None:
        pending=Pending(workflow_id=wf.workflow_id,request_version=session.get(Request,wf.request_id).version,
            status='pending',expires_at=driver.r.now()+timedelta(hours=24),
            sealed_scope=driver.r._seal(Pending,wf.workflow_id,'sealed_scope',dict(project_candidate=None,
                actions=['read'],budget_seconds=None,root=None,reason='Select and explicitly authorize a registered project before discovery.')))
        session.add(pending)
        driver._event(session,wf,'workflow.authorization_required','需要先明确并授权项目、目录、动作、预算和时效；选择项目本身不会授予权限。')
    driver._block(session,wf,'PROJECT_AUTHORIZATION_REQUIRED')
    return []


def recover(driver,session,wf):
    if wf.status!='blocked':return
    if wf.blocker_reason=='PROJECT_AUTHORIZATION_REQUIRED' and wf.phase=='project_routing':
        pending=session.get(Pending,wf.workflow_id)
        if pending and pending.status=='pending' and pending.expires_at<=driver.r.now():
            pending.status='expired'
            driver._event(session,wf,'workflow.authorization_expired','项目授权请求已过期，需要重新核对授权范围。')
        if not pending or pending.status!='granted' or not catalog(driver.r,session,wf.request_id):return
    elif wf.blocker_reason=='ROLE_UNAVAILABLE':
        if driver.roles is None:return
        try:driver.roles.snapshot(workflow_id=wf.workflow_id,_session=session)
        except (ValueError,OSError):return
    else:return
    wf.status='active';wf.blocker_reason=None;wf.version+=1
    driver._event(session,wf,'workflow.recovered','所需配置已就绪，继续校验当前版本。')
