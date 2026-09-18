"""One v3 workflow attempt per poll, with encrypted local crash markers.

This entrypoint requires new native CLI and deterministic-executor admission.
It does not inherit a legacy report-only gate or accept a fixture as admission.
"""
import json
import os
import hashlib
from dataclasses import asdict,replace
from datetime import datetime
from pathlib import Path
import time
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from personal_agent_core.crypto import KeyRing,KeyEntry
from personal_agent_core.manifest import canonical_json
from personal_agent.api.dal_client import sign_decision
from personal_agent.api.dal_client import _read_bridge_file
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.timeline.driver import validate_result
from personal_agent_dal.worker.supervisor import Supervisor,SupervisorRefusal
from personal_agent_dal.worker.runtime_admission import private_json,validate_admission
from personal_agent_dal.worker.runtime_process import os_boot_id,run_process,stop_registered
from personal_agent_dal.worker.role_adapter import build_plan,parse_events,load_adapter_config,read_final_report
from personal_agent_dal.worker.workflow_inventory import WorkflowInventory
from personal_agent_dal.worker.workflow_executor import RepositoryExecutor
from personal_agent_dal.worker.workflow_process import validate_executor


def load_config(path):
    body=private_json(path)
    required={'schema','identity','supervisor_root','signing_key_file','data_key_file','data_kid',
        'pins','adapter_config_file','admission_file','executor_admission_file','git_pin','sandbox_pin','projects','config_refs'}
    if set(body)!=required or body['schema']!='dal.workflow-worker/1.0':raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
    if body['identity']['boot_id']!=os_boot_id():raise SupervisorRefusal('SIGNER_IDENTITY_STALE')
    if not isinstance(body['projects'],dict) or len(body['projects'])>128:raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
    for project in body['projects'].values():
        if set(project)!={'root','kind','verification_commands'} or project['kind'] not in ('existing','local_new'):
            raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
        if not isinstance(project['verification_commands'],list) or not 1<=len(project['verification_commands'])<=16:
            raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
        for command in project['verification_commands']:
            if (set(command)!={'pin','arguments','timeout_seconds'} or not isinstance(command['arguments'],list)
                or any(not isinstance(a,str) or '\0' in a for a in command['arguments'])
                or type(command['timeout_seconds']) is not int or not 1<=command['timeout_seconds']<=300):
                raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
    return body


