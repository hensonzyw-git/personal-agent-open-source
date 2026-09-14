"""Isolated acceptance composition: real enrollment/auth and denied writes."""
from pathlib import Path
from contextlib import contextmanager
import pytest


def test_refuses_unmarked_existing_directory(tmp_path):
    from personal_agent.acceptance import initialize
    (tmp_path / 'keep').write_text('existing')
    with pytest.raises(ValueError):
        initialize(tmp_path, '127.0.0.1', 8843)
    assert (tmp_path / 'keep').read_text() == 'existing'


def test_refuses_public_bind_and_symlink(tmp_path):
    from personal_agent.acceptance import initialize
    with pytest.raises(ValueError):
        initialize(tmp_path / 'public', '0.0.0.0', 8843)
    target = tmp_path / 'target'; target.mkdir()
    link = tmp_path / 'link'; link.symlink_to(target)
    with pytest.raises(ValueError):
        initialize(link, '127.0.0.1', 8843)


def test_requires_existing_complete_state(tmp_path):
    from personal_agent.acceptance import build_acceptance
    with pytest.raises((ValueError, FileNotFoundError)):
        build_acceptance(tmp_path)


@contextmanager
def real_https(app, root):
    import socket, ssl, threading, time, uvicorn, httpx
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, ssl_keyfile=str(root/'tls.key'), ssl_certfile=str(root/'tls.crt'), access_log=False, log_level='critical'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+5
        while not server.started and time.monotonic()<deadline: time.sleep(.01)
        assert server.started
        context = ssl.create_default_context(cafile=str(root/'tls.crt'))
        with httpx.Client(base_url=f'https://127.0.0.1:{port}', verify=context, trust_env=False, timeout=35) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def enroll(client, app):
    from personal_agent.acceptance import now
    from personal_agent.auth.enrollment import create_enrollment_code
    from personal_agent.auth.device_keys import encode_device_public_key, build_signing_input, der_to_jose, b64u_encode
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    deps = app.state.acceptance_deps
    with deps.session_factory() as s:
        code = create_enrollment_code(s, now=now()); s.commit()
    key = ec.generate_private_key(ec.SECP256R1())
    response = client.post('/v1/enrollments/claim', json={'code': code.code, 'public_key': encode_device_public_key(key.public_key()), 'display_name': 'synthetic'})
    assert response.status_code == 201, response.text
    device = response.json()['device_id']
    challenge = client.post('/v1/auth/challenges', json={'device_id': device}).json()
    signature = b64u_encode(der_to_jose(key.sign(build_signing_input(challenge_id=challenge['challenge_id'], nonce_b64u=challenge['nonce'], device_id=device), ec.ECDSA(hashes.SHA256()))))
    response = client.post('/v1/auth/tokens', json={'device_id': device, 'challenge_id': challenge['challenge_id'], 'nonce': challenge['nonce'], 'signature': signature})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer '+response.json()['access_token'], 'X-Client-Wire-Version': '4'}, device


def test_real_auth_and_fixed_sdk_scenarios(tmp_path):
    from fastapi.testclient import TestClient
    from personal_agent.acceptance import initialize, build_acceptance, SCOPES
    from personal_agent.storage.models import Device
    import json, uuid
    root = tmp_path/'local'; initialize(root, '127.0.0.1', 8843)
    app = build_acceptance(root)
    with real_https(app, root) as client:
        assert client.get('/v1/capabilities').status_code == 401
        headers, device = enroll(client, app)
        deps = app.state.acceptance_deps
        with deps.session_factory() as s:
            assert set(json.loads(s.get(Device, device).scopes)) == set(SCOPES)
        caps = client.get('/v1/capabilities', headers=headers)
        assert caps.status_code == 200, caps.text
        conversation = caps.json()['conversation_id']
        for text, expected in [('你好','conversation'), ('查询','analysis'), ('比较','analysis'), ('澄清','clarification')]:
            response = client.post('/v1/chat/messages', headers={**headers, 'Idempotency-Key': str(uuid.uuid4())}, json={'conversation_id': conversation, 'text': text})
            assert response.status_code in (200, 202), response.text
            result = response.json()
            assert result.get('result_envelope', {}).get('kind') == expected, json.dumps(result, ensure_ascii=False)
        for text in ('空响应', '畸形参数', '夹带正文', '提供方错误', '越权写入', '只读失败'):
            response = client.post('/v1/chat/messages', headers={**headers, 'Idempotency-Key': str(uuid.uuid4())}, json={'conversation_id': conversation, 'text': text})
            result = response.json()
            assert result.get('result_envelope', {}).get('failure'), (text, result)
            assert result.get('record_id') is None, result
    app.state.acceptance_engine.dispose()


