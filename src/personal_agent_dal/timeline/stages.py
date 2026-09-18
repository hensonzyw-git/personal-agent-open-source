"""Frozen Stage revisions and post-commit satisfaction evidence.

Methods accept evidence only from the owning driver/verified executor. These are
not model tools or public operator transitions.
"""
import re
import hashlib
from sqlalchemy import select,delete
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.timeline.requests import digest,valid_id
from personal_agent_dal.storage.timeline_models import DevelopmentStagePlan as Plan,DevelopmentStage as Stage,DevelopmentStageDependency as Edge,DevelopmentDependencySatisfaction as Satisfaction,DevelopmentStageWriter as Writer,DevelopmentWorkflow,DevelopmentGate


def validate_plan(body):
    if not isinstance(body,dict) or set(body)!={'nodes','edges','acceptance'}:raise ValueError('PLAN_INVALID')
    nodes=body['nodes'];edges=body['edges'];keys=[];assigned=[]
    if (not isinstance(edges,list) or len(edges)>4096 or not isinstance(body['acceptance'],list)
        or not body['acceptance'] or any(not isinstance(a,str) or not a.strip() for a in body['acceptance'])):
        raise ValueError('PLAN_INVALID')
    if not isinstance(nodes,list) or not 1<=len(nodes)<=64:raise ValueError('PLAN_INVALID')
    for n in nodes:
        if not isinstance(n,dict) or set(n)!={'stage_id','revision','goal','acceptance'}:raise ValueError('PLAN_INVALID')
        valid_id(n['stage_id'])
        if type(n['revision']) is not int or n['revision']<1 or not isinstance(n['goal'],str) or not n['goal'].strip() or len(n['goal'].encode())>32768:raise ValueError('PLAN_INVALID')
        if not isinstance(n['acceptance'],list) or not n['acceptance'] or any(not isinstance(a,str) or not a.strip() for a in n['acceptance']):raise ValueError('PLAN_INVALID')
        keys.append((n['stage_id'],n['revision']));assigned.extend(n['acceptance'])
    if len({k[0] for k in keys})!=len(keys) or sorted(assigned)!=sorted(body['acceptance']) or len(set(assigned))!=len(assigned):raise ValueError('PLAN_INVALID')
    adjacency={k:[] for k in keys};pairs=set()
    for e in edges:
        if not isinstance(e,dict) or set(e)!={'upstream_id','upstream_revision','downstream_id','downstream_revision'}:raise ValueError('PLAN_INVALID')
        if (any(not isinstance(e[k],str) for k in ('upstream_id','downstream_id'))
            or any(type(e[k]) is not int or e[k]<1 for k in ('upstream_revision','downstream_revision'))):
            raise ValueError('PLAN_INVALID')
        a=(e['upstream_id'],e['upstream_revision']);b=(e['downstream_id'],e['downstream_revision'])
        if a not in adjacency or b not in adjacency or (a,b) in pairs:raise ValueError('PLAN_INVALID')
        pairs.add((a,b));adjacency[a].append(b)
    visiting=set();done=set()
    def visit(k):
        if k in visiting:raise ValueError('PLAN_CYCLE')
        if k in done:return
        visiting.add(k)
        for child in adjacency[k]:visit(child)
        visiting.remove(k);done.add(k)
    for k in keys:visit(k)
    return body


def stage_identity(workflow_id, logical_id):
    """Authority-owned identity, stable across plan revisions and retries."""
    return 'stage_' + hashlib.sha256(
        (workflow_id + '\0' + logical_id).encode('utf-8')).hexdigest()


def plan_stages(session,plan_id):
    from personal_agent_dal.storage.timeline_models import DevelopmentStageMembership as Member
    return list(session.scalars(select(Stage).join(Member,
        (Member.stage_id==Stage.stage_id)&(Member.stage_revision==Stage.revision))
        .where(Member.plan_id==plan_id).order_by(Member.ordinal)))


