"""Two OS processes and separate durable stores; synthetic worker, no native CLI.

The only injected component is an in-memory synthetic admission registry. No
native acceptance evidence is manufactured or installed in service files.
"""
import json
import multiprocessing
import socket
import sqlite3
import time
from pathlib import Path

import httpx
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.dal.test_timeline_composition import configuration
from tests.dal.test_timeline_requests import world
from tests.dal.test_timeline_transport import request
from tests.dal.test_service_api import _enroll, _post, _auth, SERVICE_KEY, ENROLLMENT_SECRET
from personal_agent.api.dal_client import sign_decision
from personal_agent_dal.timeline.requests import digest


def serve(database, configuration_path, public_pem, admission_time, sock):
    from sqlalchemy import select
    from personal_agent_dal.service.app import create_app
    from personal_agent_dal.storage.engine import create_database_engine
    from personal_agent_dal.storage.timeline_models import RoleConfigurationBinding
    from personal_agent_dal.timeline import config as composition
    from personal_agent_dal.timeline.roles import RoleService
    from tests.dal.test_timeline_roles import config, registry
    original=composition.load_endpoint
    def synthetic_endpoint(*args,**kwargs):
        endpoint=original(*args,**kwargs)
        body=config();snapshot=dict(body,digest=digest(body),source='system')
        endpoint.roles=RoleService(endpoint.requests,registry(body))
        revision=endpoint.roles.register(body)
        with endpoint.requests.sessions() as session:
            bound=session.scalar(select(RoleConfigurationBinding)) is not None
        if not bound:endpoint.roles.bind(scope='system',scope_id='default',revision_id=revision,expected_version=0)
        endpoint.execution_authority.registry['worker-1']=dict(
            keys={'test':serialization.load_pem_public_key(public_pem)},
            admission=dict(schema='dal.workflow-admission/1.0',worker_id='worker-1',boot_id='synthetic-boot',
                supervisor_epoch=1,snapshot_digest=digest(snapshot),issued_at=admission_time-1,
                expires_at=admission_time+1800,revoked=False,evidence_digest='a'*64))
        endpoint.roles.availability=endpoint.execution_authority.available
        return endpoint
    composition.load_endpoint=synthetic_endpoint
    engine=create_database_engine(Path(database))
    app=create_app(engine,service_key=SERVICE_KEY,enrollment_secret=ENROLLMENT_SECRET,timeline_config_path=Path(configuration_path))
    uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='on')).run(sockets=[sock])


def test_lifespan_prepares_signed_worker_attempt_and_restart_replays_result(world,tmp_path):
    config,pa=configuration(tmp_path)
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    context=multiprocessing.get_context('spawn');started=int(time.time());process=None
    def start():
        child=context.Process(target=serve,args=(world[0].url.database,str(config),public,started,sock))
        child.start()
        for _ in range(150):
            assert child.is_alive(), 'service child failed'
            try:
                if httpx.get(base+'/health',timeout=.2,trust_env=False).status_code==200:return child
            except httpx.HTTPError:pass
            time.sleep(.02)
        child.terminate();child.join(5)
        raise AssertionError('service startup deadline')
    def proof(binding,domain,payload):
        now=int(time.time())
        return sign_decision(dict(iss='worker-1',aud='dal-workflow',domain=domain,jti='socket-test',iat=now,exp=now+120,
            step_id=binding['step_id'],execution_id=binding['execution_id'],binding_digest=digest(binding),payload_digest=digest(payload)),key=private,kid='test')
    try:
        process=start()
        with httpx.Client(base_url=base,trust_env=False,timeout=5) as client:
            token=_enroll(client)
            submitted=client.post('/internal/development/commands',json=request(pa));assert submitted.status_code==200
            for _ in range(100):
                claim=_post(client,'/workflow/claim',{},_auth(token));assert claim.status_code==200
                if claim.json().get('binding'):break
                time.sleep(.1)
            item=claim.json();binding=item['binding'];assert binding is not None
            launch=dict(step_id=binding['step_id'],assertion=proof(binding,'dal.workflow-prelaunch/1.0',{'input_digest':binding['input_digest']}))
            assert _post(client,'/workflow/prelaunch',launch,_auth(token)).status_code==200
            result=dict(kind='clarification',text='Synthetic offline result',ready=False,questions=['Clarify scope'],acceptance=[])
            body=dict(step_id=binding['step_id'],attempt_id=binding['execution_id'],result=result,assertion=proof(binding,'dal.workflow-result/1.0',result))
            # Separate synthetic Worker store persists before upload and survives restart.
            with sqlite3.connect(tmp_path/'worker.db') as local:
                local.execute('CREATE TABLE pending (body TEXT NOT NULL)')
                local.execute('INSERT INTO pending VALUES (?)',(json.dumps(body),))
            first=_post(client,'/workflow/result',body,_auth(token));assert first.status_code==200, first.text
            process.terminate();process.join(5);assert not process.is_alive()
            process=start()
            with sqlite3.connect(tmp_path/'worker.db') as local:
                replay=json.loads(local.execute('SELECT body FROM pending').fetchone()[0])
            repeated=_post(client,'/workflow/result',replay,_auth(token))
            assert repeated.status_code==200,repeated.text
            assert repeated.json()['result_digest']==first.json()['result_digest']==digest(result)
            replay['result']['text']='tampered'
            assert _post(client,'/workflow/result',replay,_auth(token)).status_code==409
    finally:
        if process is not None and process.is_alive():process.terminate();process.join(5)
        sock.close()
