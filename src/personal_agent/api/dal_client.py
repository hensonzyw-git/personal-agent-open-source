"""Dedicated, closed ES256 assertions and fixed-destination resume transport."""
import json
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest
from pydantic import StrictInt
from typing import Literal, Annotated
from pydantic import Field


class LegacyDecisionClaims(Closed):
    """Closed pre-upgrade shape, usable only for historical import replay."""
    iss: Id
    aud: Id
    jti: Id
    iat: StrictInt
    exp: StrictInt
    decision_id: Id
    device_id: Id
    subject_id: Id
    key_thumbprint: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$")]
    decision: Literal['approve_once', 'approve_once_accept_duplicate_cost', 'reject']
    binding_sha256: Digest


class DecisionClaims(LegacyDecisionClaims):
    proposal_id: Id


def sign_decision(claims, *, key, kid):
    return jwt.encode(claims, key, algorithm='ES256', headers={'kid': kid, 'typ': 'JWT'})


def verify_closed_assertion(assertion, *, keys, now_epoch, schema, issuer=None, audience=None):
    try:
        if not isinstance(assertion, str) or len(assertion)>16384:
            raise ValueError('ASSERTION_SIZE_INVALID')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('ASSERTION_DUPLICATE_FIELD')
                result[key] = value
            return result
        from personal_agent.auth.device_keys import b64u_decode
        parts = assertion.split('.')
        if len(parts)!=3: raise ValueError('ASSERTION_INVALID')
        for part in parts[:2]:
            json.loads(b64u_decode(part), object_pairs_hook=unique_object)
        header = jwt.get_unverified_header(assertion)
        if set(header) != {'alg','kid','typ'} or header['alg'] != 'ES256' or header['typ'] != 'JWT':
            raise ValueError('ASSERTION_HEADER_INVALID')
        key = keys[header['kid']]
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError('ASSERTION_KEY_INVALID')
        claims = jwt.decode(assertion, key, algorithms=['ES256'], options={
            'verify_exp': False, 'verify_iat': False, 'verify_nbf': False,
            'verify_aud': False, 'verify_iss': False})
        body = schema.model_validate(claims).model_dump(by_alias=True)
        if issuer is not None and (body['iss'] != issuer or body['aud'] != audience):
            raise ValueError('ASSERTION_DESTINATION_INVALID')
        issued, expires = (body['iat'],body['exp']) if 'iat' in body else (body['issued_at'],body['expires_at'])
        if not 0 <= issued <= now_epoch < expires or not 0 < expires-issued <= 900:
            raise ValueError('ASSERTION_TIME_INVALID')
        return body
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('ASSERTION_INVALID') from exc


def verify_decision(assertion, *, keys, issuer, audience, now_epoch, legacy_replay=False):
    body = verify_closed_assertion(assertion, keys=keys, issuer=issuer, audience=audience,
                                   now_epoch=now_epoch, schema=LegacyDecisionClaims if legacy_replay else DecisionClaims)
    if body['subject_id'] != 'device:'+body['device_id'] or len(body['key_thumbprint']) != 43:
        raise ValueError('ASSERTION_IDENTITY_INVALID')
    return body


class ProposalBridgeClaims(Closed):
    iss: Id
    aud: Id
    jti: Id
    iat: StrictInt
    exp: StrictInt
    operation: Literal['resume-proposal']
    request_id: Id
    feature_id: Id
    selection_id: Id


class FixedDalTransport:
    def __init__(self, *, base_url, key, kid, issuer, audience):
        from urllib.parse import urlsplit
        # Reject raw control/space spellings before urlsplit can normalize them.
        if not isinstance(base_url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in base_url):
            raise ValueError('DAL_DESTINATION_INVALID')
        parsed=urlsplit(base_url)
        if base_url != 'http://127.0.0.1:8820' and (
                parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('','/')):
            raise ValueError('DAL_DESTINATION_INVALID')
        self.base_url=base_url.rstrip('/')
        self.key,self.kid,self.issuer,self.audience=key,kid,issuer,audience

    def _post(self,path,body):
        import httpx
        # No ambient proxy, redirects, caller URL or provider credentials.
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=10) as client:
            with client.stream('POST',self.base_url+path,json=body) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > 65536:
                        raise ValueError('DAL_RESPONSE_TOO_LARGE')
                    content.extend(chunk)
                return json.loads(content)

    def propose(self,body):
        import time
        from personal_agent_core.ids import new_id
        now=int(time.time())
        claims=dict(iss=self.issuer,aud=self.audience,jti=new_id(),iat=now,exp=now+60,
                    operation='resume-proposal',**body)
        return self._post('/internal/resume-proposals',{'assertion':sign_decision(claims,key=self.key,kid=self.kid)})

    def deliver(self,assertion):
        return self._post('/internal/human-decisions',{'assertion':assertion})


def _read_bridge_file(path, *, kind, limit=65536):
    """Use the operator reader's single-fd pattern, with bounded nonblocking IO."""
    import os
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
                raise ValueError
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, min(8192, limit + 1 - size))
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise ValueError
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise ValueError(f'DAL_RESUME_{kind}_FILE_INVALID') from None


def load_bridge(path, *, session_factory, token_ring):
    """Opt-in owner-only configuration, with a dedicated P-256 key file."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from personal_agent.api.dal_resume import ResumeBridge
    config=json.loads(_read_bridge_file(path,kind='CONFIG'))
    if set(config)!={'base_url','signing_key_file','kid','issuer','audience'}:
        raise ValueError('DAL_RESUME_CONFIG_INVALID')
    key=load_pem_private_key(_read_bridge_file(config.pop('signing_key_file'),kind='KEY',limit=16384),password=None)
    if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):
        raise ValueError('DAL_RESUME_KEY_INVALID')
    if any(key.public_key().public_numbers()==registered.public_numbers() for registered in token_ring.verification_keys()):
        raise ValueError('DAL_RESUME_KEY_NOT_DEDICATED')
    validate_trust_ids(config['issuer'], config['audience'], [config['kid']])
    transport=FixedDalTransport(key=key,**config)
    return ResumeBridge(session_factory=session_factory,transport=transport,key=key,kid=config['kid'],
                        issuer=config['issuer'],audience=config['audience'])


def validate_trust_ids(issuer, audience, kids):
    from pydantic import TypeAdapter
    adapter = TypeAdapter(Id)
    try:
        adapter.validate_python(issuer)
        adapter.validate_python(audience)
        if not kids: raise ValueError
        for kid in kids: adapter.validate_python(kid)
    except (ValueError, TypeError):
        raise ValueError('DAL_RESUME_TRUST_IDS_INVALID') from None
