"""Deterministic whole-utterance admission, shared by PA and DAL."""
import re


def parse_decision(text):
    if not isinstance(text,str) or len(text.encode())>32768:return None
    text=text.strip()
    if re.fullmatch(r'(?:通过|批准|同意(?:这个\s*(?:PRD|交付))?)[。！!]?',text,re.I):
        return dict(decision='approve',feedback='')
    if re.fullmatch(r'(?:拒绝|不通过|暂停)[。！!]?',text):return dict(decision='reject',feedback='')
    match=re.fullmatch(r'(?:修改|需要修改|修改意见)[：:]\s*(\S[\s\S]*)',text)
    if match:return dict(decision='request_changes',feedback=match[1])
    return None


def parse_project_choice(text,candidates):
    if not isinstance(text,str) or not isinstance(candidates,list):return None
    text=text.strip()
    ordinal=re.fullmatch(r'(?:选|选择)第\s*([1-9][0-9]*|[一二三四五六七八九十])\s*个(?:项目)?[。！!]?',text)
    if ordinal:
        n=int(ordinal[1]) if ordinal[1].isascii() else '一二三四五六七八九十'.index(ordinal[1])+1
        return candidates[n-1]['candidate_key'] if 1<=n<=len(candidates) else None
    named=re.fullmatch(r'(?:选择|选)\s+(.+?)[。！!]?',text)
    if named:
        found=[c['candidate_key'] for c in candidates if c['display_name']==named[1]]
        if len(found)==1:return found[0]
    return None


from datetime import timedelta
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.timeline.requests import digest,valid_id
from personal_agent_dal.storage.timeline_models import (DevelopmentWorkflow as Workflow,DevelopmentRequest as Request,
    DevelopmentArtifact as Artifact,DevelopmentDecisionRequest as Decision,DevelopmentDecisionReceipt as Receipt,
    DevelopmentGate as Gate,DevelopmentProjectAuthorization as Grant,DevelopmentProjectBinding as ProjectBinding)


