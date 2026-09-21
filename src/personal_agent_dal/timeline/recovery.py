"""Explicit version-bound recovery; unknown executions are never replayed."""
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.storage.timeline_models import (
    DevelopmentWorkflow as Workflow, DevelopmentDriverStep as Step, DevelopmentGate as Gate,
    DevelopmentRequest as Request, DevelopmentRequestRevision as Revision,
    DevelopmentCommand as Command, DevelopmentDecisionRequest as Decision,
    DevelopmentArtifact as Artifact,
)
from personal_agent_dal.timeline.requests import digest,valid_id
import hashlib

ACTIONS=frozenset({'clarification','resume','pause','cancel','refresh'})


class RecoveryService:
    def __init__(self,requests):self.r=requests

    def process(self,**command):
        try:return self.apply(**command)
        except ValueError as exc:
            if str(exc) not in ('STALE_BINDING','RECONCILIATION_REQUIRED','PROPOSAL_REFRESH_REQUIRED','INPUT_LIMIT'):raise
            reason=str(exc)
            fingerprint=digest(command)
            def work(session):
                old=session.get(Command,command['command_id'])
                if old:
                    if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                    return self.r._open(Command,old.command_id,'sealed_result',old.sealed_result)
                result=dict(command_id=command['command_id'],workflow_id=command['workflow_id'],status='refused',reason=reason)
                session.add(Command(command_id=command['command_id'],body_sha256=fingerprint,
                    sealed_result=self.r._seal(Command,command['command_id'],'sealed_result',result)))
                return result
            with self.r.sessions() as session:return run_write_transaction(session,lambda:work(session))

    def apply(self,*,command_id,source_message_ref,subject,workflow_id,expected_version,action,text):
        for value in (command_id,source_message_ref,subject,workflow_id):valid_id(value)
        if (action not in ACTIONS or type(expected_version) is not int or expected_version<1
            or not isinstance(text,str) or not text.strip() or len(text.encode())>32768):raise ValueError('INVALID_ARGUMENT')
        fingerprint=digest(dict(command_id=command_id,source_message_ref=source_message_ref,subject=subject,
            workflow_id=workflow_id,expected_version=expected_version,action=action,text=text))
        def work(s):
            old=s.get(Command,command_id)
            if old:
                if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return self.r._open(Command,command_id,'sealed_result',old.sealed_result)
            wf=s.get(Workflow,workflow_id)
            if wf is None or wf.version!=expected_version or wf.status in ('completed','cancelled'):
                raise ValueError('STALE_BINDING')
            request=s.get(Request,wf.request_id)
            gate=s.get(Gate,workflow_id)
            if gate is None:
                gate=Gate(workflow_id=workflow_id,mode='open',epoch=1,version=1);s.add(gate)
            unknown=s.scalar(select(Step.step_id).where(Step.workflow_id==workflow_id,
                Step.status.in_(('dispatch_started','result_unknown'))).limit(1))
            from personal_agent_dal.storage.timeline_models import DevelopmentRemoteEffect
            remote_unknown=s.scalar(select(DevelopmentRemoteEffect.step_id).join(Step).where(Step.workflow_id==workflow_id,
                DevelopmentRemoteEffect.status.in_(('started','unknown'))).limit(1))
            if remote_unknown is not None and action not in ('pause','cancel'):raise ValueError('RECONCILIATION_REQUIRED')
            if unknown is not None and action not in ('pause','cancel'):raise ValueError('RECONCILIATION_REQUIRED')
            decisions=list(s.scalars(select(Decision).where(Decision.workflow_id==workflow_id,Decision.status=='pending')))
            if action in ('pause','cancel'):
                # Freeze immediately, but do not claim termination of an unknown
                # external process. A cancellation with unknown remains paused.
                wf.status='paused' if unknown is not None or action=='pause' else 'cancelled'
                gate.mode='paused' if wf.status=='paused' else 'cancelled'
                gate.version+=1;gate.epoch+=1
            elif action=='clarification':
                if wf.status not in ('active','blocked') or wf.phase!='clarify':raise ValueError('STALE_BINDING')
                previous=s.scalar(select(Revision).where(Revision.request_id==request.request_id,
                    Revision.revision==request.version))
                body=self.r._open(Revision,previous.revision_id,'sealed_body',previous.sealed_body)
                combined=body['text']+'\n\n用户补充：\n'+text
                if len(combined.encode())>32768:raise ValueError('INPUT_LIMIT')
                request.version+=1
                revision_id=new_id()
                s.add(Revision(revision_id=revision_id,request_id=request.request_id,revision=request.version,
                    body_sha256=hashlib.sha256(combined.encode()).hexdigest(),
                    sealed_body=self.r._seal(Revision,revision_id,'sealed_body',{'text':combined})))
                wf.status='active';wf.blocker_reason=None;gate.mode='open';gate.version+=1;gate.epoch+=1
            elif action=='resume':
                if wf.status not in ('paused','blocked'):raise ValueError('STALE_BINDING')
                if wf.phase in ('prd_waiting','project_selection'):
                    raise ValueError('PROPOSAL_REFRESH_REQUIRED')
                wf.status='active';gate.mode='open';gate.version+=1;gate.epoch+=1
                if wf.phase=='delivery_waiting':wf.phase='delivery_prepare';gate.mode='paused'
                from personal_agent_dal.storage.timeline_models import DevelopmentStagePlan
                from personal_agent_dal.timeline.stages import plan_stages
                plan=s.scalar(select(DevelopmentStagePlan).where(DevelopmentStagePlan.workflow_id==workflow_id).order_by(DevelopmentStagePlan.revision.desc()))
                if plan is not None and any(row.state=='blocked' for row in plan_stages(s,plan.plan_id)):
                    # A fresh human request permits planning, never a budget reset
                    # or direct resumption of the exhausted source writer.
                    from personal_agent_dal.storage.timeline_models import DevelopmentStageWriter
                    writer=s.get(DevelopmentStageWriter,workflow_id)
                    if writer is not None:s.delete(writer)  # Also repairs pre-fix persisted rows.
                    wf.phase='delivery_revision_planning';gate.mode='paused'
                wf.blocker_reason=None
            elif action=='refresh':
                if wf.phase not in ('prd_waiting','project_selection','delivery_waiting'):raise ValueError('PROPOSAL_REFRESH_REQUIRED')
                kind={'prd_waiting':'prd','project_selection':'project_selection','delivery_waiting':'delivery'}[wf.phase]
                artifact=s.scalar(select(Artifact).where(Artifact.workflow_id==workflow_id,
                    Artifact.kind=={'prd':'prd','project_selection':'project_route','delivery':'delivery'}[kind]).order_by(Artifact.revision.desc()))
                if artifact is None:raise ValueError('PROPOSAL_REFRESH_REQUIRED')
                wf.status='active';gate.mode='open';gate.version+=1;gate.epoch+=1
                if kind=='delivery':wf.phase='delivery_prepare';gate.mode='paused'
            invalidated=[]
            for decision in decisions:
                decision.status='superseded';invalidated.append(decision.decision_id)
            for step in s.scalars(select(Step).where(Step.workflow_id==workflow_id,Step.status=='prepared')):
                step.status='retired'
                from personal_agent_dal.storage.timeline_models import DevelopmentExecution
                execution=s.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step.step_id))
                if execution and execution.started_at is None:execution.charged_seconds=0
            from personal_agent_dal.storage.timeline_models import DevelopmentStageWriter
            writer=s.get(DevelopmentStageWriter,workflow_id)
            if writer is not None and action=='resume':writer.epoch=gate.epoch
            wf.version+=1
            self.r._append_event(s,request,'workflow.recovered',dict(summary='开发任务状态已更新。',
                workflow_version=wf.version,status=wf.status,phase=wf.phase,
                invalidated_decision_ids=invalidated,reconciliation_required=unknown is not None))
            if action=='refresh' and kind!='delivery':
                from personal_agent_dal.timeline.decisions import DecisionService
                candidates=None
                if kind=='project_selection':
                    candidates=self.r._open(Artifact,artifact.artifact_id,'sealed_body',artifact.sealed_body)['candidates']
                DecisionService(self.r).propose(workflow_id,artifact.artifact_id,kind=kind,candidates=candidates,_session=s)
            result=dict(command_id=command_id,workflow_id=workflow_id,workflow_version=wf.version,
                status='accepted',workflow_status=wf.status,phase=wf.phase,reconciliation_required=unknown is not None)
            s.add(Command(command_id=command_id,body_sha256=fingerprint,
                sealed_result=self.r._seal(Command,command_id,'sealed_result',result)))
            return result
        with self.r.sessions() as s:return run_write_transaction(s,lambda:work(s))
