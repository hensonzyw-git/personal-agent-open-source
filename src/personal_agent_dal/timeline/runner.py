"""Bounded production preparation loop and authenticated Worker pull routes."""
import asyncio
import logging
import httpx
from sqlalchemy import select
from fastapi import BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest
from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow, DevelopmentDriverStep
from personal_agent_dal.timeline.driver import WorkflowDriver

log=logging.getLogger(__name__)


class WorkflowRunner:
    def __init__(self,driver):self.driver=driver;self.after=''

    def tick(self,limit=32):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('SCAN_LIMIT_INVALID')
        with self.driver.r.sessions() as s:
            workflows=list(s.scalars(select(DevelopmentWorkflow.workflow_id).where(
                ((DevelopmentWorkflow.status=='active')|((DevelopmentWorkflow.status=='blocked')&DevelopmentWorkflow.blocker_reason.in_(('ROLE_UNAVAILABLE','PROJECT_AUTHORIZATION_REQUIRED')))),DevelopmentWorkflow.workflow_id>self.after).order_by(DevelopmentWorkflow.workflow_id).limit(limit)))
        self.after=workflows[-1] if workflows else ''
        for workflow_id in workflows:
            try:self.driver.tick(workflow_id)
            except (ValueError, KeyError, TypeError):
                # A corrupt or stale task cannot kill scheduling for other tasks.
                log.warning('Workflow preparation refused; no dispatch performed')
        return len(workflows)

    async def run(self):
        while True:
            try:await asyncio.to_thread(self.tick)
            except (OSError, __import__('sqlalchemy').exc.OperationalError):
                log.warning('Workflow storage temporarily unavailable')
            await asyncio.sleep(5)


class StepRequest(Closed):
    step_id: Id


class LaunchRequest(StepRequest):
    assertion: str


class ResultRequest(LaunchRequest):
    attempt_id: Id
    result: dict


