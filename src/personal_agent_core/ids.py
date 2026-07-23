"""Identifier helpers.

The technical design fixes canonical lowercase UUID strings for `device_id`,
`challenge_id`, request and trace identifiers, and reuses one client generated
UUIDv4 as the Finance idempotency key and the Feishu `client_token`. A model
supplied identifier is never trusted, so parsing is strict.
"""

from __future__ import annotations

import uuid


class InvalidIdentifierError(ValueError):
    """A value is not a canonical UUID string."""


def new_id() -> str:
    """Generate a canonical lowercase UUIDv4 string."""
    return str(uuid.uuid4())


def parse_uuid(value: object) -> uuid.UUID:
    """Parse a canonical UUID string, rejecting braces, urns and uppercase."""
    if not isinstance(value, str):
        raise InvalidIdentifierError(
            f"expected a UUID string, got {type(value).__name__}"
        )
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise InvalidIdentifierError(f"not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise InvalidIdentifierError(
            f"UUID must be canonical lowercase and unbraced: {value!r}"
        )
    return parsed


def is_uuid4(value: object) -> bool:
    """Whether a value is a canonical UUIDv4 string."""
    try:
        return parse_uuid(value).version == 4
    except InvalidIdentifierError:
        return False


def require_uuid4(value: object) -> str:
    """Return a canonical UUIDv4 string or raise.

    Write idempotency keys must be version 4 so that a caller cannot derive a
    predictable key for someone else's request.
    """
    parsed = parse_uuid(value)
    if parsed.version != 4:
        raise InvalidIdentifierError(
            f"expected a UUIDv4, got version {parsed.version}"
        )
    return str(parsed)
