from personal_agent_dal.timeline.stages import plan_stages
"""Frozen delivery proposals and fresh observer-bound atomic acceptance."""
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.storage.timeline_models import (
    DevelopmentDelivery as Delivery,DevelopmentAcceptanceProbe as Probe,DevelopmentWorkflow as Workflow,
    DevelopmentGate as Gate,DevelopmentDriverStep as Step,DevelopmentStageWriter as Writer,
    DevelopmentStage as Stage,DevelopmentStagePlan as Plan,DevelopmentWorkspace as Workspace,
    DevelopmentDecisionRequest as Decision,DevelopmentDecisionReceipt as Receipt,DevelopmentCommand as Command,
    DevelopmentRequest as Request,DevelopmentArtifact as Artifact,
)
from personal_agent_dal.timeline.requests import digest,TimelineRefusal
from personal_agent_dal.timeline.artifacts import ArtifactService


def manifest_inputs(requests,s,wf):
    plan=s.scalar(select(Plan).where(Plan.workflow_id==wf.workflow_id).order_by(Plan.revision.desc()))
    if plan is None:raise ValueError('STAGES_NOT_COMPLETE')
    stages=plan_stages(s,plan.plan_id)
    if not stages or any(row.state!='committed' for row in stages):raise ValueError('STAGES_NOT_COMPLETE')
    if s.get(Writer,wf.workflow_id):raise ValueError('WRITER_NOT_QUIESCENT')
    from personal_agent_dal.storage.models import Feature
    feature=s.get(Feature,wf.feature_id)
    if feature is None or feature.state!='intake':raise ValueError('FEATURE_REFERENCE_INVALID')
    workspace=s.get(Workspace,wf.workflow_id)
    if workspace is None:raise ValueError('WORKSPACE_REQUIRED')
    commits=[]
    for step in s.scalars(select(Step).where(Step.workflow_id==wf.workflow_id,Step.phase=='stage_commit',Step.status=='completed').order_by(Step.expected_version)):
        result=requests._open(Step,step.step_id,'sealed_result',step.sealed_result)
        if digest(result)!=step.result_digest:raise ValueError('COMMIT_RECEIPT_INVALID')
        commits.append(dict(sha=result['commit_sha'],parent=result['parent_sha'],tree=result['candidate']['tree_sha']))
    return dict(commit_history=commits,plan_id=plan.plan_id,plan_digest=plan.dag_digest,design_digest=plan.design_digest,
        feature_id=feature.feature_id,feature_version=feature.version,
        workspace=requests._open(Workspace,wf.workflow_id,'sealed_manifest',workspace.sealed_manifest),
        stages=[dict(stage_id=row.stage_id,revision=row.revision,state_version=row.state_version,
            base_sha=row.base_sha,head_sha=row.head_sha,tree_sha=row.tree_sha,
            verification_digest=row.verification_digest,review_digest=row.review_digest,commit_digest=row.commit_digest) for row in stages])


def freeze_dispatch(driver,s,wf):
    gate=s.get(Gate,wf.workflow_id)
    if gate is None:raise ValueError('EXECUTION_FENCED')
    if s.scalar(select(Step.step_id).where(Step.workflow_id==wf.workflow_id,Step.status.in_(('dispatch_started','result_unknown'))).limit(1)):
        raise ValueError('RECONCILIATION_REQUIRED')
    if gate.mode=='open':gate.mode='paused';gate.version+=1;gate.epoch+=1
    if gate.mode!='paused':raise ValueError('EXECUTION_FENCED')
    for step in s.scalars(select(Step).where(Step.workflow_id==wf.workflow_id,Step.status=='prepared')):step.status='retired'
    return manifest_inputs(driver.r,s,wf)


