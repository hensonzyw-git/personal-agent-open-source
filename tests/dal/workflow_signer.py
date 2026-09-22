"""Synthetic keys, real signatures; never install these in a service config."""
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from personal_agent.api.dal_client import sign_decision
from personal_agent_dal.timeline.driver import WorkflowDriver
from personal_agent_dal.timeline.execution import ExecutionAuthority
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.storage.timeline_models import DevelopmentExecution, DevelopmentRoleSnapshot, DevelopmentDriverStep


class SignedDriver(WorkflowDriver):
    """Test convenience around the actual cryptographic execution boundary."""
    def __init__(self,r,*,roles,kill_switch=lambda:False):
        self.test_key=ec.generate_private_key(ec.SECP256R1())
        self.test_registry={}
        super().__init__(r,roles=roles,kill_switch=kill_switch,authority=ExecutionAuthority(r,self.test_registry))

    def proof(self,step_id,domain,payload):
        with self.r.sessions() as s:
            row=s.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step_id))
            if row is None:return 'invalid-synthetic-assertion'
            now=int(self.r.now().timestamp())
            return sign_decision(dict(iss='synthetic',aud='dal-workflow',domain=domain,jti='proof',iat=now,exp=now+120,
                step_id=step_id,execution_id=row.execution_id,binding_digest=row.binding_digest,payload_digest=digest(payload)),
                key=self.test_key,kid='test')

    def dispatch(self,step_id,*,admission):
        if not admission:return super().dispatch(step_id,admission=admission)
        with self.r.sessions() as s:
            step=s.get(DevelopmentDriverStep,step_id);snapshot=s.get(DevelopmentRoleSnapshot,step.snapshot_id)
            now=int(self.r.now().timestamp())
            self.test_registry['synthetic']=dict(keys={'test':self.test_key.public_key()},admission=dict(
                schema='dal.workflow-admission/1.0',worker_id='synthetic',boot_id='test-boot',supervisor_epoch=1,
                snapshot_digest=snapshot.digest,issued_at=now-1,expires_at=now+1800,revoked=False,evidence_digest='a'*64))
            sha=step.input_digest
        self.reserve(step_id,worker_id='synthetic')
        return super().dispatch(step_id,admission=self.proof(step_id,'dal.workflow-prelaunch/1.0',{'input_digest':sha}))

    def accept(self,step_id,*,attempt_id,result,receipt=None):
        return super().accept(step_id,attempt_id=attempt_id,result=result,
            receipt=receipt or self.proof(step_id,'dal.workflow-result/1.0',result))
