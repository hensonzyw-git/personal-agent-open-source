"""Durable workflow claims. Provider text never acts as transition authority.

A prepared step can be claimed once by an admitted worker. A dispatch marker
is irreversible: an interrupted worker must reconcile the same attempt, never
create a replacement merely because time elapsed.
"""
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.machine.execution_results import _SECRET
from personal_agent_dal.storage.timeline_models import (
    DevelopmentRequest as Request, DevelopmentRequestRevision as Revision,
    DevelopmentWorkflow as Workflow, DevelopmentDriverStep as Step,
    DevelopmentGate as Gate, DevelopmentArtifact as Artifact,
    DevelopmentProjectBinding as ProjectBinding, DevelopmentProjectAuthorization as Grant,
)
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.timeline.artifacts import ArtifactService
from personal_agent_dal.timeline.decisions import DecisionService

# These are business actions, resolved through the three configured roles.
PHASES = {
    'clarify': ('planner', 'clarification'),
    'project_routing': ('planner', 'project_route'),
    'researching': ('planner', 'research'),
    'prd_authoring': ('planner', 'prd'),
    'design_authoring': ('planner', 'design'),
    'design_review': ('reviewer', 'review'),
    'delivery_revision_planning': ('planner', 'revision_plan'),
}
WAITING = {'project_selection', 'prd_waiting', 'delivery_waiting', 'accepted'}


def validate_result(phase, result):
    # Scan before semantic checks, including malformed payloads.
    try:
        raw = canonical_json(result)
    except (ValueError, TypeError):
        raise ValueError('RESULT_INVALID') from None
    if _SECRET.search(raw):
        raise ValueError('RESULT_SECRET')
    if not isinstance(result, dict) or len(raw.encode()) > 2*1024*1024:
        raise ValueError('RESULT_INVALID')
    expected = PHASES.get(phase, (None, None))[1]
    fields = {'kind', 'text'}
    extras = {
        'clarification': {'ready', 'questions', 'acceptance'},
        'project_route': {'candidates'},
        'research': {'sources', 'unknowns', 'workspace_receipt_digest'},
        'design': {'prd_digest', 'plan'},
        'review': {'reviewed_artifact_id', 'reviewed_digest', 'verdict', 'findings'},
        'revision_plan': {'stages', 'scope_changed'},
    }
    if (result.get('kind') != expected or set(result) != fields | extras.get(expected, set())
        or not isinstance(result.get('text'), str) or not result['text'].strip()):
        raise ValueError('RESULT_INVALID')
    if expected == 'clarification':
        if (type(result['ready']) is not bool or not isinstance(result['questions'], list)
            or not isinstance(result['acceptance'], list)
            or any(not isinstance(x,str) or not x.strip() for x in result['questions']+result['acceptance'])
            or (result['ready'] and (result['questions'] or not result['acceptance']))
            or (not result['ready'] and not result['questions'])):
            raise ValueError('RESULT_INVALID')
    if expected == 'project_route':
        candidates=result['candidates']
        if not isinstance(candidates,list) or not 1<=len(candidates)<=10:
            raise ValueError('RESULT_INVALID')
        keys=set()
        for c in candidates:
            if (not isinstance(c,dict) or set(c)!={'candidate_key','project_id','grant_id','display_name','kind'}
                or any(not isinstance(v,str) or not v for v in c.values())
                or c['kind'] not in ('existing','local_new') or c['candidate_key'] in keys):
                raise ValueError('RESULT_INVALID')
            keys.add(c['candidate_key'])
    if expected == 'review':
        if (result['verdict'] not in ('PASS','NEEDS_REVISION') or not isinstance(result['findings'],list)
            or any(not isinstance(x,str) or not x.strip() for x in result['findings'])
            or (result['verdict']=='PASS') != (not result['findings'])):
            raise ValueError('RESULT_INVALID')
    return result


