"""Local-only ADK acceptance. No production configuration or connector loading.

Run `python -m personal_agent.acceptance --help`. All model responses are fixed,
through the real SDK witness/Runner. This does not evaluate real model semantics.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import stat

# This isolated process uses bundled pricing metadata. No SDK startup fetch.
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from sqlalchemy import event, select

from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.keys import HmacKey
from personal_agent.api.composition import device_authorization
from personal_agent.api.finance_query_projection import decode_finance_query_projection
from personal_agent.api.orchestrator import ReadCompleted, ResolveFailedSafe
from personal_agent.auth.enrollment import create_enrollment_code, encode_device_scopes
from personal_agent.auth.tokens import SigningKey, TokenKeyRing
from personal_agent.context.builder import ContextBuilder
from personal_agent.context.compactor import Compactor
from personal_agent.context.config import default_context_config
from personal_agent.mcp_client.registry import ConnectorRegistry, TrustLevel
from personal_agent.policy.bridge import GovernedToolBridge
from personal_agent.runtime.prompt import build_system_prompt
from personal_agent.runtime.witnessed_model import WitnessedLiteLlm
from personal_agent.storage.engine import create_all, create_database_engine, session_factory
from personal_agent.storage.models import Device
from personal_agent_core.crypto import KeyRing, KeyEntry
from personal_agent_core.manifest import load_manifest
from personal_agent_core.write_switch import WriteSwitch
from mcp.types import Tool

VERSION = 'adk-device-synthetic-v1'
READ = 'finance.query_expenses'
SCOPES = ('device.self.read', 'device.self.revoke', 'meta.capabilities.read', 'finance.expense.read')
SCENARIOS = ('你好', '澄清', '查询', '比较', '只读失败', '空响应', '畸形参数', '夹带正文', '提供方错误', '越权写入')


def now():
    return datetime.now(timezone.utc)


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        meta = os.fstat(f.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077:
            raise ValueError('acceptance file must be private regular file')
        return f.read()


def initialize(root: Path, host: str, port: int):
    address = ipaddress.ip_address(host)
    if not (address.is_private or address.is_loopback) or address.is_unspecified or address.is_multicast or address.version != 4:
        raise ValueError('explicit private IPv4 address required')
    if not 1024 <= port <= 65535:
        raise ValueError('unprivileged port required')
    if root.is_symlink() or root.exists():
        raise ValueError('initialization requires a new directory')
    root.mkdir(mode=0o700, parents=True)
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ADK synthetic acceptance')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now()-timedelta(minutes=5))
            .not_valid_after(now()+timedelta(days=7)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(private, hashes.SHA256()))
    _write(root/'tls.key', pem)
    _write(root/'tls.crt', cert.public_bytes(serialization.Encoding.PEM))
    token = ec.generate_private_key(ec.SECP256R1())
    _write(root/'token.key', token.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _write(root/'keys.json', json.dumps({k: base64.b64encode(os.urandom(32)).decode() for k in ('data', 'identifier', 'cursor')}).encode())
    config = {'version': VERSION, 'host': host, 'port': port, 'certificate_sha256': cert.fingerprint(hashes.SHA256()).hex()}
    _write(root/'acceptance.json', json.dumps(config).encode())
    engine = create_database_engine(root/'agent.sqlite')
    create_all(engine); engine.dispose()
    os.chmod(root/'agent.sqlite', 0o600)
    return config


def settings(root):
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('private marked directory required')
    config = json.loads(_read(root/'acceptance.json'))
    if config['version'] != VERSION:
        raise ValueError('fixture version mismatch')
    address = ipaddress.ip_address(config['host'])
    if address.version != 4 or address.is_unspecified or address.is_multicast or not (address.is_loopback or address.is_private):
        raise ValueError('invalid acceptance bind')
    if type(config['port']) is not int or not 1024 <= config['port'] <= 65535:
        raise ValueError('invalid acceptance port')
    cert = x509.load_pem_x509_certificate(_read(root/'tls.crt'))
    if cert.fingerprint(hashes.SHA256()).hex() != config['certificate_sha256']:
        raise ValueError('certificate fingerprint mismatch')
    if address not in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress):
        raise ValueError('certificate address mismatch')
    if not cert.not_valid_before_utc <= now() < cert.not_valid_after_utc:
        raise ValueError('acceptance certificate expired')
    _read(root/'agent.sqlite')
    return config


def call(name, ident, **args):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def response_for(context, operation_id):
    """Selection comes from the durable original input and recovered evidence.

    No mutable process queue, global scenario index or wall-clock selection.
    Retrying the same durable state emits the same business/tool identities.
    """
    text = context['current_input']
    meta = {'goal': text, 'source_refs': [context['current_user_source_ref']], 'constraints': []}
    refs = context.get('tool_evidence_refs', [])
    task = context.get('bound_task') or meta
    answer = {'kind': 'conversation', 'text': '合成数据验收：你好。', 'coverage': 'complete', 'evidence_refs': []}
    if text == '澄清':
        answer.update(kind='clarification', text='合成数据验收：需要哪个月份？')
    if text in {'查询', '比较', '只读失败'}:
        if not refs:
            dates = [{'start': '2026-09-01', 'end': '2026-09-30'}]
            if text == '比较':
                dates += [{'start': '2026-08-01', 'end': '2026-08-31'}]
                meta['comparisons'] = [{'metric_kind': 'total', 'current': {'date_range': dates[0]}, 'baseline': {'date_range': dates[1]}, 'source_refs': meta['source_refs']}]
            return [call('finance_query_expenses', 'query_'+str(i), task=meta, arguments={'view': 'total', 'date_range': date,
                     **({'name_contains': ['fixture_failure']} if text == '只读失败' else {})}) for i, date in enumerate(dates)]
        metrics = [m for m in context['metrics'] if m['metric_kind'] == 'total']
        nodes = [{'kind': 'metric', 'metric_ref': metrics[0]['metric_ref']}]
        if text == '比较':
            by_ref = {m['evidence_ref']: m for m in metrics}
            nodes = [{'kind': 'comparison', 'comparison_ref': task['comparisons'][0]['comparison_ref'],
                      'current_metric_ref': by_ref['query_query_0']['metric_ref'], 'baseline_metric_ref': by_ref['query_query_1']['metric_ref']}]
        answer = {'kind': 'analysis', 'analysis_nodes': nodes, 'commentary': '合成场景说明，非真实账本。', 'coverage': 'complete', 'evidence_refs': refs}
    if text == '越权写入':
        return [call('finance_log_income', 'denied', task=meta, arguments={})]
    return [call('agent_finish', 'finish', task=task, answer=answer)]


def model_factory(prepared):
    async def transport(request):
        if str(request.url) != 'https://open.bigmodel.cn/api/paas/v4/chat/completions':
            raise ValueError('unexpected SDK endpoint')
        body = json.loads(request.content)
        current = [m['content'] for m in body['messages'] if m['role'] == 'user'][-1]
        context = json.loads(current.split('\n', 1)[1])['task_context']
        text = context['current_input']
        if text == '提供方错误':
            return httpx.Response(503, json={'error': {'message': 'synthetic unavailable'}})
        calls = response_for(context, prepared.binding.request_id)
        content = None
        if text == '空响应': calls = []
        if text == '畸形参数': calls[0]['function']['arguments'] = '{'
        if text == '夹带正文': content = 'synthetic mixed prose'
        return httpx.Response(200, json={'id': 'synthetic-'+prepared.binding.request_id, 'created': 1,
            'object': 'chat.completion', 'model': 'synthetic',
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30},
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': content, 'tool_calls': calls}}]})
    return WitnessedLiteLlm(model='openai/synthetic', provider_name='zhipu', api_key='synthetic-no-credential',
                           binding=prepared.binding, transport=httpx.MockTransport(transport))


class DisabledInterpreter:
    def interpret(self, *, envelope):
        raise RuntimeError('acceptance requires wire v4 and the enabled local device')


def build_acceptance(root: Path):
    config = settings(root)
    keys = {k: base64.b64decode(v, validate=True) for k, v in json.loads(_read(root/'keys.json')).items()}
    identifier_key = HmacKey(kid='acceptance-identifier', secret=keys['identifier'])
    cursor_key = HmacKey(kid='acceptance-cursor', secret=keys['cursor'])
    private = serialization.load_pem_private_key(_read(root/'token.key'), password=None)
    ring = TokenKeyRing(active=SigningKey('acceptance', private, private.public_key()))
    keyring = KeyRing([KeyEntry('acceptance', keys['data'], 'active')], service='personal-agent')
    engine = create_database_engine(root/'agent.sqlite')
    sessions = session_factory(engine)
    manifest = load_manifest()
    contract = next(t for t in manifest['tools'] if t['name'] == READ)
    registry = ConnectorRegistry()
    registry.refresh('synthetic', trust_level=TrustLevel.PERSONAL_DATA,
                     discovered=[Tool(name=READ, description='合成数据验收查询', input_schema=contract['model_input_schema'])])
    bridge = GovernedToolBridge(registry, global_allowlist=frozenset({READ}), write_switch=WriteSwitch(root/'writes-disabled.json'))
    context_config = default_context_config()
    builder = ContextBuilder(context_config, compactor=Compactor(context_config))

    @event.listens_for(sessions, 'before_flush')
    def restrict_new_devices(session, *_):
        for obj in session.new:
            if isinstance(obj, Device):
                obj.scopes = encode_device_scopes(SCOPES)

    def device_for(auth):
        with sessions() as s:
            return device_authorization(s, auth.device_id, enabled_tools=frozenset({READ}), manifest_version=manifest['allowed_tools_version'])

    def authorize_for(auth):
        def authorize(*, tool, model_args):
            device = device_for(auth)
            if device is None:
                raise ValueError('unknown acceptance device')
            return bridge.authorize(tool, model_args, device)[1]
        return authorize

    def envelope(session, auth, *, conversation_id, session_id, current_event_id, user_text,
                 clarification_context, finance_retry_context, input_parts=()):
        device = device_for(auth)
        tools = bridge.visible_tools(device) if device else []
        if input_parts:
            raise ValueError('acceptance text only')
        return builder.build(session, keyring, identifier_key, conversation_id=conversation_id,
            session_id=session_id, current_event_id=current_event_id, user_text=user_text,
            system_instruction=build_system_prompt(today='2026-09-14', runtime_v2=True), effective_tools=tools,
            essential_tools=tuple(t.alias for t in tools), clarification_context=clarification_context,
            finance_retry_context=finance_retry_context)

    class SyntheticRead:
        def __init__(self, auth): self.auth = auth
        def resolve(self, *, tool, model_args, idempotency_key=None):
            args = authorize_for(self.auth)(tool=tool, model_args=model_args)
            if args.get('name_contains') == ['fixture_failure']:
                from personal_agent_core.errors import ErrorCode
                return ResolveFailedSafe(ErrorCode.INTERNAL_ERROR)
            dates = args.get('date_range')
            if dates not in ({'start': '2026-09-01', 'end': '2026-09-30'}, {'start': '2026-08-01', 'end': '2026-08-31'}):
                raise ValueError('outside synthetic fixture')
            filters = dict(date_range=dates, categories=[], name_contains=[], is_family_expense='all', personal_amount_cny=None)
            data = {'status': 'ok', 'view': 'total', 'filters_applied': filters, 'metric': 'personal_spend_total_cny',
                'record_count': 3, 'personal_spend_total_cny': '120.00' if dates['start']=='2026-09-01' else '100.00',
                'source_system': 'feishu_bitable', 'evidence': {'kind': 'aggregate_query', 'query_id': 'synthetic-'+dates['start'],
                'config_checksum': VERSION, 'schema_snapshot_checksum': 'synthetic', 'scanned_pages': 1, 'matched_count': 3,
                'started_at': '2026-09-14T00:00:00Z', 'completed_at': '2026-09-14T00:00:01Z'}}
            return ReadCompleted(result='合成验收数据', projection=decode_finance_query_projection(data))
        def commit(self, *, intent, idempotency_key, duplicate_override):
            raise RuntimeError('acceptance business writes forbidden')

    deps = AgentApiDeps(session_factory=sessions, token_ring=ring, keyring=keyring,
        identifier_key=identifier_key, cursor_key=cursor_key, now=now,
        build_interpreter=lambda auth: DisabledInterpreter(), build_envelope=envelope,
        build_dispatcher=lambda auth, trace: SyntheticRead(auth), build_authorizer=authorize_for,
        capabilities=lambda auth: [{'alias': t.alias, 'description': t.description, 'risk_level': t.risk_level,
             'required_scopes': list(t.required_scopes)} for t in bridge.visible_tools(device_for(auth))],
        enrollment_manifest_version=manifest['allowed_tools_version'], v2_model_factory=model_factory)
    app = build_app(deps)

    @app.middleware('http')
    async def local_devices(request, call_next):
        with sessions() as s:
            deps.v2_device_ids = frozenset(s.scalars(select(Device.device_id).where(Device.status=='active')))
        return await call_next(request)
    app.state.acceptance_deps = deps
    app.state.acceptance_engine = engine
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'code', 'serve'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8843)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'init':
        print(json.dumps(initialize(args.directory, args.host, args.port)))
        return
    config = settings(args.directory)
    if args.command == 'code':
        engine = create_database_engine(args.directory/'agent.sqlite')
        with session_factory(engine)() as s:
            code = create_enrollment_code(s, now=now()); s.commit()
            print(code.code)
        engine.dispose()
        return
    import uvicorn
    app = build_acceptance(args.directory)
    print(json.dumps({'mode': 'fixed-responses', 'fixture_version': VERSION,
        'entrypoint_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'manifest_version': load_manifest()['allowed_tools_version']}), flush=True)
    uvicorn.run(app, host=config['host'], port=config['port'], ssl_keyfile=str(args.directory/'tls.key'),
                ssl_certfile=str(args.directory/'tls.crt'), access_log=False)


if __name__ == '__main__':
    main()
