"""Strict loading for the independent Finance-query cursor credential."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping


CURSOR_SECRET_ENV = "PERSONAL_DATA_MCP_QUERY_CURSOR_SECRET"
MIN_CURSOR_SECRET_BYTES = 32
_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class QueryCursorSecretError(ValueError):
    """The configured cursor credential is present but cannot be trusted."""


def load_query_cursor_secret(
    env: Mapping[str, str] | None = None, *, required: bool = True
) -> bytes | None:
    """Load one base64url cursor secret, optionally leaving query disabled.

    Missing is distinct from malformed: production composition may keep the
    query tool unadvertised when no key has been provisioned, but a present
    typo must fail startup rather than silently rotate every outstanding cursor.
    """
    values = env if env is not None else os.environ
    configured = values.get(CURSOR_SECRET_ENV)
    if configured is None or configured == "":
        if required:
            raise QueryCursorSecretError(
                f"set {CURSOR_SECRET_ENV} to the query cursor signing key "
                "(base64url, at least 32 bytes once decoded)"
            )
        return None
    if configured != configured.strip():
        raise QueryCursorSecretError(
            f"{CURSOR_SECRET_ENV} must not contain surrounding whitespace"
        )
    raw = configured
    unpadded = raw.rstrip("=")
    if (
        not unpadded
        or "=" in unpadded
        or not set(unpadded) <= _BASE64URL_ALPHABET
    ):
        raise QueryCursorSecretError(
            f"{CURSOR_SECRET_ENV} is not valid base64url"
        )
    try:
        secret = base64.b64decode(
            raw + "=" * (-len(raw) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise QueryCursorSecretError(
            f"{CURSOR_SECRET_ENV} is not valid base64url"
        ) from error
    canonical = base64.urlsafe_b64encode(secret).decode("ascii")
    if raw not in {canonical, canonical.rstrip("=")}:
        raise QueryCursorSecretError(
            f"{CURSOR_SECRET_ENV} is not canonical base64url"
        )
    if len(secret) < MIN_CURSOR_SECRET_BYTES:
        raise QueryCursorSecretError(
            f"{CURSOR_SECRET_ENV} decoded to {len(secret)} bytes; at least "
            f"{MIN_CURSOR_SECRET_BYTES} are required"
        )
    return secret