def accept_delivery(driver,s,wf,step,result,execution):
    inputs=driver.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
    if result['manifest']['stage_manifest']!=inputs['delivery'] or inputs['delivery']!=manifest_inputs(driver.r,s,wf):
        raise ValueError('DELIVERY_BINDING_INVALID')
    expected=inputs['delivery']['workspace']['kind']
    if result['manifest']['kind']!=('pr' if expected=='existing' else 'local'):
        raise ValueError('DELIVERY_KIND_INVALID')
    gate=s.get(Gate,wf.workflow_id)
    if gate.mode!='paused':raise ValueError('DELIVERY_NOT_QUIESCENT')
    artifact_id=ArtifactService(driver.r).record(step_id=step.step_id,expected_result_digest=step.result_digest,_session=s)
    s.flush()
    ident=new_id()
    s.add(Delivery(delivery_id=ident,workflow_id=wf.workflow_id,artifact_id=artifact_id,manifest_digest=digest(result['manifest']),
        sealed_manifest=driver.r._seal(Delivery,ident,'sealed_manifest',result['manifest']),source_step_id=step.step_id,
        gate_version=gate.version,gate_epoch=gate.epoch));s.flush()
    from personal_agent_dal.timeline.decisions import DecisionService
    DecisionService(driver.r).propose(wf.workflow_id,artifact_id,kind='delivery',_session=s)


class DeliveryService:
    def __init__(self,requests):self.r=requests

    def process(self,**command):
        from personal_agent_dal.timeline.decisions import parse_decision,DecisionService
        parsed=parse_decision(command['text'])
        if parsed is None or parsed['decision']!='approve':
            return DecisionService(self.r)._process_standard(**command)
        fingerprint=digest(dict(kind='decision',**command))
        def begin(s):
            old=s.get(Command,command['command_id'])
            if old:
                if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return self.r._open(Command,old.command_id,'sealed_result',old.sealed_result)
            decision=s.get(Decision,command['decision_id'])
            if decision is None or decision.kind!='delivery' or decision.status!='pending' or decision.binding_digest!=command['binding_digest'] or decision.expires_at<=self.r.now():
                raise ValueError('STALE_BINDING')
            binding=self.r._open(Decision,decision.decision_id,'sealed_binding',decision.sealed_binding)
            wf=s.get(Workflow,decision.workflow_id);gate=s.get(Gate,wf.workflow_id)
            if (wf.status!='active' or wf.phase!='delivery_waiting' or wf.version!=binding['workflow_version']
                or gate.mode!='paused' or (gate.version,gate.epoch)!=(binding['gate_version'],binding['gate_epoch'])):
                raise ValueError('STALE_BINDING')
            probe=s.get(Probe,command['command_id'])
            if probe is None:
                probe=Probe(command_id=command['command_id'],source_message_ref=command['source_message_ref'],decision_id=decision.decision_id,
                    workflow_id=wf.workflow_id,fingerprint=fingerprint,nonce=new_id(),
                    sealed_command=self.r._seal(Probe,command['command_id'],'sealed_command',command))
                s.add(probe)
            elif probe.fingerprint!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
            if probe.result_digest is None:return None
            if (self.r.now()-probe.observed_at).total_seconds()>30:
                # Refresh a read-only observation, never a coder or commit.
                probe.nonce=new_id();probe.step_id=None;probe.result_digest=None;probe.sealed_result=None;probe.observed_at=None
                return None
            observed=self.r._open(Probe,probe.command_id,'sealed_result',probe.sealed_result)
            if digest(observed)!=probe.result_digest or observed['nonce']!=probe.nonce:raise ValueError('DELIVERY_PROBE_INVALID')
            delivery=s.scalar(select(Delivery).where(Delivery.artifact_id==binding['artifact_id']))
            manifest=self.r._open(Delivery,delivery.delivery_id,'sealed_manifest',delivery.sealed_manifest)
            if (observed['manifest_digest']!=delivery.manifest_digest or observed['matches'] is not True
                or manifest['stage_manifest']!=manifest_inputs(self.r,s,wf)):raise ValueError('DELIVERY_HEAD_CHANGED')
            if s.scalar(select(Step.step_id).where(Step.workflow_id==wf.workflow_id,Step.status.in_(('dispatch_started','result_unknown'))).limit(1)):
                raise ValueError('RECONCILIATION_REQUIRED')
            for step in s.scalars(select(Step).where(Step.workflow_id==wf.workflow_id,Step.status=='prepared')):step.status='retired'
            from personal_agent_dal.timeline.driver import WorkflowDriver
            WorkflowDriver(self.r,roles=None)._current(s,s.get(Step,probe.step_id))
            wf.phase='accepted';wf.status='completed';wf.version+=1
            wf.accepted_delivery_id=delivery.delivery_id;wf.acceptance_receipt_id=command['command_id'];wf.completed_at=self.r.now()
            gate.mode='delivered';gate.version+=1;gate.epoch+=1;decision.status='consumed'
            result=dict(command_id=command['command_id'],decision_id=decision.decision_id,workflow_id=wf.workflow_id,
                workflow_version=wf.version,status='accepted',decision='approve',phase='accepted',workflow_status='completed',
                delivery_id=delivery.delivery_id,manifest_digest=delivery.manifest_digest,source_text=command['text'],feedback='')
            s.add(Receipt(command_id=command['command_id'],source_message_ref=command['source_message_ref'],decision_id=decision.decision_id,
                body_digest=digest({k:v for k,v in command.items() if k!='expected_kind'}),
                sealed_result=self.r._seal(Receipt,command['command_id'],'sealed_result',result)))
            s.add(Command(command_id=command['command_id'],body_sha256=fingerprint,sealed_result=self.r._seal(Command,command['command_id'],'sealed_result',result)))
            from personal_agent_dal.storage.audit import append_audit_event
            append_audit_event(s,event_id=new_id(),trace_id=wf.workflow_id,event_type='development.delivery.accepted',redacted_summary='immutable delivery accepted; no merge or deploy',now=self.r.now())
            self.r._append_event(s,s.get(Request,wf.request_id),'decision.accepted',dict(summary='交付版本已回读并验收。',decision_id=decision.decision_id,status=wf.status,phase=wf.phase))
            return result
        with self.r.sessions() as s:result=run_write_transaction(s,lambda:begin(s))
        if result is None:raise TimelineRefusal('DELIVERY_PROBE_PENDING')
        return result