class StageService:
    def __init__(self,requests):self.r=requests

    def _write(self,fn):
        with self.r.sessions() as s:return run_write_transaction(s,lambda:fn(s))

    def freeze(self,*,workflow_id,revision,design_digest,review_digest,body,_session=None):
        for sha in (design_digest,review_digest):
            if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('ARTIFACT_INVALID')
        if type(revision) is not int or revision<1:raise ValueError('PLAN_INVALID')
        validate_plan(body)
        # Never use a model's local node key as a database-global identity.
        ids={n['stage_id']:stage_identity(workflow_id,n['stage_id']) for n in body['nodes']}
        nodes=[dict(n,stage_id=ids[n['stage_id']]) for n in body['nodes']]
        edges=[dict(e,upstream_id=ids[e['upstream_id']],downstream_id=ids[e['downstream_id']]) for e in body['edges']]
        sha=digest(body)
        def work(s):
            old=s.scalar(select(Plan).where(Plan.workflow_id==workflow_id,Plan.revision==revision))
            if old:
                if (old.dag_digest,old.design_digest,old.review_digest)!=(sha,design_digest,review_digest):raise ValueError('IDEMPOTENCY_CONFLICT')
                return old.plan_id
            workflow=s.get(DevelopmentWorkflow,workflow_id)
            if workflow is None:raise ValueError('WORKFLOW_NOT_FOUND')
            gate=s.get(DevelopmentGate,workflow_id)
            if workflow.status!='active' or gate is None or gate.mode!='open':raise ValueError('EXECUTION_FENCED')
            self._reviewed_design(s,workflow_id,design_digest,review_digest,body)
            # Database stage generations are authority-owned, even when a new
            # approved design reuses a model's local stage name and revision.
            from sqlalchemy import func
            versions={n['stage_id']:max(n['revision'],(s.scalar(select(func.max(Stage.revision)).where(Stage.stage_id==n['stage_id'])) or 0)+1) for n in nodes}
            prepared_nodes=[dict(n,revision=versions[n['stage_id']]) for n in nodes]
            prepared_edges=[dict(e,upstream_revision=versions[e['upstream_id']],downstream_revision=versions[e['downstream_id']]) for e in edges]
            id=new_id();s.add(Plan(plan_id=id,workflow_id=workflow_id,revision=revision,design_digest=design_digest,review_digest=review_digest,dag_digest=sha));s.flush()
            for ordinal,n in enumerate(prepared_nodes):
                key=n['stage_id']+':'+str(n['revision'])
                s.add(Stage(stage_id=n['stage_id'],revision=n['revision'],plan_id=id,ordinal=ordinal,state='pending',state_version=1,
                    sealed_goal=self.r._seal(Stage,key,'sealed_goal',n),review_fix_cycle=0))
            s.flush()
            from personal_agent_dal.storage.timeline_models import DevelopmentStageMembership as Member
            for ordinal,n in enumerate(prepared_nodes):s.add(Member(plan_id=id,stage_id=n['stage_id'],stage_revision=n['revision'],ordinal=ordinal))
            for e in prepared_edges:s.add(Edge(edge_id=new_id(),plan_id=id,**e))
            return id
        return work(_session) if _session is not None else self._write(work)

    def _reviewed_design(self,s,workflow_id,design_digest,review_digest,body):
        from personal_agent_dal.storage.timeline_models import DevelopmentArtifact as Artifact, DevelopmentDriverStep as Step, DevelopmentRoleSnapshot as Snapshot
        design=s.scalar(select(Artifact).where(Artifact.workflow_id==workflow_id,Artifact.kind=='design',Artifact.body_sha256==design_digest).order_by(Artifact.revision.desc()))
        review=s.scalar(select(Artifact).where(Artifact.workflow_id==workflow_id,Artifact.kind=='review',Artifact.source_receipt_digest==review_digest).order_by(Artifact.revision.desc()))
        if design is None or review is None:raise ValueError('REVIEW_SOURCE_REQUIRED')
        design_body=self.r._open(Artifact,design.artifact_id,'sealed_body',design.sealed_body)
        review_body=self.r._open(Artifact,review.artifact_id,'sealed_body',review.sealed_body)
        if (digest(design_body)!=design.source_receipt_digest or digest(review_body)!=review.source_receipt_digest
            or design_body.get('plan')!=body or review_body.get('verdict')!='PASS'
            or review_body.get('findings')!=[] or review_body.get('reviewed_artifact_id')!=design.artifact_id
            or review_body.get('reviewed_digest')!=design_digest):raise ValueError('REVIEW_BINDING_INVALID')
        authors=[]
        for artifact,role in ((design,'planner'),(review,'reviewer')):
            step=s.get(Step,artifact.source_step_id)
            if step is None or step.status!='completed' or step.result_digest!=artifact.source_receipt_digest:
                raise ValueError('REVIEW_SOURCE_REQUIRED')
            self._execution_receipt(s,step)
            snapshot=s.get(Snapshot,step.snapshot_id)
            if snapshot is None:raise ValueError('REVIEW_SOURCE_REQUIRED')
            snap=__import__('json').loads(snapshot.body)
            if digest(snap)!=snapshot.digest:raise ValueError('ROLE_CONFIG_INTEGRITY')
            authors.append(snap['roles'][role]['model'])
        if authors[0]==authors[1]:raise ValueError('REVIEW_NOT_INDEPENDENT')

    def _execution_receipt(self,s,step):
        from personal_agent_dal.storage.timeline_models import DevelopmentExecution as Execution
        execution=s.scalar(select(Execution).where(Execution.step_id==step.step_id))
        if execution is None or not execution.receipt_digest or execution.execution_id!=step.attempt_id:
            raise ValueError('EXECUTOR_RECEIPT_REQUIRED')
        receipt=self.r._open(Execution,execution.execution_id,'sealed_receipt',execution.sealed_receipt)
        if digest(receipt)!=execution.receipt_digest or receipt['result_digest']!=step.result_digest:
            raise ValueError('EXECUTOR_RECEIPT_REQUIRED')
        return execution

    def _stage_evidence(self,s,row,receipt,phase,candidate):
        from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step, DevelopmentExecution as Execution
        execution=s.scalar(select(Execution).where(Execution.receipt_digest==receipt))
        step=s.get(Step,execution.step_id) if execution else None
        plan=s.get(Plan,row.plan_id)
        if (step is None or step.status!='completed' or step.phase!=phase or step.workflow_id!=plan.workflow_id
            or (step.stage_id,step.stage_revision)!=(row.stage_id,row.revision)):
            raise ValueError('EXECUTOR_RECEIPT_REQUIRED')
        self._execution_receipt(s,step)
        result=self.r._open(Step,step.step_id,'sealed_result',step.sealed_result)
        inputs=self.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
        if (digest(inputs)!=step.input_digest or inputs.get('stage',{}).get('state_version')!=row.state_version
            or inputs['stage'].get('dependency_digest')!=row.dependency_digest
            or inputs['stage'].get('plan_digest')!=plan.dag_digest
            or result.get('candidate')!=candidate):raise ValueError('STALE_BINDING')
        return result

    def _dependencies(self,s,row):
        evidence=[]
        for edge in s.scalars(select(Edge).where(Edge.plan_id==row.plan_id,Edge.downstream_id==row.stage_id,Edge.downstream_revision==row.revision).order_by(Edge.edge_id)):
            upstream=s.get(Stage,(edge.upstream_id,edge.upstream_revision))
            proof=s.scalar(select(Satisfaction).where(Satisfaction.edge_id==edge.edge_id))
            if proof is None or upstream.state!='committed' or upstream.commit_digest!=proof.commit_digest or upstream.state_version!=proof.upstream_state_version or (upstream.head_sha,upstream.tree_sha)!=(proof.commit_sha,proof.tree_sha):raise ValueError('DEPENDENCIES_NOT_READY')
            evidence.append(dict(edge_id=edge.edge_id,satisfaction_id=proof.satisfaction_id,commit_digest=proof.commit_digest))
        return digest(evidence)

    def ready(self,plan_id,*,_session=None):
        def work(s):
            result=[]
            for row in plan_stages(s,plan_id):
                if row.state!='pending':continue
                try:sha=self._dependencies(s,row)
                except ValueError:continue
                row.dependency_digest=sha;row.state='ready';row.state_version+=1;result.append(row.stage_id)
            return result
        return work(_session) if _session is not None else self._write(work)

    def _row(self,s,id,revision,version,states):
        row=s.scalar(select(Stage).where(Stage.stage_id==id,Stage.revision==revision))
        if row is None or row.state_version!=version or row.state not in states:raise ValueError('STALE_BINDING')
        plan=s.get(Plan,row.plan_id);workflow=s.get(DevelopmentWorkflow,plan.workflow_id)
        gate=s.get(DevelopmentGate,plan.workflow_id)
        if workflow.status!='active' or gate is None or gate.mode!='open':raise ValueError('EXECUTION_FENCED')
        if row.state!='ready':
            writer=s.get(Writer,plan.workflow_id)
            if writer is None or (writer.stage_id,writer.stage_revision,writer.epoch)!=(row.stage_id,row.revision,gate.epoch):raise ValueError('EXECUTION_FENCED')
        if row.dependency_digest!=self._dependencies(s,row):raise ValueError('DEPENDENCIES_NOT_READY')
        return row,plan

    def claim(self,workflow_id,stage_id,revision,*,expected_version,_session=None):
        def work(s):
            row,plan=self._row(s,stage_id,revision,expected_version,('ready',))
            if plan.workflow_id!=workflow_id or s.get(Writer,workflow_id):raise ValueError('WORKFLOW_WRITER_BUSY')
            first=s.scalar(select(Stage).where(Stage.plan_id==plan.plan_id,Stage.state=='ready').order_by(Stage.ordinal))
            if first.stage_id!=stage_id:raise ValueError('STAGE_ORDER_CONFLICT')
            s.add(Writer(workflow_id=workflow_id,stage_id=stage_id,stage_revision=revision,epoch=s.get(DevelopmentGate,workflow_id).epoch))
            row.state='coding';row.state_version+=1
            return dict(stage_id=stage_id,revision=revision,state=row.state,state_version=row.state_version,dependency_digest=row.dependency_digest)
        return work(_session) if _session is not None else self._write(work)

    def _candidate(self,row,body):
        if set(body)!={'base_sha','head_sha','tree_sha'} or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{40}',v) for v in body.values()):raise ValueError('CANDIDATE_INVALID')
        if any(getattr(row,k)!=v for k,v in body.items()):raise ValueError('STALE_BINDING')

    def candidate(self,id,revision,*,expected_version,_session=None,**candidate):
        def work(s):
            row,_=self._row(s,id,revision,expected_version,('coding','fixing'))
            for k,v in candidate.items():
                if k not in ('base_sha','head_sha','tree_sha') or not isinstance(v,str) or not re.fullmatch('[a-f0-9]{40}',v):raise ValueError('CANDIDATE_INVALID')
            if set(candidate)!={'base_sha','head_sha','tree_sha'}:raise ValueError('CANDIDATE_INVALID')
            for k,v in candidate.items():setattr(row,k,v)
            row.verification_digest=row.review_digest=row.commit_digest=None
            row.state='verifying';row.state_version+=1
        return work(_session) if _session is not None else self._write(work)

    def verified(self,id,revision,*,expected_version,receipt_digest,passed=True,_session=None,**candidate):
        self._evidence(id,revision,expected_version,receipt_digest,('verifying',),'reviewing' if passed else 'fixing','verification_digest',candidate,_session)

    def reviewed(self,id,revision,*,expected_version,receipt_digest,passed,_session=None,**candidate):
        self._evidence(id,revision,expected_version,receipt_digest,('reviewing',),'commit_ready' if passed else 'fixing','review_digest',candidate,_session)

    def _evidence(self,id,revision,version,receipt,states,target,field,candidate,_session=None):
        if not re.fullmatch('[a-f0-9]{64}',receipt):raise ValueError('RECEIPT_INVALID')
        def work(s):
            row,plan=self._row(s,id,revision,version,states);self._candidate(row,candidate)
            result=self._stage_evidence(s,row,receipt,'verify' if field=='verification_digest' else 'code_review',candidate)
            if (result.get('passed') is not True) != (target=='fixing'):raise ValueError('RECEIPT_INVALID')
            setattr(row,field,receipt)
            if target=='fixing':
                row.review_fix_cycle+=1
                row.verification_digest=row.review_digest=None
                if row.review_fix_cycle>=3:target_state='blocked'
                else:target_state=target
            else:target_state=target
            row.state=target_state;row.state_version+=1
            if target_state=='blocked':
                writer=s.get(Writer,plan.workflow_id)
                if writer is not None:s.delete(writer)
        return work(_session) if _session is not None else self._write(work)

    def committed(self,id,revision,*,expected_version,receipt_digest,_session=None,**candidate):
        if not re.fullmatch('[a-f0-9]{64}',receipt_digest):raise ValueError('RECEIPT_INVALID')
        def work(s):
            row,plan=self._row(s,id,revision,expected_version,('commit_ready',));self._candidate(row,candidate)
            result=self._stage_evidence(s,row,receipt_digest,'stage_commit',candidate)
            if result.get('committed') is not True:raise ValueError('COMMIT_NOT_AUTHORIZED')
            writer=s.get(Writer,plan.workflow_id)
            if writer is None or (writer.stage_id,writer.stage_revision)!=(id,revision) or not row.verification_digest or not row.review_digest:raise ValueError('COMMIT_NOT_AUTHORIZED')
            row.head_sha=result['commit_sha']
            row.state='committed';row.state_version+=1;row.commit_digest=receipt_digest
            for edge in s.scalars(select(Edge).where(Edge.plan_id==row.plan_id,Edge.upstream_id==id,Edge.upstream_revision==revision)):
                s.add(Satisfaction(satisfaction_id=new_id(),edge_id=edge.edge_id,commit_digest=receipt_digest,commit_sha=row.head_sha,tree_sha=row.tree_sha,upstream_state_version=row.state_version,observed_at=self.r.now()))
            s.delete(writer)
        return work(_session) if _session is not None else self._write(work)
