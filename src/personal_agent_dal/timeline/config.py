"""Opt-in service composition; all material is read from protected local files."""
import json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.api.dal_client import _read_bridge_file
from personal_agent_core.crypto import KeyEntry,KeyRing
from personal_agent_dal.timeline.requests import RequestService
from personal_agent_dal.timeline.transport import TimelineEndpoint


def load_endpoint(path,*,engine,kill_switch=lambda:False):
    config=json.loads(_read_bridge_file(path,kind='TIMELINE_CONFIG'))
    if set(config)-{'role_registry_file','execution_registry_file'}!={'schema_version','data_key_file','data_kid','cursor_key_file','signing_key_file','kid','pa_public_keys'} or config['schema_version']!='dal.timeline-service/1.0':
        raise ValueError('TIMELINE_CONFIG_INVALID')
    data=_read_bridge_file(config['data_key_file'],kind='TIMELINE_DATA_KEY',limit=32)
    cursor=_read_bridge_file(config['cursor_key_file'],kind='TIMELINE_CURSOR_KEY',limit=32)
    if len(data)!=32 or len(cursor)!=32 or data==cursor:
        raise ValueError('TIMELINE_KEYS_INVALID')
    private=serialization.load_pem_private_key(_read_bridge_file(config['signing_key_file'],kind='TIMELINE_SIGNING_KEY',limit=16384),password=None)
    if not isinstance(private,ec.EllipticCurvePrivateKey) or not isinstance(private.curve,ec.SECP256R1):
        raise ValueError('TIMELINE_KEYS_INVALID')
    if not isinstance(config['pa_public_keys'],dict) or not config['pa_public_keys']:
        raise ValueError('TIMELINE_TRUST_INVALID')
    keys={}
    for kid,key_path in config['pa_public_keys'].items():
        key=serialization.load_pem_public_key(_read_bridge_file(key_path,kind='TIMELINE_PUBLIC_KEY',limit=16384))
        if not isinstance(key,ec.EllipticCurvePublicKey) or not isinstance(key.curve,ec.SECP256R1):
            raise ValueError('TIMELINE_TRUST_INVALID')
        if key.public_numbers()==private.public_key().public_numbers():
            raise ValueError('TIMELINE_KEYS_NOT_SEPARATE')
        keys[kid]=key
    requests=RequestService(engine,keyring=KeyRing([KeyEntry(kid=config['data_kid'],key=data,state='active')],service='dal'),cursor_key=cursor)
    endpoint=TimelineEndpoint(requests,trusted_keys=keys,signing_key=private,kid=config['kid'],kill_switch=kill_switch)
    if config.get('role_registry_file'):
        from personal_agent_dal.timeline.roles import RoleService
        registry=json.loads(_read_bridge_file(config['role_registry_file'],kind='ROLE_REGISTRY'))
        if not isinstance(registry,list) or not registry or len(registry)>128:raise ValueError('ROLE_REGISTRY_INVALID')
        endpoint.roles=RoleService(requests,registry)
    from personal_agent_dal.timeline.execution import ExecutionAuthority
    execution_registry={}
    if config.get('execution_registry_file'):
        registry=json.loads(_read_bridge_file(config['execution_registry_file'],kind='WORKFLOW_REGISTRY'))
        if not isinstance(registry,dict) or len(registry)>32:raise ValueError('WORKFLOW_REGISTRY_INVALID')
        for worker_id,path in registry.items():
            def read_record(path=path,worker_id=worker_id):
                record=json.loads(_read_bridge_file(path,kind='WORKFLOW_ADMISSION'))
                if set(record)!={'admission','evidence','public_key_file','kid'}:raise ValueError('WORKFLOW_ADMISSION_INVALID')
                from personal_agent_dal.timeline.requests import digest
                evidence=record['evidence']
                if (digest(evidence)!=record['admission']['evidence_digest']
                    or evidence.get('schema')!='dal.runtime-admission/3.0'
                    or evidence.get('scope')!='timeline-workflow-v3'
                    or evidence.get('provenance')!='operator-attested-external-native-cli'
                    or evidence.get('identity',{}).get('worker_id')!=worker_id
                    or evidence.get('identity',{}).get('boot_id')!=record['admission']['boot_id']
                    or evidence.get('identity',{}).get('supervisor_epoch')!=record['admission']['supervisor_epoch']):
                    raise ValueError('WORKFLOW_ADMISSION_INVALID')
                key=serialization.load_pem_public_key(_read_bridge_file(record['public_key_file'],kind='WORKFLOW_WORKER_KEY',limit=16384))
                if not isinstance(key,ec.EllipticCurvePublicKey) or not isinstance(key.curve,ec.SECP256R1):raise ValueError('WORKFLOW_TRUST_INVALID')
                return dict(admission=record['admission'],keys={record['kid']:key})
            read_record()
            execution_registry[worker_id]=read_record
    endpoint.execution_authority=ExecutionAuthority(requests,execution_registry)
    endpoint.roles.availability=endpoint.execution_authority.available
    return endpoint
