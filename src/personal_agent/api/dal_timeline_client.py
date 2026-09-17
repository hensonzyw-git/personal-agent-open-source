"""Fixed destination and exact signed-response binding for the Timeline bridge."""
import json
import httpx
from personal_agent.api.dal_client import FixedDalTransport
from personal_agent_dal.timeline.transport import envelope, verify_envelope, _unique


class TimelineTransport(FixedDalTransport):
    def __init__(self, *, base_url, key, kid, trusted_keys):
        super().__init__(base_url=base_url,key=key,kid=kid,issuer='pa-timeline',audience='dal-timeline')
        self.trusted_keys=trusted_keys

    def _post(self,path,body):
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=10) as client:
            with client.stream('POST',self.base_url+path,json=body) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError('DAL_UNAVAILABLE')
                raw=bytearray()
                for chunk in response.iter_bytes():
                    if len(raw)+len(chunk)>1024*1024:raise ValueError('DAL_RESPONSE_INVALID')
                    raw.extend(chunk)
                try:return json.loads(raw,object_pairs_hook=_unique)
                except (ValueError,UnicodeError,RecursionError):raise ValueError('DAL_RESPONSE_INVALID') from None

    def call(self, *, operation, request_id, subject, body):
        if operation not in ('submit','request_detail','request_list'):raise ValueError('INVALID_ARGUMENT')
        scope='dal.request' if operation=='submit' else 'dal.read'
        request=envelope(key=self.key,kid=self.kid,issuer=self.issuer,audience=self.audience,
            operation=operation,request_id=request_id,subject=subject,scope=scope,body=body)
        response=self._post('/internal/development/commands' if operation=='submit' else '/internal/development/query',request)
        try:
            claims,result=verify_envelope(response,keys=self.trusted_keys,issuer='dal-timeline',audience='pa-timeline')
            from personal_agent_dal.timeline.requests import digest
            if claims['request_body_sha256']!=digest(body):raise ValueError
            if any(claims[k]!=v for k,v in dict(operation=operation,request_id=request_id,subject=subject,scope=scope).items()):
                raise ValueError
            if operation=='submit':
                if set(result)!={'schema_version','command_id','receipt_id','status','workflow_version','request'}:raise ValueError
                if result['schema_version']!='dal.timeline/1.0' or result['status']!='accepted' or result['command_id']!=request_id:raise ValueError
                from personal_agent_dal.timeline.requests import valid_id, digest
                if result['receipt_id']!='receipt:'+digest(request_id):raise ValueError
                receipt=result['request']
                if set(receipt)!={'schema_version','request_id','version','status','feature_id'}:raise ValueError
                if receipt['schema_version']!='dal.timeline/1.0' or receipt['status']!='accepted_not_started' or receipt['feature_id'] is not None:raise ValueError
                valid_id(receipt['request_id'])
                if type(receipt['version']) is not int or receipt['version']<1 or result['workflow_version']!=receipt['version']:raise ValueError
            return result
        except (ValueError,TypeError,KeyError):raise ValueError('DAL_RESPONSE_INVALID') from None


def load_bridge(path, *, session_factory, keyring, token_ring):
    from personal_agent.api.dal_client import _read_bridge_file, validate_trust_ids
    from personal_agent.api.dal_timeline import TimelineBridge
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    config=json.loads(_read_bridge_file(path,kind='TIMELINE_CONFIG'),object_pairs_hook=_unique)
    if set(config)!={'base_url','signing_key_file','kid','dal_public_keys'}:
        raise ValueError('TIMELINE_CONFIG_INVALID')
    key=serialization.load_pem_private_key(_read_bridge_file(config['signing_key_file'],kind='TIMELINE_KEY',limit=16384),password=None)
    if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):raise ValueError('TIMELINE_KEY_INVALID')
    if any(key.public_key().public_numbers()==k.public_numbers() for k in token_ring.verification_keys()):
        raise ValueError('TIMELINE_KEY_NOT_DEDICATED')
    if not isinstance(config['dal_public_keys'],dict):raise ValueError('TIMELINE_TRUST_INVALID')
    validate_trust_ids('pa-timeline','dal-timeline',[config['kid'],*config['dal_public_keys']])
    keys={}
    for kid,path in config['dal_public_keys'].items():
        public=serialization.load_pem_public_key(_read_bridge_file(path,kind='TIMELINE_PUBLIC_KEY',limit=16384))
        if not isinstance(public,ec.EllipticCurvePublicKey) or not isinstance(public.curve,ec.SECP256R1):raise ValueError('TIMELINE_TRUST_INVALID')
        if public.public_numbers()==key.public_key().public_numbers():raise ValueError('TIMELINE_KEY_NOT_DEDICATED')
        keys[kid]=public
    if not keys:raise ValueError('TIMELINE_TRUST_INVALID')
    transport=TimelineTransport(base_url=config['base_url'],key=key,kid=config['kid'],trusted_keys=keys)
    return TimelineBridge(session_factory=session_factory,keyring=keyring,transport=transport)
