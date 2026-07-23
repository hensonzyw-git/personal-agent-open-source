"""The second authorisation check, run inside the Finance MCP process.

Loopback is not an authorisation boundary. The Agent backend already authorised
the device and signed a per-call Host Context, but the technical design (4.3) is
explicit that Finance MCP re-verifies rather than trusting that a request
arrived on `127.0.0.1`: it holds only the verification public key, recomputes
the canonical argument hash from what it actually received, and compares the
signed claims to the request field by field.

The one invariant this module exists to guarantee is ordering: **no execution
record is created before verification passes.** A rejected call must leave no
trace of having been half accepted, so the gate runs before the handler, and a
handler is the only thing that touches the execution store. A test proves a
rejected call leaves the execution table empty, because "it raised" is not the
same claim as "it wrote nothing".

The header names are the ones the governed bridge sends for a Streamable HTTP
call. `_meta` is not read here: over HTTP the context travels in headers, and
accepting a second copy from the body would reintroduce the duplicate channel
that was deliberately removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import ServiceKeyRing, verify_host_context
from personal_agent_core.manifest import load_manifest


AUTHORIZATION_HEADER = "authorization"
REQUEST_ID_HEADER = "x-request-id"
IDEMPOTENCY_HEADER = "idempotency-key"
TRACEPARENT_HEADER = "traceparent"
USER_ID_HEADER = "x-user-id"
TIMEZONE_HEADER = "x-timezone"

_BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class VerifiedCall:
    """The identity a verified call carries forward into execution."""

    tool: str
    idempotency_key: str
    request_id: str
    trace_id: str
    user_id: str
    device_id: str
    timezone: str
    scopes: tuple[str, ...]
    allowed_tools_version: str


class Authorizer:
    """Verifies the signed Host Context and the tool's scope."""

    def __init__(
        self,
        verification_ring: ServiceKeyRing,
        *,
        allowed_tools_version: str | None = None,
    ) -> None:
        # A ring that still holds private material would let this process mint
        # tokens as well as verify them; the design keeps the two apart.
        self._ring = verification_ring.public_only()
        self._allowed_tools_version = (
            allowed_tools_version
            if allowed_tools_version is not None
            else load_manifest()["allowed_tools_version"]
        )

    def _bearer(self, headers: dict[str, str]) -> str:
        raw = headers.get(AUTHORIZATION_HEADER, "")
        if not raw.startswith(_BEARER_PREFIX):
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail="missing or malformed Authorization header",
            )
        token = raw[len(_BEARER_PREFIX) :].strip()
        if not token:
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail="empty bearer token",
            )
        return token

    def _required_header(self, headers: dict[str, str], name: str) -> str:
        value = headers.get(name)
        if not value:
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail=f"missing {name} header",
            )
        return value

    def authorize(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        headers: dict[str, str],
        required_scopes: tuple[str, ...],
        now=None,
    ) -> VerifiedCall:
        """Verify one call, or raise before anything downstream runs.

        `headers` are lower-cased. `arguments` are the model-visible arguments
        exactly as received; the hash is recomputed from them and any Host-only
        field a model tried to smuggle in is stripped inside `verify_host_context`.
        """
        token = self._bearer(headers)
        idempotency_key = self._required_header(headers, IDEMPOTENCY_HEADER)
        request_id = self._required_header(headers, REQUEST_ID_HEADER)
        trace_id = self._required_header(headers, TRACEPARENT_HEADER)
        user_id = self._required_header(headers, USER_ID_HEADER)
        timezone = self._required_header(headers, TIMEZONE_HEADER)

        claims = verify_host_context(
            self._ring,
            token,
            tool=tool,
            idempotency_key=idempotency_key,
            request_id=request_id,
            user_id=user_id,
            trace_id=trace_id,
            timezone=timezone,
            arguments=arguments,
            now=now,
        )

        granted = frozenset(claims.get("scopes") or [])
        missing = set(required_scopes) - granted
        if missing:
            # The scope check is separate from the binding check so a caller
            # that presents a valid token for a tool it lacks the scope for is
            # told SCOPE_DENIED, not HOST_CONTEXT_MISMATCH.
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail=f"{tool} requires scopes not granted to the device",
            )
        if claims["allowed_tools_version"] != self._allowed_tools_version:
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail=(
                    f"{tool} carries a stale allowed_tools_version"
                ),
            )

        return VerifiedCall(
            tool=tool,
            idempotency_key=idempotency_key,
            request_id=request_id,
            trace_id=trace_id,
            user_id=user_id,
            device_id=str(claims["device_id"]),
            timezone=timezone,
            scopes=tuple(sorted(granted)),
            allowed_tools_version=str(claims["allowed_tools_version"]),
        )