def prepare_probe(driver,s,wf):
    probe=s.scalar(select(Probe).where(Probe.workflow_id==wf.workflow_id,Probe.step_id.is_(None),Probe.result_digest.is_(None)).order_by(Probe.command_id))
    if probe is None:
        existing=s.scalar(select(Step.step_id).where(Step.workflow_id==wf.workflow_id,Step.phase=='delivery_probe',Step.status=='prepared'))
        return existing
    decision=s.get(Decision,probe.decision_id)
    if decision.status!='pending' or decision.expires_at<=driver.r.now():return
    binding=driver.r._open(Decision,decision.decision_id,'sealed_binding',decision.sealed_binding)
    if binding['workflow_version']!=wf.version:return
    delivery=s.scalar(select(Delivery).where(Delivery.artifact_id==binding['artifact_id']))
    gate=s.get(Gate,wf.workflow_id)
    snapshot=driver.roles.snapshot(workflow_id=wf.workflow_id,_session=s)
    manifest=driver.r._open(Delivery,delivery.delivery_id,'sealed_manifest',delivery.sealed_manifest)
    # Exact original permission/workspace input from the completed delivery step.
    source=s.get(Step,delivery.source_step_id)
    inputs=driver.r._open(Step,source.step_id,'sealed_input',source.sealed_input)
    inputs.update(phase='delivery_probe',role='planner',workflow_version=wf.version,gate_epoch=gate.epoch,
        snapshot_digest=snapshot['snapshot_digest'],probe={'nonce':probe.nonce,'manifest_digest':delivery.manifest_digest,'manifest':manifest})
    ident=new_id()
    s.add(Step(step_id=ident,workflow_id=wf.workflow_id,phase='delivery_probe',input_digest=digest(inputs),
        sealed_input=driver.r._seal(Step,ident,'sealed_input',inputs),snapshot_id=snapshot['snapshot_id'],
        expected_version=wf.version,gate_epoch=gate.epoch,cycle=1,status='prepared'))
    s.flush();probe.step_id=ident
    return ident


def accept_probe(driver,s,wf,step,result,execution):
    probe=s.scalar(select(Probe).where(Probe.step_id==step.step_id))
    inputs=driver.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
    if (probe is None or result['nonce']!=probe.nonce or result['manifest_digest']!=inputs['probe']['manifest_digest']):
        raise ValueError('DELIVERY_PROBE_INVALID')
    probe.result_digest=digest(result);probe.sealed_result=driver.r._seal(Probe,probe.command_id,'sealed_result',result)
    from datetime import datetime,timezone
    observed=datetime.fromtimestamp(result['observed_at'],timezone.utc)
    if not 0<=(driver.r.now()-observed).total_seconds()<=30:raise ValueError('DELIVERY_PROBE_EXPIRED')
    probe.observed_at=observed