class WorkflowDriver:
    def __init__(self, requests, *, roles, kill_switch=lambda: False):
        self.r, self.roles, self.kill_switch = requests, roles, kill_switch

    def _write(self, fn):
        with self.r.sessions() as s:
            return run_write_transaction(s, lambda: fn(s), attempts=8)

    def _event(self, s, wf, kind, text, **extra):
        self.r._append_event(s,s.get(Request,wf.request_id),kind,
            dict(summary=text,phase=wf.phase,status=wf.status,workflow_version=wf.version,**extra))

    def _block(self, s, wf, reason):
        if wf.status != 'blocked':
            wf.status='blocked';wf.version+=1
            self._event(s,wf,'workflow.blocked','开发暂时无法继续：'+reason,reason=reason)
        return dict(workflow_id=wf.workflow_id,status=wf.status,phase=wf.phase,reason=reason)

    def tick(self, workflow_id):
        # Resolving immutable configuration does not itself grant launch rights.
        snapshot=None
        if self.roles is not None:
            try:snapshot=self.roles.snapshot(workflow_id=workflow_id)
            except ValueError:pass
        def work(s):
            wf=s.scalar(select(Workflow).where(Workflow.workflow_id==workflow_id))
            if wf is None:raise ValueError('WORKFLOW_NOT_FOUND')
            if wf.status!='active' or wf.phase in WAITING:
                return dict(workflow_id=workflow_id,status=wf.status,phase=wf.phase)
            if self.kill_switch():return self._block(s,wf,'KILL_SWITCH_ACTIVE')
            gate=s.get(Gate,workflow_id)
            if gate is None:
                gate=Gate(workflow_id=workflow_id,mode='open',epoch=1,version=1);s.add(gate);s.flush()
            if gate.mode!='open':return self._block(s,wf,'EXECUTION_FENCED')
            existing=s.scalar(select(Step).where(Step.workflow_id==workflow_id,Step.expected_version==wf.version,
                Step.status.in_(('prepared','dispatch_started','result_unknown'))))
            if existing:return dict(workflow_id=workflow_id,status=existing.status,step_id=existing.step_id)
            if wf.phase not in PHASES:return self._block(s,wf,'EXECUTOR_REQUIRED')
            if snapshot is None:return self._block(s,wf,'ROLE_UNAVAILABLE')
            role=PHASES[wf.phase][0]
            revision=s.scalar(select(Revision).where(Revision.request_id==wf.request_id).order_by(Revision.revision.desc()))
            request=self.r._open(Revision,revision.revision_id,'sealed_body',revision.sealed_body)
            inputs=dict(schema_version='dal.workflow-input/1.0',owner={'kind':'workflow','workflow_id':workflow_id},
                phase=wf.phase,role=role,request_revision=revision.revision,request=request,
                workflow_version=wf.version,gate_epoch=gate.epoch,snapshot_digest=snapshot['snapshot_digest'],artifacts=[])
            for artifact in s.scalars(select(Artifact).where(Artifact.workflow_id==workflow_id).order_by(Artifact.kind,Artifact.revision)):
                inputs['artifacts'].append(dict(artifact_id=artifact.artifact_id,kind=artifact.kind,
                    revision=artifact.revision,digest=artifact.body_sha256,source_receipt_digest=artifact.source_receipt_digest))
            project=s.get(ProjectBinding,workflow_id)
            if project:
                grant=s.get(Grant,project.grant_id)
                if grant is None or grant.revoked or grant.version!=project.grant_version or grant.expires_at<=self.r.now():
                    return self._block(s,wf,'PROJECT_AUTHORIZATION_REQUIRED')
                inputs['project']=dict(project_id=project.project_id,grant_id=grant.grant_id,grant_version=grant.version,grant_digest=grant.digest)
            elif wf.phase not in ('clarify','project_routing'):
                return self._block(s,wf,'PROJECT_AUTHORIZATION_REQUIRED')
            id=new_id()
            cycle=1+len(list(s.scalars(select(Step.step_id).where(Step.workflow_id==workflow_id,Step.phase==wf.phase))))
            if wf.phase in ('design_authoring','design_review') and cycle>3:
                return self._block(s,wf,'REVIEW_BUDGET_EXHAUSTED')
            s.add(Step(step_id=id,workflow_id=workflow_id,phase=wf.phase,input_digest=digest(inputs),
                sealed_input=self.r._seal(Step,id,'sealed_input',inputs),snapshot_id=snapshot['snapshot_id'],
                expected_version=wf.version,gate_epoch=gate.epoch,cycle=cycle,status='prepared'))
            self._event(s,wf,'workflow.prepared','已准备开发步骤，等待获准的执行环境。',step_id=id)
            return dict(workflow_id=workflow_id,status='prepared',step_id=id)
        return self._write(work)

    def dispatch(self, step_id, *, admission):
        """Only a verified runtime admission is acceptable; no model authority."""
        def work(s):
            step=s.get(Step,step_id)
            if step is None or step.status!='prepared':raise ValueError('STEP_NOT_DISPATCHABLE')
            wf,gate=self._current(s,step)
            # Production adapter must verify signatures, pins and current worker
            # authorization before issuing this exact immutable admission.
            if (not isinstance(admission,dict) or set(admission)!={'step_id','input_digest','snapshot_id','gate_epoch','worker_id','receipt_digest'}
                or admission['step_id']!=step_id or admission['input_digest']!=step.input_digest
                or admission['snapshot_id']!=step.snapshot_id or admission['gate_epoch']!=gate.epoch):
                raise ValueError('RUNTIME_ADMISSION_REQUIRED')
            # This API is internal, not exposed as a model or operator endpoint.
            step.attempt_id=new_id();step.status='dispatch_started'
            return dict(step_id=step_id,attempt_id=step.attempt_id,input=self.r._open(Step,step_id,'sealed_input',step.sealed_input))
        return self._write(work)

    def _current(self,s,step):
        wf=s.scalar(select(Workflow).where(Workflow.workflow_id==step.workflow_id))
        gate=s.scalar(select(Gate).where(Gate.workflow_id==step.workflow_id))
        if (wf.status!='active' or wf.phase!=step.phase or wf.version!=step.expected_version
            or gate is None or gate.mode!='open' or gate.epoch!=step.gate_epoch):
            raise ValueError('STALE_BINDING')
        return wf,gate

    def accept(self,step_id,*,attempt_id,result):
        def work(s):
            step=s.get(Step,step_id)
            if step is None or step.attempt_id!=attempt_id:raise ValueError('RESULT_SOURCE_INVALID')
            result_body=validate_result(step.phase,result)
            sha=digest(result_body)
            if step.status=='completed':
                if step.result_digest!=sha:raise ValueError('RESULT_CONFLICT')
                return dict(step_id=step_id,status='completed',result_digest=sha)
            if step.status not in ('dispatch_started','result_unknown'):raise ValueError('RESULT_SOURCE_INVALID')
            wf,gate=self._current(s,step)
            inputs=self.r._open(Step,step_id,'sealed_input',step.sealed_input)
            if digest(inputs)!=step.input_digest:raise ValueError('INPUT_INTEGRITY_FAILED')
            if step.phase=='design_review':
                designs=[a for a in inputs['artifacts'] if a['kind']=='design']
                if not designs or (result_body['reviewed_artifact_id'],result_body['reviewed_digest'])!=(designs[-1]['artifact_id'],designs[-1]['digest']):
                    raise ValueError('REVIEW_BINDING_INVALID')
            if step.phase=='design_authoring':
                prds=[a for a in inputs['artifacts'] if a['kind']=='prd']
                if not prds or result_body['prd_digest']!=prds[-1]['digest']:raise ValueError('PRD_BINDING_INVALID')
            step.status='completed';step.result_digest=sha
            step.sealed_result=self.r._seal(Step,step_id,'sealed_result',result_body)
            s.flush()
            if step.phase=='clarify':
                if result_body['ready']:wf.phase='project_routing';wf.version+=1
                else:
                    wf.status='blocked';wf.version+=1
                    self._event(s,wf,'workflow.clarification',result_body['text'],questions=result_body['questions'])
            else:
                id=ArtifactService(self.r).record(step_id=step_id,expected_result_digest=sha,_session=s);s.flush()
                if step.phase=='project_routing':DecisionService(self.r).propose(wf.workflow_id,id,kind='project_selection',candidates=result_body['candidates'],_session=s)
                elif step.phase=='prd_authoring':DecisionService(self.r).propose(wf.workflow_id,id,kind='prd',_session=s)
                else:
                    next_phase={'researching':'prd_authoring','design_authoring':'design_review',
                        'design_review':'stage_planning' if result_body.get('verdict')=='PASS' else 'design_authoring',
                        'delivery_revision_planning':'revision_waiting'}[step.phase]
                    wf.phase=next_phase;wf.version+=1
                    self._event(s,wf,'workflow.advanced','开发步骤已完成，结果已保存。',artifact={'artifact_id':id})
            return dict(step_id=step_id,status='completed',result_digest=sha)
        return self._write(work)

    def reconcile_required(self, step_id):
        def work(s):
            step=s.get(Step,step_id)
            if step is None:raise ValueError('STEP_NOT_FOUND')
            if step.status=='dispatch_started':
                step.status='result_unknown'
                wf=s.get(Workflow,step.workflow_id)
                self._event(s,wf,'workflow.reconciliation','执行结果尚未确认，正在等待对账；不会重复执行。')
        self._write(work)
