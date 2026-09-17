"""Frozen Stage revisions and post-commit satisfaction evidence.

Methods accept evidence only from the owning driver/verified executor. These are
not model tools or public operator transitions.
"""
import re
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


class StageService:
    def __init__(self,requests):self.r=requests

    def _write(self,fn):
        with self.r.sessions() as s:return run_write_transaction(s,lambda:fn(s))

    def freeze(self,*,workflow_id,revision,design_digest,review_digest,body):
        for sha in (design_digest,review_digest):
            if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('ARTIFACT_INVALID')
        if type(revision) is not int or revision<1:raise ValueError('PLAN_INVALID')
        validate_plan(body)
        nodes,edges=body['nodes'],body['edges']
        sha=digest(body)
        def work(s):
            old=s.scalar(select(Plan).where(Plan.workflow_id==workflow_id,Plan.revision==revision))
            if old:
                if (old.dag_digest,old.design_digest,old.review_digest)!=(sha,design_digest,review_digest):raise ValueError('IDEMPOTENCY_CONFLICT')
                return old.plan_id
            if s.get(DevelopmentWorkflow,workflow_id) is None:raise ValueError('WORKFLOW_NOT_FOUND')
            id=new_id();s.add(Plan(plan_id=id,workflow_id=workflow_id,revision=revision,design_digest=design_digest,review_digest=review_digest,dag_digest=sha));s.flush()
            for ordinal,n in enumerate(nodes):
                key=n['stage_id']+':'+str(n['revision'])
                s.add(Stage(stage_id=n['stage_id'],revision=n['revision'],plan_id=id,ordinal=ordinal,state='pending',state_version=1,
                    sealed_goal=self.r._seal(Stage,key,'sealed_goal',n),review_fix_cycle=0))
            s.flush()
            for e in edges:s.add(Edge(edge_id=new_id(),plan_id=id,**e))
            return id
        return self._write(work)

    def _dependencies(self,s,row):
        evidence=[]
        for edge in s.scalars(select(Edge).where(Edge.plan_id==row.plan_id,Edge.downstream_id==row.stage_id,Edge.downstream_revision==row.revision).order_by(Edge.edge_id)):
            upstream=s.get(Stage,(edge.upstream_id,edge.upstream_revision))
            proof=s.scalar(select(Satisfaction).where(Satisfaction.edge_id==edge.edge_id))
            if proof is None or upstream.state!='committed' or upstream.commit_digest!=proof.commit_digest or upstream.state_version!=proof.upstream_state_version or (upstream.head_sha,upstream.tree_sha)!=(proof.commit_sha,proof.tree_sha):raise ValueError('DEPENDENCIES_NOT_READY')
            evidence.append(dict(edge_id=edge.edge_id,satisfaction_id=proof.satisfaction_id,commit_digest=proof.commit_digest))
        return digest(evidence)

    def ready(self,plan_id):
        def work(s):
            result=[]
            for row in s.scalars(select(Stage).where(Stage.plan_id==plan_id,Stage.state=='pending').order_by(Stage.ordinal)):
                try:sha=self._dependencies(s,row)
                except ValueError:continue
                row.dependency_digest=sha;row.state='ready';row.state_version+=1;result.append(row.stage_id)
            return result
        return self._write(work)

    def _row(self,s,id,revision,version,states):
        row=s.scalar(select(Stage).where(Stage.stage_id==id,Stage.revision==revision))
        if row is None or row.state_version!=version or row.state not in states:raise ValueError('STALE_BINDING')
        plan=s.get(Plan,row.plan_id);workflow=s.get(DevelopmentWorkflow,plan.workflow_id)
        gate=s.get(DevelopmentGate,plan.workflow_id)
        if workflow.status!='active' or (gate and gate.mode!='open'):raise ValueError('EXECUTION_FENCED')
        if row.dependency_digest!=self._dependencies(s,row):raise ValueError('DEPENDENCIES_NOT_READY')
        return row,plan

    def claim(self,workflow_id,stage_id,revision,*,expected_version):
        def work(s):
            row,plan=self._row(s,stage_id,revision,expected_version,('ready',))
            if plan.workflow_id!=workflow_id or s.get(Writer,workflow_id):raise ValueError('WORKFLOW_WRITER_BUSY')
            first=s.scalar(select(Stage).where(Stage.plan_id==plan.plan_id,Stage.state=='ready').order_by(Stage.ordinal))
            if first.stage_id!=stage_id:raise ValueError('STAGE_ORDER_CONFLICT')
            s.add(Writer(workflow_id=workflow_id,stage_id=stage_id,stage_revision=revision,epoch=1))
            row.state='coding';row.state_version+=1
            return dict(stage_id=stage_id,revision=revision,state=row.state,state_version=row.state_version,dependency_digest=row.dependency_digest)
        return self._write(work)

    def _candidate(self,row,body):
        if set(body)!={'base_sha','head_sha','tree_sha'} or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{40}',v) for v in body.values()):raise ValueError('CANDIDATE_INVALID')
        if any(getattr(row,k)!=v for k,v in body.items()):raise ValueError('STALE_BINDING')

    def candidate(self,id,revision,*,expected_version,**candidate):
        def work(s):
            row,_=self._row(s,id,revision,expected_version,('coding','fixing'))
            for k,v in candidate.items():
                if k not in ('base_sha','head_sha','tree_sha') or not isinstance(v,str) or not re.fullmatch('[a-f0-9]{40}',v):raise ValueError('CANDIDATE_INVALID')
            if set(candidate)!={'base_sha','head_sha','tree_sha'}:raise ValueError('CANDIDATE_INVALID')
            for k,v in candidate.items():setattr(row,k,v)
            row.verification_digest=row.review_digest=row.commit_digest=None
            row.state='verifying';row.state_version+=1
        self._write(work)

    def verified(self,id,revision,*,expected_version,receipt_digest,passed=True,**candidate):
        self._evidence(id,revision,expected_version,receipt_digest,('verifying',),'reviewing' if passed else 'fixing','verification_digest',candidate)

    def reviewed(self,id,revision,*,expected_version,receipt_digest,passed,**candidate):
        self._evidence(id,revision,expected_version,receipt_digest,('reviewing',),'commit_ready' if passed else 'fixing','review_digest',candidate)

    def _evidence(self,id,revision,version,receipt,states,target,field,candidate):
        if not re.fullmatch('[a-f0-9]{64}',receipt):raise ValueError('RECEIPT_INVALID')
        def work(s):
            row,_=self._row(s,id,revision,version,states);self._candidate(row,candidate)
            setattr(row,field,receipt)
            if target=='fixing':
                row.review_fix_cycle+=1
                row.verification_digest=row.review_digest=None
                if row.review_fix_cycle>=3:target_state='blocked'
                else:target_state=target
            else:target_state=target
            row.state=target_state;row.state_version+=1
        self._write(work)

    def committed(self,id,revision,*,expected_version,receipt_digest,**candidate):
        if not re.fullmatch('[a-f0-9]{64}',receipt_digest):raise ValueError('RECEIPT_INVALID')
        def work(s):
            row,plan=self._row(s,id,revision,expected_version,('commit_ready',));self._candidate(row,candidate)
            writer=s.get(Writer,plan.workflow_id)
            if writer is None or (writer.stage_id,writer.stage_revision)!=(id,revision) or not row.verification_digest or not row.review_digest:raise ValueError('COMMIT_NOT_AUTHORIZED')
            row.state='committed';row.state_version+=1;row.commit_digest=receipt_digest
            for edge in s.scalars(select(Edge).where(Edge.plan_id==row.plan_id,Edge.upstream_id==id,Edge.upstream_revision==revision)):
                s.add(Satisfaction(satisfaction_id=new_id(),edge_id=edge.edge_id,commit_digest=receipt_digest,commit_sha=row.head_sha,tree_sha=row.tree_sha,upstream_state_version=row.state_version,observed_at=self.r.now()))
            s.delete(writer)
        self._write(work)
