"""Authenticated workflow execution, separate from legacy report-only contracts.

Registry entries are operator-installed metadata. Every admission is checked
again at dispatch and result acceptance. A consumed dispatch never becomes a
fresh attempt after a timeout. Signatures prove provenance, not model accuracy.
"""
from datetime import timedelta
from typing import Literal
from pydantic import StrictInt
from sqlalchemy import select
from personal_agent.api.dal_client import verify_closed_assertion
from personal_agent_core.ids import new_id
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest
from personal_agent_dal.storage.timeline_models import (
    DevelopmentExecution as Execution, DevelopmentRoleSnapshot as Snapshot,
)
from personal_agent_dal.timeline.requests import digest
import json


class WorkerProof(Closed):
    iss: Id
    aud: Literal['dal-workflow']
    domain: Literal['dal.workflow-prelaunch/1.0','dal.workflow-result/1.0','dal.workflow-stop/1.0','dal.workflow-publication/1.0']
    jti: Id
    iat: StrictInt
    exp: StrictInt
    step_id: Id
    execution_id: Id
    binding_digest: Digest
    payload_digest: Digest


class ExecutionAuthority:
    def __init__(self, requests, registry):
        self.r, self.registry = requests, registry

    def available(self,body):
        from types import SimpleNamespace
        from personal_agent_core.manifest import canonical_json
        snapshot=SimpleNamespace(body=canonical_json(body),digest=digest(body))
        for worker_id in self.registry:
            try:self.admission(worker_id,snapshot)
            except (ValueError,OSError,KeyError,TypeError):continue
            return True
        return False

    def _worker(self, worker_id):
        record=self.registry.get(worker_id)
        if record is None:raise ValueError('WORKER_NOT_AUTHORIZED')
        # Callable sources reload revocation and operator-controlled admission
        # files; in-memory records are useful for isolated synthetic tests.
        return record() if callable(record) else record

    def admission(self, worker_id, snapshot, *, now=None):
        record=self._worker(worker_id)
        now=self.r.now() if now is None else now
        admission=record['admission']
        if (set(admission)!={'schema','worker_id','boot_id','supervisor_epoch','snapshot_digest','issued_at','expires_at','revoked','evidence_digest'}
            or admission['schema']!='dal.workflow-admission/1.0'
            or admission['worker_id']!=worker_id or admission['snapshot_digest']!=snapshot.digest
            or admission['revoked'] is not False
            or not admission['issued_at']<=int(now.timestamp())<admission['expires_at']
            or type(admission['supervisor_epoch']) is not int or admission['supervisor_epoch']<1):
            raise ValueError('RUNTIME_ADMISSION_REQUIRED')
        body=json.loads(snapshot.body)
        if digest(body)!=snapshot.digest or body['contract_version']!='dal.role-contract/3.0':
            raise ValueError('ROLE_CONFIG_INTEGRITY')
        return record,admission

    def reserve(self, s, step, worker_id):
        snapshot=s.get(Snapshot,step.snapshot_id)
        if snapshot is None:raise ValueError('RUNTIME_ADMISSION_REQUIRED')
        _,admission=self.admission(worker_id,snapshot)
        prior=s.scalar(select(Execution).where(Execution.step_id==step.step_id))
        if prior:
            if prior.worker_id!=worker_id:raise ValueError('EXECUTION_ALREADY_OWNED')
            return self.binding(prior)
        execution_id=new_id()
        inputs=self.r._open(__import__('personal_agent_dal.storage.timeline_models',fromlist=['DevelopmentDriverStep']).DevelopmentDriverStep,
            step.step_id,'sealed_input',step.sealed_input)
        authorization=inputs.get('authorization')
        grant_id=inputs.get('project',{}).get('grant_id')
        spent=sum(row.charged_seconds if row.charged_seconds is not None else row.reserved_seconds
            for row in s.scalars(select(Execution).where(Execution.grant_id==grant_id))) if grant_id else 0
        remaining=authorization['budget_seconds']-spent if authorization else 600
        if remaining<15:raise ValueError('EXECUTION_BUDGET_EXHAUSTED')
        budget=min(600,remaining)
        until=min(self.r.now()+timedelta(seconds=budget),
            __import__('datetime').datetime.fromtimestamp(admission['expires_at'],__import__('datetime').timezone.utc))
        if authorization:
            from datetime import datetime
            until=min(until,datetime.fromisoformat(authorization['expires_at']))
        binding=dict(schema='dal.workflow-execution/1.0',owner={'kind':'workflow','workflow_id':step.workflow_id},
            step_id=step.step_id,execution_id=execution_id,input_digest=step.input_digest,
            snapshot_id=step.snapshot_id,snapshot_digest=snapshot.digest,
            expected_version=step.expected_version,gate_epoch=step.gate_epoch,
            worker_id=worker_id,boot_id=admission['boot_id'],supervisor_epoch=admission['supervisor_epoch'],
            lease_id=new_id(),lease_until=until.isoformat(),admission_digest=digest(admission))
        s.add(Execution(execution_id=execution_id,step_id=step.step_id,worker_id=worker_id,
            boot_id=binding['boot_id'],supervisor_epoch=binding['supervisor_epoch'],lease_id=binding['lease_id'],
            lease_until=until,grant_id=grant_id,reserved_seconds=budget,admission_digest=binding['admission_digest'],binding_digest=digest(binding),
            sealed_binding=self.r._seal(Execution,execution_id,'sealed_binding',binding)))
        return binding

    def binding(self,row):
        binding=self.r._open(Execution,row.execution_id,'sealed_binding',row.sealed_binding)
        if digest(binding)!=row.binding_digest:raise ValueError('EXECUTION_BINDING_INVALID')
        return binding

    def verify(self,s,step,assertion,*,domain,payload,observation_only=False):
        row=s.scalar(select(Execution).where(Execution.step_id==step.step_id))
        if row is None:raise ValueError('RUNTIME_ADMISSION_REQUIRED')
        binding=self.binding(row)
        if observation_only:
            record=self._worker(row.worker_id);admission=None
        else:record,admission=self.admission(row.worker_id,s.get(Snapshot,step.snapshot_id))
        if ((not observation_only and (digest(admission)!=row.admission_digest or row.lease_until<=self.r.now()))
            or (binding['input_digest'],binding['gate_epoch'],binding['expected_version'])!=
                (step.input_digest,step.gate_epoch,step.expected_version)):
            raise ValueError('EXECUTION_AUTHORITY_EXPIRED')
        claims=verify_closed_assertion(assertion,keys=record['keys'],schema=WorkerProof,
            issuer=row.worker_id,audience='dal-workflow',now_epoch=int(self.r.now().timestamp()))
        if (claims['domain']!=domain or claims['exp']-claims['iat']>120 or
            claims['step_id']!=step.step_id or claims['execution_id']!=row.execution_id or
            claims['binding_digest']!=row.binding_digest or claims['payload_digest']!=digest(payload)):
            raise ValueError('RESULT_SOURCE_INVALID')
        return row

    def settle(self,row):
        if row.charged_seconds is None:
            import math
            row.charged_seconds=min(row.reserved_seconds,max(0,math.ceil((self.r.now()-row.started_at).total_seconds()))) if row.started_at else 0

    def record_result(self,s,step,assertion,result):
        row=self.verify(s,step,assertion,domain='dal.workflow-result/1.0',payload=result)
        self.settle(row)
        receipt=dict(binding=self.binding(row),result_digest=digest(result),assertion=assertion)
        # Same result may be re-signed after response loss; immutable first receipt
        # survives. Different results for one attempt always fail closed.
        if row.receipt_digest:
            old=self.r._open(Execution,row.execution_id,'sealed_receipt',row.sealed_receipt)
            if old['result_digest']!=digest(result):raise ValueError('RESULT_CONFLICT')
        else:
            row.receipt_digest=digest(receipt)
            row.sealed_receipt=self.r._seal(Execution,row.execution_id,'sealed_receipt',receipt)
        return row