def test_restart_replays_idempotent_result_without_new_run(tmp_path):
    from personal_agent.acceptance import initialize, build_acceptance
    from personal_agent.storage.models import Operation
    from sqlalchemy import select, func
    import uuid
    root = tmp_path/'restart'; initialize(root, '127.0.0.1', 8843)
    app = build_acceptance(root)
    key = str(uuid.uuid4())
    with real_https(app, root) as client:
        headers, _ = enroll(client, app)
        conversation = client.get('/v1/capabilities', headers=headers).json()['conversation_id']
        payload = {'conversation_id': conversation, 'text': '比较'}
        first = client.post('/v1/chat/messages', headers={**headers, 'Idempotency-Key': key}, json=payload).json()
        assert first['result_envelope']['kind'] == 'analysis'
    app.state.acceptance_engine.dispose()
    reopened = build_acceptance(root)
    with real_https(reopened, root) as client:
        replay = client.post('/v1/chat/messages', headers={**headers, 'Idempotency-Key': key}, json=payload).json()
        assert replay['operation_id'] == first['operation_id']
        assert replay['result_envelope'] == first['result_envelope']
        with reopened.state.acceptance_deps.session_factory() as s:
            assert s.scalar(select(func.count()).select_from(Operation)) == 1
    reopened.state.acceptance_engine.dispose()


@pytest.mark.parametrize('field,value', [('host','0.0.0.0'), ('host','127.0.0.2'), ('port',443), ('certificate_sha256','0'*64)])
def test_tampered_bind_or_certificate_is_rejected(tmp_path, field, value):
    import json
    from personal_agent.acceptance import initialize, settings
    root = tmp_path/'bind'; initialize(root, '127.0.0.1', 8843)
    file = root/'acceptance.json'; config = json.loads(file.read_text()); config[field] = value
    file.write_text(json.dumps(config))
    with pytest.raises(ValueError): settings(root)


def test_invalid_signature_and_expired_token_refused(tmp_path):
    from personal_agent.acceptance import initialize, build_acceptance, now
    from personal_agent.auth.tokens import issue_access_token
    from datetime import timedelta
    from personal_agent.storage.models import Device
    root = tmp_path/'auth'; initialize(root, '127.0.0.1', 8843)
    app = build_acceptance(root)
    with real_https(app, root) as client:
        headers, device_id = enroll(client, app)
        challenge = client.post('/v1/auth/challenges', json={'device_id': device_id}).json()
        reply = client.post('/v1/auth/tokens', json={'device_id': device_id, 'challenge_id': challenge['challenge_id'], 'nonce': challenge['nonce'], 'signature': 'A'*86})
        assert reply.status_code >= 400
        corrupted = dict(headers); corrupted['Authorization'] += 'invalid'
        assert client.get('/v1/capabilities', headers=corrupted).status_code == 401
        # Real signature with an old issued-at, not an unsigned dummy token.
        deps = app.state.acceptance_deps
        with deps.session_factory() as s:
            device = s.get(Device, device_id)
            token = issue_access_token(deps.token_ring, device_id=device_id, device_key_thumbprint=device.device_key_thumbprint, scopes=['meta.capabilities.read'], allowed_tools_version=device.allowed_tools_version, now=now()-timedelta(hours=1))
        expired = {'Authorization': 'Bearer '+token}
        assert client.get('/v1/capabilities', headers=expired).status_code == 401
    app.state.acceptance_engine.dispose()


def test_generated_certificate_declares_tls_server_usage(tmp_path):
    from personal_agent.acceptance import initialize
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    root = tmp_path/'certificate'; initialize(root, '127.0.0.1', 8843)
    cert = x509.load_pem_x509_certificate((root/'tls.crt').read_bytes())
    assert ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