def mount_routes(app,endpoint,service,github_adapter=None):
    if endpoint is None:return
    driver=WorkflowDriver(endpoint.requests,roles=endpoint.roles,kill_switch=lambda:service.kill_switch,
        authority=getattr(endpoint,'execution_authority',None))
    endpoint.driver=driver
    endpoint.runner=WorkflowRunner(driver)

    def bound(step_id,worker_id):
        from personal_agent_dal.storage.timeline_models import DevelopmentExecution
        with driver.r.sessions() as s:
            row=s.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step_id))
            if row is None or row.worker_id!=worker_id:raise HTTPException(403,'WORKER_NOT_AUTHORIZED')

    @app.post('/workflow/publication')
    def publication(body:ResultRequest,background_tasks:BackgroundTasks,identity=Depends(service.auth)):
        bound(body.step_id,identity[0])
        from personal_agent_dal.timeline.publication import PublicationService
        try:
            result=PublicationService(driver,github_adapter).perform(step_id=body.step_id,worker_id=identity[0],assertion=body.assertion,payload=body.result,schedule=background_tasks.add_task)
            return JSONResponse(result,status_code=202 if result=={'status':'pending'} else 200)
        except (ValueError,OSError):raise HTTPException(409,'WORKFLOW_PUBLICATION_REFUSED') from None

    class ReconcileRequest(StepRequest):
        payload_digest: Digest

    from personal_agent_dal.service.app import _OperatorAuth
    @app.post('/operator/development/remote-effects/reconcile')
    def reconcile_effect(body:ReconcileRequest,actor=Depends(_OperatorAuth(service,'control'))):
        from personal_agent_dal.timeline.publication import PublicationService
        try:return PublicationService(driver,github_adapter).reconcile(step_id=body.step_id,payload_digest=body.payload_digest,actor=actor)
        except (ValueError,OSError,httpx.HTTPError):raise HTTPException(409,'WORKFLOW_RECONCILIATION_REFUSED') from None

    claim_cursors={}

    @app.post('/workflow/claim')
    def claim(identity=Depends(service.auth)):
        if service.kill_switch:raise HTTPException(503,'KILL_SWITCH_ACTIVE')
        with driver.r.sessions() as s:
            ids=list(s.scalars(select(DevelopmentDriverStep.step_id).where(
                DevelopmentDriverStep.status.in_(('prepared','dispatch_started','result_unknown')),
                DevelopmentDriverStep.step_id>claim_cursors.get(identity[0],''))
                .order_by(DevelopmentDriverStep.step_id).limit(100)))
        if not ids:claim_cursors[identity[0]]=''
        for step_id in ids:
            # reserve may return on the first candidate. Advance only through
            # attempted steps, so the rest of this page remain reachable.
            claim_cursors[identity[0]]=step_id
            try:return driver.reserve(step_id,worker_id=identity[0])
            except ValueError as exc:
                if str(exc)=='EXECUTION_BUDGET_EXHAUSTED':
                    def blocked(s):
                        step=s.get(DevelopmentDriverStep,step_id)
                        driver._block(s,s.get(DevelopmentWorkflow,step.workflow_id),'EXECUTION_BUDGET_EXHAUSTED')
                    driver._write(blocked)
                continue
        return {'binding':None}

    @app.post('/workflow/stop')
    def observe_stop(body:ResultRequest,identity=Depends(service.auth)):
        bound(body.step_id,identity[0])
        def work(session):
            step=session.get(DevelopmentDriverStep,body.step_id)
            if step is None or step.attempt_id!=body.attempt_id:raise ValueError('RESULT_SOURCE_INVALID')
            result=body.result
            if set(result)!={'process_exited','head_sha','tree_sha'} or result['process_exited'] is not True:
                raise ValueError('STOP_UNPROVEN')
            import re
            for field in ('head_sha','tree_sha'):
                if result[field] is not None and (not isinstance(result[field],str) or not re.fullmatch('[a-f0-9]{40}',result[field])):
                    raise ValueError('STOP_UNPROVEN')
            execution=driver.authority.verify(session,step,body.assertion,domain='dal.workflow-stop/1.0',payload=result,observation_only=True)
            from personal_agent_dal.storage.timeline_models import DevelopmentExecution
            execution.stop_receipt=driver.r._seal(DevelopmentExecution,execution.execution_id,'stop_receipt',
                dict(result=result,assertion=body.assertion))
            driver.authority.settle(execution)
            from personal_agent_dal.timeline.stop_observation import apply
            apply(driver,session,step,execution,result,body.assertion)
            return {'status':'observed','attempt_id':execution.execution_id}
        try:return driver._write(work)
        except ValueError:raise HTTPException(409,'WORKFLOW_STOP_REFUSED') from None

    @app.post('/workflow/status')
    def status(body:StepRequest,identity=Depends(service.auth)):
        bound(body.step_id,identity[0])
        from personal_agent_dal.storage.timeline_models import DevelopmentExecution
        with driver.r.sessions() as session:
            step=session.get(DevelopmentDriverStep,body.step_id)
            execution=session.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==body.step_id))
            try:driver._current(session,step);stopped=False
            except ValueError:stopped=True
            if execution.lease_until<=driver.r.now():stopped=True
            return dict(step_id=step.step_id,attempt_id=execution.execution_id,status=step.status,
                result_digest=step.result_digest,binding_digest=execution.binding_digest,stop_required=stopped)

    @app.post('/workflow/prelaunch')
    def prelaunch(body:LaunchRequest,identity=Depends(service.auth)):
        bound(body.step_id,identity[0])
        try:return driver.dispatch(body.step_id,admission=body.assertion)
        except ValueError:raise HTTPException(409,'WORKFLOW_EXECUTION_REFUSED') from None

    @app.post('/workflow/result')
    def result(body:ResultRequest,identity=Depends(service.auth)):
        bound(body.step_id,identity[0])
        try:return driver.accept(body.step_id,attempt_id=body.attempt_id,result=body.result,receipt=body.assertion)
        except ValueError:raise HTTPException(409,'WORKFLOW_RESULT_REFUSED') from None