class DecisionService:
    def __init__(self,requests):self.r=requests

    def propose(self,workflow_id,artifact_id,*,kind,candidates=None,_session=None):
        if kind not in ('prd','project_selection'):raise ValueError('DECISION_KIND_INVALID')
        def work(s):
            wf=s.scalar(select(Workflow).where(Workflow.workflow_id==workflow_id))
            artifact=s.get(Artifact,artifact_id)
            expected_kind='prd' if kind=='prd' else 'project_route'
            if wf is None or wf.status!='active' or artifact is None or artifact.workflow_id!=workflow_id or artifact.kind!=expected_kind:raise ValueError('STALE_BINDING')
            newest=s.scalar(select(Artifact).where(Artifact.workflow_id==workflow_id,Artifact.kind==expected_kind).order_by(Artifact.revision.desc()))
            if newest.artifact_id!=artifact_id:raise ValueError('STALE_BINDING')
            if kind=='project_selection':
                body=self.r._open(Artifact,artifact_id,'sealed_body',artifact.sealed_body)
                if candidates!=body.get('candidates') or not isinstance(candidates,list) or not 1<=len(candidates)<=10:raise ValueError('CANDIDATES_INVALID')
                if any(set(c)!={'candidate_key','project_id','grant_id','display_name','kind'} for c in candidates):raise ValueError('CANDIDATES_INVALID')
                if len({c['candidate_key'] for c in candidates})!=len(candidates):raise ValueError('CANDIDATES_INVALID')
            current=list(s.scalars(select(Decision).where(Decision.workflow_id==workflow_id,Decision.status=='pending')))
            for old in current:
                binding=self.r._open(Decision,old.decision_id,'sealed_binding',old.sealed_binding)
                if binding['artifact_id']==artifact_id and old.kind==kind and old.expires_at>self.r.now():return self._projection(old,binding)
                old.status='superseded'
            gate=s.get(Gate,workflow_id)
            if gate is None:
                gate=Gate(workflow_id=workflow_id,mode='open',epoch=1,version=1);s.add(gate)
            if gate.mode!='open':raise ValueError('EXECUTION_FENCED')
            wf.phase='prd_waiting' if kind=='prd' else 'project_selection';wf.version+=1
            id=new_id();expires=self.r.now()+timedelta(hours=24)
            binding=dict(workflow_id=workflow_id,workflow_version=wf.version,decision_id=id,kind=kind,decision_version=1,
                artifact_id=artifact_id,artifact_revision=artifact.revision,body_sha256=artifact.body_sha256,
                gate_version=gate.version,gate_epoch=gate.epoch,expires_at=expires.isoformat())
            if candidates is not None:binding.update(candidates=candidates,candidate_set_digest=digest(candidates))
            row=Decision(decision_id=id,workflow_id=workflow_id,kind=kind,version=1,binding_digest=digest(binding),
                sealed_binding=self.r._seal(Decision,id,'sealed_binding',binding),status='pending',expires_at=expires)
            s.add(row)
            text='PRD 已准备好，请打开文档审核；可以直接回复“通过”或“修改：意见”。' if kind=='prd' else '请选择项目：\n'+'\n'.join(f'{i+1}. {c["display_name"]}' for i,c in enumerate(candidates))
            request=s.get(Request,wf.request_id)
            self.r._append_event(s,request,'decision.requested',dict(summary=text,status=wf.status,phase=wf.phase,
                artifact=dict(artifact_id=artifact_id,kind=artifact.kind,revision=artifact.revision),decision=self._projection(row,binding)))
            return self._projection(row,binding)
        if _session is not None:return work(_session)
        with self.r.sessions() as s:return run_write_transaction(s,lambda:work(s))

    @staticmethod
    def _projection(row,binding):return dict(decision_id=row.decision_id,kind=row.kind,version=row.version,binding_digest=row.binding_digest,binding=binding,expires_at=row.expires_at.isoformat())

    def consume(self,*,command_id,source_message_ref,subject,decision_id,binding_digest,text,expected_kind=None,_session=None):
        for value in (command_id,source_message_ref,subject,decision_id):valid_id(value)
        if not isinstance(text,str) or not text.strip() or len(text.encode())>32768:raise ValueError('INVALID_ARGUMENT')
        fingerprint=digest(dict(command_id=command_id,source_message_ref=source_message_ref,subject=subject,decision_id=decision_id,binding_digest=binding_digest,text=text))
        def work(s):
            old=s.scalar(select(Receipt).where(Receipt.command_id==command_id))
            if old:
                if old.body_digest!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return self.r._open(Receipt,command_id,'sealed_result',old.sealed_result)
            if s.scalar(select(Receipt).where(Receipt.source_message_ref==source_message_ref)):raise ValueError('MESSAGE_ALREADY_CONSUMED')
            row=s.scalar(select(Decision).where(Decision.decision_id==decision_id))
            if row is None or row.status!='pending' or row.expires_at<=self.r.now() or row.binding_digest!=binding_digest:raise ValueError('STALE_BINDING')
            if expected_kind is not None and row.kind!=expected_kind:raise ValueError('SCOPE_REQUIRED')
            binding=self.r._open(Decision,decision_id,'sealed_binding',row.sealed_binding)
            if digest(binding)!=binding_digest:raise ValueError('STALE_BINDING')
            wf=s.get(Workflow,row.workflow_id);gate=s.get(Gate,row.workflow_id)
            if wf.status!='active' or wf.version!=binding['workflow_version'] or gate.version!=binding['gate_version'] or gate.epoch!=binding['gate_epoch'] or gate.mode!='open':raise ValueError('STALE_BINDING')
            choice=None;feedback=''
            if row.kind=='project_selection':
                choice=parse_project_choice(text,binding['candidates'])
                if choice is None:raise ValueError('AMBIGUOUS_TARGET')
                selected=next(c for c in binding['candidates'] if c['candidate_key']==choice)
                grant=s.get(Grant,selected['grant_id'])
                if grant is None or grant.revoked or grant.expires_at<=self.r.now() or grant.project_id!=selected['project_id'] or grant.subject!=subject:raise ValueError('INPUT_NOT_AUTHORIZED')
                data=self.r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
                if digest(data)!=grant.digest or 'read' not in data['actions']:raise ValueError('INPUT_NOT_AUTHORIZED')
                s.add(ProjectBinding(workflow_id=wf.workflow_id,project_id=grant.project_id,grant_id=grant.grant_id,grant_version=grant.version,
                    route_artifact_id=binding['artifact_id'],candidate_digest=binding['candidate_set_digest'],
                    sealed_binding=self.r._seal(ProjectBinding,wf.workflow_id,'sealed_binding',dict(candidate=selected,grant_digest=grant.digest))))
                wf.phase='project_registration';decision='select_project'
            elif row.kind=='prd':
                parsed=parse_decision(text)
                if parsed is None:raise ValueError('AMBIGUOUS_TARGET')
                decision,feedback=parsed['decision'],parsed['feedback']
                if decision=='approve':wf.phase='design_authoring'
                elif decision=='request_changes':wf.phase='prd_authoring'
                else:
                    wf.status='paused';gate.mode='paused';gate.version+=1;gate.epoch+=1
            else:raise ValueError('DELIVERY_PROBE_REQUIRED')
            row.status='consumed';wf.version+=1
            result=dict(command_id=command_id,decision_id=decision_id,workflow_id=wf.workflow_id,workflow_version=wf.version,
                status='accepted',decision=decision,phase=wf.phase,workflow_status=wf.status)
            # Full original text and feedback remain encrypted in the receipt.
            s.add(Receipt(command_id=command_id,source_message_ref=source_message_ref,decision_id=decision_id,body_digest=fingerprint,
                sealed_result=self.r._seal(Receipt,command_id,'sealed_result',dict(**result,source_text=text,feedback=feedback))))
            self.r._append_event(s,s.get(Request,wf.request_id),'decision.accepted',dict(summary='决定已接纳。',phase=wf.phase,status=wf.status,decision_id=decision_id))
            return dict(**result,source_text=text,feedback=feedback)
        if _session is not None:return work(_session)
        with self.r.sessions() as s:return run_write_transaction(s,lambda:work(s))

    def process(self, **command):
        """Signed commands get a durable accepted/refused receipt, including stale replies."""
        from personal_agent_dal.storage.timeline_models import DevelopmentCommand
        fingerprint=digest(dict(kind='decision',**command))
        def work(s):
            old=s.get(DevelopmentCommand,command['command_id'])
            if old:
                if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return self.r._open(DevelopmentCommand,old.command_id,'sealed_result',old.sealed_result)
            try:
                with s.begin_nested():
                    result=self.consume(**command,_session=s)
            except ValueError as exc:
                allowed={'STALE_BINDING','SCOPE_REQUIRED','AMBIGUOUS_TARGET','INPUT_NOT_AUTHORIZED',
                    'MESSAGE_ALREADY_CONSUMED','DELIVERY_PROBE_REQUIRED'}
                if str(exc) not in allowed:raise
                result=dict(command_id=command['command_id'],decision_id=command['decision_id'],status='refused',reason=str(exc))
            id=command['command_id']
            s.add(DevelopmentCommand(command_id=id,body_sha256=fingerprint,
                sealed_result=self.r._seal(DevelopmentCommand,id,'sealed_result',result)))
            return result
        with self.r.sessions() as s:return run_write_transaction(s,lambda:work(s))