class WorkflowWorker:
    def __init__(self,transport,config):
        self.transport,self.config=transport,config
        identity=config['identity']
        self.supervisor=Supervisor(config['supervisor_root'],boot_id=identity['boot_id'],epoch=identity['supervisor_epoch'])
        key=_read_bridge_file(config['data_key_file'],kind='WORKFLOW_DATA_KEY',limit=32)
        if len(key)!=32:raise SupervisorRefusal('WORKFLOW_KEY_INVALID')
        self.inventory=WorkflowInventory(self.supervisor,KeyRing([KeyEntry(kid=config['data_kid'],key=key,state='active')],service='dal-worker'))
        self.key=serialization.load_pem_private_key(_read_bridge_file(config['signing_key_file'],kind='WORKFLOW_SIGNING_KEY',limit=16384),password=None)
        if not isinstance(self.key,ec.EllipticCurvePrivateKey) or not isinstance(self.key.curve,ec.SECP256R1):raise SupervisorRefusal('WORKFLOW_KEY_INVALID')

    def proof(self,binding,domain,payload):
        now=int(time.time())
        return sign_decision(dict(iss=binding['worker_id'],aud='dal-workflow',domain=domain,jti=binding['execution_id'],
            iat=now,exp=now+120,step_id=binding['step_id'],execution_id=binding['execution_id'],
            binding_digest=digest(binding),payload_digest=digest(payload)),key=self.key,kid=self.config['identity']['kid'])

    def _send_result(self,row):
        binding=row['binding']['execution_binding'];result=row['result']
        status=self.transport.workflow_call('status',{'step_id':binding['step_id']})
        if status.get('attempt_id')!=binding['execution_id'] or status.get('binding_digest')!=digest(binding):raise SupervisorRefusal('RESULT_SOURCE_INVALID')
        if status['status']=='completed':
            if status['result_digest']!=digest(result):raise SupervisorRefusal('RESULT_CONFLICT')
            response=dict(status='completed',result_digest=status['result_digest'])
        else:
            response=self.transport.workflow_call('result',dict(step_id=binding['step_id'],attempt_id=binding['execution_id'],
                result=result,assertion=self.proof(binding,'dal.workflow-result/1.0',result)))
        if response.get('status')!='completed' or response.get('result_digest')!=digest(result):raise SupervisorRefusal('RESULT_NOT_ACCEPTED')
        self.inventory.transition(binding['execution_id'],'result_ready','reported',response=response)
        return response

    def poll(self):
        # Local unknown effects always precede new dispatch. No elapsed-time
        # retry can create another execution or hide a still-live process.
        for row in self.inventory.page():
            with self.inventory.owner(row['effective_attempt']):
                if row['state']=='result_ready':return self._send_result(row)
                if row['state'] in ('starting','running','unknown','dispatch_requested','granted'):
                    observation=row['observation'].get('executor_process',row['observation'])
                    stopped=stop_registered(observation,boot_id=self.supervisor.boot_id)
                    no_process_phase=row['binding']['execution_input']['phase']=='project_registration' and not row['observation'].get('executor_launch_started')
                    if row['state'] in ('dispatch_requested','granted') or no_process_phase:
                        stopped=dict(requested=False,forced=False,process_exited=True,reason='NATIVE_LAUNCH_NOT_STARTED')
                    if row['state']!='unknown':self.inventory.transition(row['effective_attempt'],row['state'],'unknown',observation={'reconciliation_stop':stopped})
                    else:self.inventory.observe(row['effective_attempt'],{'reconciliation_stop':stopped})
                    if stopped['process_exited']:
                        binding=row['binding']['execution_binding']
                        from personal_agent_dal.worker.workflow_observer import observe
                        result=observe(self,row)
                        response=self.transport.workflow_call('stop',dict(step_id=binding['step_id'],attempt_id=binding['execution_id'],
                            result=result,assertion=self.proof(binding,'dal.workflow-stop/1.0',result)))
                        if response!={'status':'observed','attempt_id':binding['execution_id']}:raise SupervisorRefusal('STOP_NOT_ACCEPTED')
                        self.inventory.transition(row['effective_attempt'],'unknown','refused',response=response)
                        return {'status':'stopped','attempt_id':binding['execution_id']}
                    raise SupervisorRefusal('EXECUTION_RECONCILIATION_REQUIRED')
                if row['state']=='prepared':return self._execute(row)
        claim=self.transport.workflow_call('claim',{})
        binding=claim.get('binding')
        if binding is None:return {'status':'idle'}
        expected={'schema','owner','step_id','execution_id','input_digest','snapshot_id','snapshot_digest','expected_version',
            'gate_epoch','worker_id','boot_id','supervisor_epoch','lease_id','lease_until','admission_digest'}
        if (not isinstance(binding,dict) or set(binding)!=expected or binding['schema']!='dal.workflow-execution/1.0'
            or binding['owner'].get('kind')!='workflow' or digest(claim['input'])!=binding['input_digest']
            or digest(claim['snapshot'])!=binding['snapshot_digest']
            or any(binding[k]!=self.config['identity'][k] for k in ('worker_id','boot_id','supervisor_epoch'))):
            raise SupervisorRefusal('WORKFLOW_CLAIM_INVALID')
        if claim['status']!='prepared':raise SupervisorRefusal('REMOTE_DISPATCH_WITHOUT_LOCAL_MARKER')
        wf=binding['owner']['workflow_id']
        self.inventory.renew_reservation(wf,binding)
        reservation=self.supervisor.reserve(attempt_id='workflow:'+wf,workspace_id=wf,generation=1,
            authority={'workflow_id':wf},read_roots=[])
        context=dict(owner=binding['owner'],attempt_id=binding['execution_id'],worker_id=binding['worker_id'],
            snapshot=claim['snapshot'],snapshot_sha256=binding['snapshot_digest'],execution_role=claim['input']['role'],
            completion_mode='workflow_result',isolation=None,execution_binding=binding,execution_input=claim['input'])
        row=self.inventory.adopt(context,reservation)
        with self.inventory.owner(binding['execution_id']):return self._execute(row)

    def _execute(self,row):
        context=row['binding'];inputs=context['execution_input'];binding=context['execution_binding'];attempt=binding['execution_id']
        reservation=self.inventory.execution_reservation(attempt)
        if inputs['phase'] in ('coding','fix','stage_commit') and not {'read','write'}<=set(inputs.get('authorization',{}).get('actions',[])):
            raise SupervisorRefusal('PROJECT_AUTHORIZATION_REQUIRED')
        if datetime.fromisoformat(binding['lease_until']).timestamp()-time.time()<15:
            raise SupervisorRefusal('EXECUTION_BUDGET_EXHAUSTED')
        adapter=load_adapter_config(self.config['adapter_config_file'])
        admission=dict(path=self.config['admission_file'],identity=self.config['identity'],pins=self.config['pins'],
            adapters=adapter,config_refs=self.config['config_refs'])
        plan=build_plan(context,reservation,self.config['pins'],adapter)
        admission_sha=validate_admission(admission,context=context,reservation=reservation,plan=plan)
        plan=replace(plan,admission=admission,admission_sha256=admission_sha,production_enabled=True)
        validate_executor(self.config,self.supervisor)
        if plan.final_report_path and os.path.lexists(plan.final_report_path):
            raise SupervisorRefusal('CLI_FINAL_OUTPUT_ALREADY_EXISTS')
        def heartbeat():
            status=self.transport.workflow_call('status',{'step_id':binding['step_id']})
            return (status.get('stop_required') is False and status.get('attempt_id')==attempt
                and status.get('binding_digest')==digest(binding))
        self.inventory.transition(attempt,'prepared','dispatch_requested',observation={'plan':asdict(plan)})
        try:
            receipt=self.transport.workflow_call('prelaunch',dict(step_id=binding['step_id'],
                assertion=self.proof(binding,'dal.workflow-prelaunch/1.0',{'input_digest':binding['input_digest']})))
            if receipt.get('attempt_id')!=attempt or digest(receipt.get('input'))!=binding['input_digest']:
                raise SupervisorRefusal('PRELAUNCH_BINDING_INVALID')
            self.inventory.transition(attempt,'dispatch_requested','granted')
            phase=inputs['phase']
            deterministic=phase in ('project_registration','workspace_prepare','verify','stage_commit','delivery_publication','delivery_prepare','delivery_probe')
            if deterministic:
                self.inventory.transition(attempt,'granted','starting')
                self.inventory.transition(attempt,'starting','running')
                executor=RepositoryExecutor(self.supervisor,reservation,self.config,inputs,inventory=self.inventory,attempt=attempt,heartbeat=heartbeat)
                def publish(payload):
                    while True:
                        if datetime.fromisoformat(binding['lease_until']).timestamp()<=time.time() or not heartbeat():
                            raise SupervisorRefusal('AUTHORITY_LOST')
                        result=self.transport.workflow_call('publication',dict(step_id=binding['step_id'],attempt_id=attempt,result=payload,assertion=self.proof(binding,'dal.workflow-publication/1.0',payload)))
                        if result!={'status':'pending'}:return result
                        time.sleep(1)
                executor.publish=publish
                if not heartbeat():raise SupervisorRefusal('AUTHORITY_LOST')
                if phase=='project_registration':result=executor.registration()
                elif phase=='workspace_prepare':result=executor.prepare()
                elif phase=='verify':result=executor.verify(heartbeat=heartbeat)
                elif phase in ('delivery_publication','delivery_prepare'):result=executor.delivery()
                elif phase=='delivery_probe':result=executor.probe()
                else:result=executor.commit()
            else:
                for directory in plan.task_directories.values():Path(directory).mkdir(mode=0o700,exist_ok=True)
                from personal_agent_dal.worker.workflow_prompt import build_prompt
                prompt=build_prompt(inputs)
                process=run_process(self.inventory,attempt,plan,heartbeat=heartbeat,
                    deadline=int(datetime.fromisoformat(binding['lease_until']).timestamp()),prompt=prompt)
                if process['reason'] or process['exit_code']!=0 or not process['stop']['process_exited']:
                    raise SupervisorRefusal('WORKFLOW_PROVIDER_FAILED')
                final=read_final_report(plan.final_report_path) if plan.final_report_path else None
                parsed=parse_events(process['raw'],plan.runtime,final_report=final)
                self.inventory.observe(attempt,{'provider_stream_sha256':hashlib.sha256(process['raw']).hexdigest(),
                    'provider_progress_messages':parsed.get('progress_messages',[])})
                if parsed['outcome']!='succeeded':raise SupervisorRefusal('WORKFLOW_PROVIDER_FAILED')
                result=json.loads(parsed['report'])
                if phase in ('coding','fix'):
                    if not isinstance(result,dict) or set(result)!={'kind','text'} or result['kind']!='code_report' or not isinstance(result['text'],str):
                        raise SupervisorRefusal('CODER_REPORT_INVALID')
                    executor=RepositoryExecutor(self.supervisor,reservation,self.config,inputs,inventory=self.inventory,attempt=attempt,heartbeat=heartbeat)
                    result=executor.candidate(stage_text=result['text'])
            validate_result(phase,result)
            if not heartbeat():raise SupervisorRefusal('AUTHORITY_LOST')
            self.inventory.transition(attempt,'running','result_ready',result=result)
            return self._send_result(self.inventory.get(attempt))
        except BaseException:
            current=self.inventory.get(attempt)
            if current['state'] in ('dispatch_requested','granted','starting','running'):
                self.inventory.transition(attempt,current['state'],'unknown')
            raise
