"""Immutable artifact publication bound to a completed executor result."""
import hashlib
from personal_agent_core.manifest import canonical_json
from sqlalchemy import select,func
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.machine.execution_results import _SECRET
from personal_agent_dal.storage.timeline_models import DevelopmentArtifact as Artifact,DevelopmentDriverStep as Step,DevelopmentWorkflow,DevelopmentRequest


KINDS={'project_route','research','prd','design','review','revision_plan','delivery'}


class ArtifactService:
    def __init__(self,requests):self.r=requests

    def record(self,*,step_id,expected_result_digest,_session=None):
        def work(s):
            step=s.scalar(select(Step).where(Step.step_id==step_id))
            if step is None or step.status!='completed' or step.result_digest!=expected_result_digest:raise ValueError('ARTIFACT_SOURCE_INVALID')
            result=self.r._open(Step,step_id,'sealed_result',step.sealed_result)
            from personal_agent_dal.timeline.requests import digest
            if digest(result)!=expected_result_digest:raise ValueError('ARTIFACT_SOURCE_INVALID')
            raw=result.get('text')
            # Leak detection must precede semantic validation.
            if _SECRET.search(canonical_json(result)):raise ValueError('ARTIFACT_SECRET')
            if not isinstance(raw,str) or not raw.strip() or len(raw.encode())>2*1024*1024 or result.get('kind') not in KINDS:raise ValueError('ARTIFACT_INVALID')
            prior=s.scalar(select(Artifact).where(Artifact.source_step_id==step_id))
            if prior:
                if prior.source_receipt_digest!=expected_result_digest:raise ValueError('ARTIFACT_SOURCE_INVALID')
                return prior.artifact_id
            previous=s.scalar(select(Artifact).where(Artifact.workflow_id==step.workflow_id,Artifact.kind==result['kind']).order_by(Artifact.revision.desc()))
            id=new_id();row=Artifact(artifact_id=id,workflow_id=step.workflow_id,kind=result['kind'],revision=previous.revision+1 if previous else 1,
                body_sha256=hashlib.sha256(raw.encode()).hexdigest(),sealed_body=self.r._seal(Artifact,id,'sealed_body',result),
                source_step_id=step_id,source_receipt_digest=expected_result_digest,supersedes_id=previous.artifact_id if previous else None)
            s.add(row);return id
        if _session is not None:return work(_session)
        with self.r.sessions() as s:return run_write_transaction(s,lambda:work(s))

    def read(self,id,*,offset=0,limit=65536):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 4<=limit<=65536:raise ValueError('INVALID_ARGUMENT')
        with self.r.sessions() as s:
            row=s.scalar(select(Artifact).where(Artifact.artifact_id==id))
            if row is None:raise ValueError('ARTIFACT_NOT_FOUND')
            workflow=s.get(DevelopmentWorkflow,row.workflow_id)
            self.r._detail(s,s.get(DevelopmentRequest,workflow.request_id))  # Same repository policy as task detail.
            body=self.r._open(Artifact,id,'sealed_body',row.sealed_body)
            raw=body['text'].encode()
            if hashlib.sha256(raw).hexdigest()!=row.body_sha256 or offset>len(raw):raise ValueError('ARTIFACT_INVALID')
            stop=min(offset+limit,len(raw))
            while stop<len(raw) and raw[stop]&0xc0==0x80:stop-=1
            try:text=raw[offset:stop].decode()
            except UnicodeError:raise ValueError('INVALID_ARGUMENT') from None
            return dict(schema_version='dal.timeline/1.0',artifact_id=id,workflow_id=row.workflow_id,kind=row.kind,revision=row.revision,
                body_sha256=row.body_sha256,offset=offset,total_bytes=len(raw),text=text,next_offset=stop if stop<len(raw) else None,complete=stop==len(raw))
