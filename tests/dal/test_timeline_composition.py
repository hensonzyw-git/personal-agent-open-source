"""Real service configuration, permissions and local socket acceptance."""
import json
import threading
import socket
import time

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.dal.test_timeline_requests import world
from tests.dal.test_timeline_transport import request
from personal_agent_dal.service.app import create_app


def protected(path,body):
    path.write_bytes(body)
    path.chmod(0o600)
    return str(path)


def configuration(tmp_path):
    pa,dal=ec.generate_private_key(ec.SECP256R1()),ec.generate_private_key(ec.SECP256R1())
    body=dict(schema_version='dal.timeline-service/1.0',data_kid='test',kid='dal',
        data_key_file=protected(tmp_path/'data',b'd'*32),cursor_key_file=protected(tmp_path/'cursor',b'c'*32),
        signing_key_file=protected(tmp_path/'signer',dal.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),
        pa_public_keys={'pa':protected(tmp_path/'pa-public',pa.public_key().public_bytes(
            serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))})
    path=tmp_path/'timeline-config.json'
    protected(path,json.dumps(body).encode())
    return path,pa


def test_service_socket_has_real_signature_gate_and_persistent_replay(world,tmp_path):
    engine,_,_=world
    config,pa=configuration(tmp_path)
    app=create_app(engine,service_key=b's'*32,enrollment_secret=b'e'*32,timeline_config_path=config)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
            assert client.post('/internal/development/commands',json={}).status_code==403
            body=request(pa)
            first=client.post('/internal/development/commands',json=body)
            assert first.status_code==200
            # Simulate an unknown first response by resending the identical command.
            repeated=client.post('/internal/development/commands',json=body)
            assert first.json()['body']==repeated.json()['body']
    finally:
        server.should_exit=True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()


def test_bad_file_permissions_fail_before_service_serves(world,tmp_path):
    config,_=configuration(tmp_path)
    config.chmod(0o644)
    with pytest.raises(ValueError):
        create_app(world[0],service_key=b's'*32,enrollment_secret=b'e'*32,timeline_config_path=config)


def test_service_kill_switch_refuses_intake(world,tmp_path):
    from fastapi.testclient import TestClient
    config,pa=configuration(tmp_path)
    stop=tmp_path/'stop';stop.touch()
    app=create_app(world[0],service_key=b's'*32,enrollment_secret=b'e'*32,
        timeline_config_path=config,kill_switch_path=stop)
    assert TestClient(app).post('/internal/development/commands',json=request(pa)).status_code==503
