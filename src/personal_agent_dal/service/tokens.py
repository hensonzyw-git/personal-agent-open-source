"""Closed, signed Worker credentials; legacy tokens carry no registration authority."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Final

from personal_agent_core.manifest import canonical_json

TOKEN_SCHEMA: Final[str] = 'dal.worker-token/1.0'
BOUND_TOKEN_SCHEMA: Final[str] = 'dal.worker-token/2.0'


class TokenError(Exception):
    """A malformed, unverifiable, or expired credential."""


def _sign(payload_b64: str, key: bytes) -> str:
    return hmac.new(key, payload_b64.encode('ascii'), hashlib.sha256).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TokenError('duplicate field')
        result[key] = value
    return result


def token_claims(token: str) -> dict:
    """Parse the closed shape only, without authenticating it (client cache check)."""
    try:
        raw, signature = token.split('.')
        if len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature):
            raise TokenError('malformed signature')
        payload = json.loads(base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True),
                             object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise TokenError('payload shape')
        fields = {'schema','worker_id','capabilities','exp'}
        if payload.get('schema') == BOUND_TOKEN_SCHEMA:
            fields |= {'machine_id','registration_epoch'}
            if (not isinstance(payload.get('machine_id'),str) or not payload['machine_id']
                or type(payload.get('registration_epoch')) is not int or payload['registration_epoch'] < 1):
                raise TokenError('identity shape')
        elif payload.get('schema') != TOKEN_SCHEMA:
            raise TokenError('unknown schema')
        if set(payload) != fields:
            raise TokenError('payload shape')
        caps = payload['capabilities']
        if (not isinstance(payload['worker_id'],str) or not payload['worker_id']
            or type(payload['exp']) is not int or payload['exp'] < 1
            or not isinstance(caps,list)
            or not all(isinstance(c,str) and c for c in caps)
            or caps != sorted(set(caps))):
            raise TokenError('payload shape')
        return payload
    except (ValueError, TypeError, UnicodeError) as exc:
        raise TokenError('malformed token') from exc


def issue_token(*, worker_id: str, capabilities: list[str], expires_at_epoch: int,
                key: bytes, machine_id: str | None = None, registration_epoch: int | None = None) -> str:
    payload = dict(schema=TOKEN_SCHEMA,worker_id=worker_id,capabilities=sorted(capabilities),exp=expires_at_epoch)
    if machine_id is not None or registration_epoch is not None:
        payload.update(schema=BOUND_TOKEN_SCHEMA,machine_id=machine_id,registration_epoch=registration_epoch)
    raw = base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip('=')
    token = f'{raw}.{_sign(raw,key)}'
    token_claims(token)
    return token


def verify_token_claims(token: str, *, key: bytes, now_epoch: int) -> dict:
    payload = token_claims(token)
    raw, signature = token.split('.')
    try:
        valid = hmac.compare_digest(_sign(raw,key),signature)
    except (UnicodeError, ValueError) as exc:
        raise TokenError('bad signature') from exc
    if not valid:
        raise TokenError('bad signature')
    if now_epoch >= payload['exp']:
        raise TokenError('token expired')
    return payload


def verify_token(token: str, *, key: bytes, now_epoch: int) -> tuple[str,list[str]]:
    claims = verify_token_claims(token,key=key,now_epoch=now_epoch)
    return claims['worker_id'],claims['capabilities']
