"""Durable workflow claims. Provider text never acts as transition authority.

A prepared step can be claimed once by an admitted worker. A dispatch marker
is irreversible: an interrupted worker must reconcile the same attempt, never
create a replacement merely because time elapsed.
"""
import re
import hashlib
import json
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.machine.execution_results import _SECRET
from personal_agent_dal.storage.timeline_models import (
    DevelopmentRequest as Request, DevelopmentRequestRevision as Revision,
    DevelopmentWorkflow as Workflow, DevelopmentDriverStep as Step,
    DevelopmentGate as Gate, DevelopmentArtifact as Artifact,
    DevelopmentDecisionReceipt as DecisionReceipt, DevelopmentDecisionRequest as Decision,
    DevelopmentRoleSnapshot as RoleSnapshot,
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
    'delivery_revision_review': ('reviewer', 'review'),
    'delivery_publication': ('planner','delivery'),
    'delivery_prepare': ('planner','delivery'),
    'delivery_probe': ('planner','delivery_probe'),
    'project_registration': ('planner','registration'),
    'workspace_prepare': ('coder','workspace'),
    'coding': ('coder', 'candidate'),
    'fix': ('coder', 'candidate'),
    'verify': ('planner', 'verification'),
    'code_review': ('reviewer', 'code_review'),
    'stage_commit': ('coder', 'commit'),
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
        'delivery': {'manifest'},
        'delivery_probe': {'nonce','manifest_digest','matches','observed_at'},
        'registration': {'project_id','grant_digest','policy','tracker_receipt'},
        'workspace': {'project_id','grant_digest','manifest'},
        'candidate': {'candidate'},
        'verification': {'candidate','passed','commands'},
        'code_review': {'candidate','passed','findings'},
        'commit': {'candidate','committed','commit_sha','parent_sha'},
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
    def sha(value):
        return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None
    if expected=='research':
        if (not isinstance(result['sources'],list) or not result['sources']
            or not isinstance(result['unknowns'],list)
            or any(not isinstance(x,str) or not x.strip() for x in result['unknowns'])
            or not sha(result['workspace_receipt_digest'])):
            raise ValueError('RESULT_INVALID')
        for source in result['sources']:
            if (not isinstance(source,dict) or set(source)!={'ref','digest'}
                or not isinstance(source['ref'],str) or not source['ref'].strip() or not sha(source['digest'])):
                raise ValueError('RESULT_INVALID')
    if expected=='design':
        from personal_agent_dal.timeline.stages import validate_plan
        if not sha(result['prd_digest']):raise ValueError('RESULT_INVALID')
        validate_plan(result['plan'])
    if expected=='review':
        from personal_agent_dal.timeline.requests import valid_id
        valid_id(result['reviewed_artifact_id'])
        if not sha(result['reviewed_digest']):raise ValueError('RESULT_INVALID')
    if expected=='revision_plan':
        if (type(result['scope_changed']) is not bool or not isinstance(result['stages'],list)
            or not result['stages']):raise ValueError('RESULT_INVALID')
        seen=set()
        for stage in result['stages']:
            if (not isinstance(stage,dict) or set(stage)!={'stage_id','revision'}
                or not isinstance(stage['stage_id'],str) or not stage['stage_id']
                or type(stage['revision']) is not int or stage['revision']<1
                or stage['stage_id'] in seen):raise ValueError('RESULT_INVALID')
            seen.add(stage['stage_id'])
    if expected=='delivery':
        manifest=result['manifest']
        common={'kind','stage_manifest','head_sha','tree_sha','commits','clean','untracked_digest'}
        extra={'repository_id','host','owner','repository','pr_number','base_branch','base_sha'} if isinstance(manifest,dict) and manifest.get('kind')=='pr' else set()
        if (not isinstance(manifest,dict) or set(manifest)!=common|extra or manifest.get('kind') not in ('local','pr')
            or manifest['clean'] is not True or manifest['untracked_digest']!=digest([])
            or not isinstance(manifest['commits'],list) or not manifest['commits']):raise ValueError('DELIVERY_INVALID')
        for key in ('head_sha','tree_sha'):
            if not isinstance(manifest[key],str) or not re.fullmatch('[a-f0-9]{40}',manifest[key]):raise ValueError('DELIVERY_INVALID')
        for commit in manifest['commits']:
            if (not isinstance(commit,dict) or set(commit)!={'sha','parent','tree'}
                or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{40}',v) for v in commit.values())):raise ValueError('DELIVERY_INVALID')
    if expected=='delivery_probe':
        from personal_agent_dal.timeline.requests import valid_id
        valid_id(result['nonce'])
        if not sha(result['manifest_digest']) or type(result['matches']) is not bool or type(result['observed_at']) is not int:raise ValueError('DELIVERY_PROBE_INVALID')
    if expected in ('registration','workspace'):
        from personal_agent_dal.timeline.requests import valid_id
        valid_id(result['project_id'])
        if not sha(result['grant_digest']):raise ValueError('RESULT_INVALID')
        if expected=='registration':
            if result['policy'] not in ('local_tracker','github_issue') or not sha(result['tracker_receipt']):raise ValueError('RESULT_INVALID')
        else:
            manifest=result['manifest']
            if not isinstance(manifest,dict) or set(manifest)!={'kind','project_id','reservation_id','generation','directory_digest','base_sha','toolchain_digest','branch'}:
                raise ValueError('RESULT_INVALID')
            for key in ('project_id','reservation_id'):valid_id(manifest[key])
            if (manifest['kind'] not in ('existing','local_new') or type(manifest['generation']) is not int or manifest['generation']<1
                or not sha(manifest['directory_digest']) or not sha(manifest['toolchain_digest'])
                or not isinstance(manifest['base_sha'],str) or not re.fullmatch('[a-f0-9]{40}',manifest['base_sha']) or manifest['base_sha']=='0'*40
                or not isinstance(manifest['branch'],str) or not re.fullmatch('refs/heads/[A-Za-z0-9_.:/-]+',manifest['branch'])):raise ValueError('RESULT_INVALID')
    if expected in ('candidate','verification','code_review','commit'):
        candidate=result['candidate']
        if (not isinstance(candidate,dict) or set(candidate)!={'base_sha','head_sha','tree_sha'}
            or any(not isinstance(v,str) or re.fullmatch('[a-f0-9]{40}',v) is None for v in candidate.values())):
            raise ValueError('RESULT_INVALID')
        if expected in ('verification','code_review') and type(result['passed']) is not bool:raise ValueError('RESULT_INVALID')
        if expected=='verification':
            if not isinstance(result['commands'],list) or not result['commands']:raise ValueError('RESULT_INVALID')
            for command in result['commands']:
                if (not isinstance(command,dict) or set(command)!={'argv_digest','exit_code','output_digest'}
                    or not sha(command['argv_digest']) or not sha(command['output_digest']) or type(command['exit_code']) is not int):raise ValueError('RESULT_INVALID')
            if result['passed']!=all(c['exit_code']==0 for c in result['commands']):raise ValueError('RESULT_INVALID')
        if expected=='code_review':
            if (not isinstance(result['findings'],list) or any(not isinstance(f,str) or not f for f in result['findings'])
                or result['passed']!=(not result['findings'])):raise ValueError('RESULT_INVALID')
        if expected=='commit':
            if (result['committed'] is not True or result['parent_sha']!=candidate['head_sha']
                or not isinstance(result['commit_sha'],str) or re.fullmatch('[a-f0-9]{40}',result['commit_sha']) is None):raise ValueError('RESULT_INVALID')
    return result


class WorkflowDriver:
    def __init__(self, requests, *, roles, kill_switch=lambda: False, authority=None):
        self.r, self.roles, self.kill_switch = requests, roles, kill_switch
        self.authority = authority

    def _write(self, fn):
        with self.r.sessions() as s:
            return run_write_transaction(s, lambda: fn(s), attempts=8)

    def _event(self, s, wf, kind, text, **extra):
        self.r._append_event(s,s.get(Request,wf.request_id),kind,
            dict(summary=text,phase=wf.phase,status=wf.status,workflow_version=wf.version,**extra))

    def _block(self, s, wf, reason):
        if wf.status != 'blocked':
            wf.status='blocked';wf.blocker_reason=reason;wf.version+=1
            self._event(s,wf,'workflow.blocked','开发暂时无法继续：'+reason,reason=reason)
        return dict(workflow_id=wf.workflow_id,status=wf.status,phase=wf.phase,reason=reason)

    def tick(self, workflow_id):
        def work(s):
            wf=s.scalar(select(Workflow).where(Workflow.workflow_id==workflow_id))
            if wf is None:raise ValueError('WORKFLOW_NOT_FOUND')
            from personal_agent_dal.timeline.authorization_requests import recover
            recover(self,s,wf)
            if wf.status=='active' and wf.phase=='delivery_waiting':
                from personal_agent_dal.timeline.delivery import prepare_probe
                probe_step=prepare_probe(self,s,wf)
                if probe_step:return dict(workflow_id=workflow_id,status='prepared',step_id=probe_step)
            if wf.status!='active' or wf.phase in WAITING:
                return dict(workflow_id=workflow_id,status=wf.status,phase=wf.phase)
            if self.kill_switch():return self._block(s,wf,'KILL_SWITCH_ACTIVE')
            gate=s.get(Gate,workflow_id)
            if gate is None:
                gate=Gate(workflow_id=workflow_id,mode='open',epoch=1,version=1);s.add(gate);s.flush()
            if gate.mode!='open' and not (gate.mode=='paused' and wf.phase in ('delivery_prepare','delivery_revision_planning','delivery_revision_review')):return self._block(s,wf,'EXECUTION_FENCED')
            existing=s.scalar(select(Step).where(Step.workflow_id==workflow_id,Step.expected_version==wf.version,
                Step.status.in_(('prepared','dispatch_started','result_unknown'))))
            if existing:return dict(workflow_id=workflow_id,status=existing.status,step_id=existing.step_id)
            stage=None
            if wf.phase=='stage_planning' or wf.phase in ('coding','fix','verify','code_review','stage_commit'):
                from personal_agent_dal.timeline.stage_driver import prepare_stage
                stage=prepare_stage(self,s,wf)
                if wf.status!='active' or wf.phase not in PHASES:
                    return dict(workflow_id=workflow_id,status=wf.status,phase=wf.phase)
            if wf.phase not in PHASES:return self._block(s,wf,'EXECUTOR_REQUIRED')
            delivery=None
            if wf.phase=='delivery_publication':
                from personal_agent_dal.timeline.delivery import manifest_inputs
                delivery=manifest_inputs(self.r,s,wf)
            if wf.phase=='delivery_prepare':
                from personal_agent_dal.timeline.delivery import freeze_dispatch
                delivery=freeze_dispatch(self,s,wf)
            if wf.phase=='project_routing':
                from personal_agent_dal.timeline.authorization_requests import require_catalog
                if not require_catalog(self,s,wf):return dict(workflow_id=workflow_id,status=wf.status,phase=wf.phase,reason=wf.blocker_reason)
            snapshot=None
            if self.roles is not None:
                try:snapshot=self.roles.snapshot(workflow_id=workflow_id,_session=s)
                except ValueError:pass
            if snapshot is None:return self._block(s,wf,'ROLE_UNAVAILABLE')
            role=PHASES[wf.phase][0]
            revision=s.scalar(select(Revision).where(Revision.request_id==wf.request_id).order_by(Revision.revision.desc()))
            request=self.r._open(Revision,revision.revision_id,'sealed_body',revision.sealed_body)
            inputs=dict(schema_version='dal.workflow-input/1.0',owner={'kind':'workflow','workflow_id':workflow_id},
                phase=wf.phase,role=role,prepared_at=int(self.r.now().timestamp()),request_revision=revision.revision,request=request,
                workflow_version=wf.version,gate_epoch=gate.epoch,snapshot_digest=snapshot['snapshot_digest'],artifacts=[])
            for artifact in s.scalars(select(Artifact).where(Artifact.workflow_id==workflow_id).order_by(Artifact.kind,Artifact.revision)):
                body=self.r._open(Artifact,artifact.artifact_id,'sealed_body',artifact.sealed_body)
                if (not isinstance(body,dict) or not isinstance(body.get('text'),str)
                    or hashlib.sha256(body['text'].encode()).hexdigest()!=artifact.body_sha256
                    or digest(body)!=artifact.source_receipt_digest):
                    raise ValueError('INPUT_INTEGRITY_FAILED')
                inputs['artifacts'].append(dict(artifact_id=artifact.artifact_id,kind=artifact.kind,
                    revision=artifact.revision,digest=artifact.body_sha256,source_receipt_digest=artifact.source_receipt_digest,
                    body=body))
            if wf.phase in ('design_review','delivery_revision_review'):
                designs=[a for a in inputs['artifacts'] if a['kind']==('revision_plan' if wf.phase=='delivery_revision_review' else 'design')]
                if not designs:return self._block(s,wf,'REVIEW_SOURCE_REQUIRED')
                design=s.get(Artifact,designs[-1]['artifact_id'])
                source=s.get(Step,design.source_step_id)
                author=s.get(RoleSnapshot,source.snapshot_id) if source and source.snapshot_id else None
                if author is None:return self._block(s,wf,'REVIEW_SOURCE_REQUIRED')
                author_body=json.loads(author.body)
                if digest(author_body)!=author.digest:raise ValueError('INPUT_INTEGRITY_FAILED')
                if author_body['roles']['planner']['model']==snapshot['roles']['reviewer']['model']:
                    return self._block(s,wf,'REVIEW_NOT_INDEPENDENT')
            if wf.phase in ('delivery_revision_planning','delivery_revision_review'):
                from personal_agent_dal.timeline.revisions import context
                proposals=[a for a in inputs['artifacts'] if a['kind']=='revision_plan']
                targets=proposals[-1]['body']['stages'] if wf.phase=='delivery_revision_review' and proposals else None
                inputs['revision_context']=context(self.r,s,wf,targets)
            if wf.phase=='project_routing':
                from personal_agent_dal.timeline.projects import catalog
                inputs['project_catalog']=catalog(self.r,s,wf.request_id)
            if stage is not None:inputs['stage']=stage
            if delivery is not None:inputs['delivery']=delivery
            if wf.phase=='delivery_prepare' and delivery['workspace']['kind']=='existing':
                published=s.scalar(select(Step).where(Step.workflow_id==workflow_id,Step.phase=='delivery_publication',Step.status=='completed').order_by(Step.expected_version.desc()))
                if published is None:return self._block(s,wf,'REMOTE_PUBLICATION_REQUIRED')
                result=self.r._open(Step,published.step_id,'sealed_result',published.sealed_result)
                if digest(result)!=published.result_digest or result['manifest']['stage_manifest']!=delivery:
                    return self._block(s,wf,'REMOTE_PUBLICATION_STALE')
                inputs['published_manifest']=result['manifest']
            inputs['human_feedback']=[]
            for receipt in s.scalars(select(DecisionReceipt).join(Decision).where(
                Decision.workflow_id==workflow_id).order_by(Decision.version,Decision.decision_id)):
                feedback=self.r._open(DecisionReceipt,receipt.command_id,'sealed_result',receipt.sealed_result)
                inputs['human_feedback'].append(dict(decision_id=receipt.decision_id,
                    command_id=receipt.command_id,decision=feedback['decision'],workflow_version=feedback['workflow_version'],
                    source_text=feedback['source_text'],feedback=feedback['feedback']))
            inputs['human_feedback'].sort(key=lambda item:item['workflow_version'])
            project=s.get(ProjectBinding,workflow_id)
            if project:
                grant=s.get(Grant,project.grant_id)
                if grant is None or grant.revoked or grant.version!=project.grant_version or grant.expires_at<=self.r.now():
                    return self._block(s,wf,'PROJECT_AUTHORIZATION_REQUIRED')
                inputs['project']=dict(project_id=project.project_id,grant_id=grant.grant_id,grant_version=grant.version,grant_digest=grant.digest)
                inputs['authorization']=self.r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
                from personal_agent_dal.timeline.phone_authorization import execution_policy
                try:policy=execution_policy(self.r,s,grant)
                except ValueError:return self._block(s,wf,'PROJECT_AUTHORIZATION_REQUIRED')
                if policy is not None:inputs['project_policy']=policy
                from personal_agent_dal.storage.timeline_models import DevelopmentWorkspace
                workspace=s.get(DevelopmentWorkspace,workflow_id)
                if workspace:
                    inputs['workspace']=self.r._open(DevelopmentWorkspace,workflow_id,'sealed_manifest',workspace.sealed_manifest)
                    inputs['workspace_receipt_digest']=workspace.receipt_digest
                elif wf.phase not in ('project_registration','workspace_prepare'):
                    return self._block(s,wf,'WORKSPACE_REQUIRED')
            elif wf.phase not in ('clarify','project_routing'):
                return self._block(s,wf,'PROJECT_AUTHORIZATION_REQUIRED')
            id=new_id()
            cycle=1+len(list(s.scalars(select(Step.step_id).where(Step.workflow_id==workflow_id,Step.phase==wf.phase))))
            if wf.phase in ('design_authoring','design_review','delivery_revision_planning','delivery_revision_review') and cycle>3:
                return self._block(s,wf,'REVIEW_BUDGET_EXHAUSTED')
            s.add(Step(step_id=id,workflow_id=workflow_id,phase=wf.phase,input_digest=digest(inputs),
                sealed_input=self.r._seal(Step,id,'sealed_input',inputs),snapshot_id=snapshot['snapshot_id'],
                expected_version=wf.version,gate_epoch=gate.epoch,cycle=cycle,status='prepared',
                stage_id=stage['stage_id'] if stage else None,stage_revision=stage['revision'] if stage else None))
            self._event(s,wf,'workflow.prepared','已准备开发步骤，等待获准的执行环境。',step_id=id)
            return dict(workflow_id=workflow_id,status='prepared',step_id=id)
        return self._write(work)

    def reserve(self,step_id,*,worker_id):
        def work(s):
            step=s.get(Step,step_id)
            if step is None or step.status not in ('prepared','dispatch_started','result_unknown'):
                raise ValueError('STEP_NOT_DISPATCHABLE')
            self._current(s,step)
            if self.authority is None:raise ValueError('RUNTIME_ADMISSION_REQUIRED')
            from personal_agent_dal.storage.timeline_models import DevelopmentExecution
            prior=s.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step_id))
            if prior and prior.worker_id==worker_id and step.status=='prepared':
                _,current=self.authority.admission(worker_id,s.get(RoleSnapshot,step.snapshot_id))
                if prior.lease_until<=self.r.now() or prior.admission_digest!=digest(current):
                    step.status='retired';prior.charged_seconds=0
                    wf=s.get(Workflow,step.workflow_id);wf.version+=1
                    self._event(s,wf,'workflow.authority_refreshed','未启动的旧权限已失效，将重新准备当前步骤。')
                    return {'binding':None}
            binding=self.authority.reserve(s,step,worker_id)
            snapshot=s.get(RoleSnapshot,step.snapshot_id)
            return dict(binding=binding,snapshot=json.loads(snapshot.body),
                input=self.r._open(Step,step_id,'sealed_input',step.sealed_input),status=step.status)
        return self._write(work)

    def dispatch(self, step_id, *, admission):
        """Only a verified runtime admission is acceptable; no model authority."""
        def work(s):
            step=s.get(Step,step_id)
            if step is None or step.status!='prepared':raise ValueError('STEP_NOT_DISPATCHABLE')
            wf,gate=self._current(s,step)
            if self.authority is None:raise ValueError('RUNTIME_ADMISSION_REQUIRED')
            execution=self.authority.verify(s,step,admission,
                domain='dal.workflow-prelaunch/1.0',payload={'input_digest':step.input_digest})
            execution.started_at=self.r.now()
            step.attempt_id=execution.execution_id;step.status='dispatch_started'
            return dict(step_id=step_id,attempt_id=step.attempt_id,input=self.r._open(Step,step_id,'sealed_input',step.sealed_input))
        return self._write(work)

    def _current(self,s,step):
        wf=s.scalar(select(Workflow).where(Workflow.workflow_id==step.workflow_id))
        gate=s.scalar(select(Gate).where(Gate.workflow_id==step.workflow_id))
        if (wf.status!='active' or (wf.phase!=step.phase and not (wf.phase=='delivery_waiting' and step.phase=='delivery_probe')) or wf.version!=step.expected_version
            or gate is None or (gate.mode!='open' and not (gate.mode=='paused' and step.phase in ('delivery_prepare','delivery_probe','delivery_revision_planning','delivery_revision_review'))) or gate.epoch!=step.gate_epoch):
            raise ValueError('STALE_BINDING')
        if self.kill_switch():raise ValueError('KILL_SWITCH_ACTIVE')
        inputs=self.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
        if digest(inputs)!=step.input_digest:raise ValueError('INPUT_INTEGRITY_FAILED')
        selected=inputs.get('project')
        if selected is not None:
            binding=s.scalar(select(ProjectBinding).where(ProjectBinding.workflow_id==wf.workflow_id))
            grant=s.scalar(select(Grant).where(Grant.grant_id==selected['grant_id']))
            if (binding is None or grant is None or grant.revoked or grant.expires_at<=self.r.now()
                or binding.project_id!=selected['project_id'] or binding.grant_id!=grant.grant_id
                or binding.grant_version!=grant.version or grant.version!=selected['grant_version']
                or grant.digest!=selected['grant_digest']):
                raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
            body=self.r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
            from personal_agent_dal.timeline.phone_authorization import execution_policy
            if inputs.get('project_policy')!=execution_policy(self.r,s,grant):raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
            if digest(body)!=grant.digest:raise ValueError('INPUT_INTEGRITY_FAILED')
            required={'read','write'} if step.phase in ('coding','fix','stage_commit') else {'read'}
            if body.get('request_id')!=wf.request_id or not required<=set(body.get('actions',[])):
                raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
        return wf,gate

    def accept(self,step_id,*,attempt_id,result,receipt=None):
        def work(s):
            step=s.get(Step,step_id)
            if step is None or step.attempt_id!=attempt_id:raise ValueError('RESULT_SOURCE_INVALID')
            if self.authority is None:raise ValueError('RUNTIME_ADMISSION_REQUIRED')
            execution=self.authority.record_result(s,step,receipt,result)
            if execution.execution_id!=attempt_id:raise ValueError('RESULT_SOURCE_INVALID')
            result_body=validate_result(step.phase,result)
            sha=digest(result_body)
            if step.status in ('completed','retired') and step.result_digest is not None:
                if step.result_digest!=sha:raise ValueError('RESULT_CONFLICT')
                return dict(step_id=step_id,status=step.status,result_digest=sha)
            if step.status not in ('dispatch_started','result_unknown'):raise ValueError('RESULT_SOURCE_INVALID')
            try:wf,gate=self._current(s,step)
            except ValueError as exc:
                inputs=self.r._open(Step,step_id,'sealed_input',step.sealed_input)
                if inputs.get('project_policy') is None or str(exc)!='PROJECT_AUTHORIZATION_REQUIRED':raise
                # A revoked template stops advancement, not receipt observation.
                step.status='retired';step.result_digest=sha
                step.sealed_result=self.r._seal(Step,step_id,'sealed_result',result_body)
                wf=s.get(Workflow,step.workflow_id)
                if wf.status=='active':self._block(s,wf,'PROJECT_AUTHORIZATION_REQUIRED')
                self._event(s,wf,'workflow.result_observed','执行回执已核对；项目授权变化，未推进开发。',step_id=step_id)
                return dict(step_id=step_id,status='retired',result_digest=sha)
            inputs=self.r._open(Step,step_id,'sealed_input',step.sealed_input)
            if digest(inputs)!=step.input_digest:raise ValueError('INPUT_INTEGRITY_FAILED')
            if step.phase in ('design_review','delivery_revision_review'):
                designs=[a for a in inputs['artifacts'] if a['kind']==('revision_plan' if wf.phase=='delivery_revision_review' else 'design')]
                if not designs or (result_body['reviewed_artifact_id'],result_body['reviewed_digest'])!=(designs[-1]['artifact_id'],designs[-1]['digest']):
                    raise ValueError('REVIEW_BINDING_INVALID')
            if step.phase=='project_routing':
                if any({k:v for k,v in candidate.items() if k!='candidate_key'} not in [{k:v for k,v in item.items() if k!='candidate_key'} for item in inputs.get('project_catalog',[])] for candidate in result_body['candidates']):
                    raise ValueError('PROJECT_CATALOG_MISMATCH')
            if step.phase=='researching' and result_body['workspace_receipt_digest']!=inputs.get('workspace_receipt_digest'):
                raise ValueError('WORKSPACE_BINDING_MISMATCH')
            if step.phase=='design_authoring':
                prds=[a for a in inputs['artifacts'] if a['kind']=='prd']
                if not prds or result_body['prd_digest']!=prds[-1]['digest']:raise ValueError('PRD_BINDING_INVALID')
            if (step.phase in ('delivery_publication','delivery_prepare','delivery_probe') and inputs.get('workspace',{}).get('kind')=='existing'
                or step.phase=='project_registration' and inputs.get('authorization',{}).get('registration_policy')=='github_issue'):
                from personal_agent_dal.storage.timeline_models import DevelopmentRemoteEffect
                effect=s.get(DevelopmentRemoteEffect,step_id)
                if effect is None or effect.status!='completed':raise ValueError('REMOTE_EVIDENCE_REQUIRED')
                observed=self.r._open(DevelopmentRemoteEffect,step_id,'sealed_result',effect.sealed_result)
                if step.phase=='project_registration':
                    if result_body['tracker_receipt']!=digest(observed):raise ValueError('REMOTE_EVIDENCE_REQUIRED')
                elif step.phase=='delivery_publication':
                    if any(result_body['manifest'].get(key)!=value for key,value in observed.items()):raise ValueError('REMOTE_EVIDENCE_REQUIRED')
                else:
                    if not 0<=int(self.r.now().timestamp())-observed['observed_at']<=30:
                        raise ValueError('DELIVERY_PROBE_EXPIRED')
                    if step.phase=='delivery_prepare' and any(result_body['manifest'].get(key)!=value for key,value in observed['remote'].items()):
                        raise ValueError('REMOTE_EVIDENCE_REQUIRED')
            step.status='completed';step.result_digest=sha
            step.sealed_result=self.r._seal(Step,step_id,'sealed_result',result_body)
            s.flush()
            if step.phase=='delivery_publication':
                from personal_agent_dal.timeline.delivery import manifest_inputs
                if result_body['manifest']['stage_manifest']!=manifest_inputs(self.r,s,wf):raise ValueError('DELIVERY_BINDING_INVALID')
                wf.phase='delivery_prepare';wf.version+=1
            elif step.phase=='delivery_prepare':
                from personal_agent_dal.timeline.delivery import accept_delivery
                accept_delivery(self,s,wf,step,result_body,execution)
            elif step.phase=='delivery_probe':
                from personal_agent_dal.timeline.delivery import accept_probe
                accept_probe(self,s,wf,step,result_body,execution)
            elif step.phase in ('project_registration','workspace_prepare'):
                from personal_agent_dal.timeline.projects import accept_project
                accept_project(self,s,wf,step,result_body,execution)
            elif step.stage_id is not None:
                from personal_agent_dal.timeline.stage_driver import accept_stage
                accept_stage(self,s,wf,step,result_body,execution)
            elif step.phase=='clarify':
                if result_body['ready']:wf.phase='project_routing';wf.version+=1
                else:
                    wf.status='blocked';wf.blocker_reason='CLARIFICATION_REQUIRED';wf.version+=1
                    self._event(s,wf,'workflow.clarification',result_body['text'],questions=result_body['questions'])
            else:
                id=ArtifactService(self.r).record(step_id=step_id,expected_result_digest=sha,_session=s);s.flush()
                if step.phase=='project_routing':DecisionService(self.r).propose(wf.workflow_id,id,kind='project_selection',candidates=result_body['candidates'],_session=s)
                elif step.phase=='prd_authoring':DecisionService(self.r).propose(wf.workflow_id,id,kind='prd',_session=s)
                elif step.phase=='delivery_revision_planning':
                    from personal_agent_dal.timeline.revisions import accept_plan
                    accept_plan(self,s,wf,result_body)
                elif step.phase=='delivery_revision_review':
                    from personal_agent_dal.timeline.revisions import accept_review
                    accept_review(self,s,wf,step,result_body)
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
