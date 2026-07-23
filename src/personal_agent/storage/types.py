"""Column types that refuse to store an ambiguous value.

SQLite has no native timestamp or boolean, and it will happily accept whatever
Python hands it. Both of the failures that would cost the most here are quiet
ones: a naive datetime that silently means "some local time", and an amount or
payload written as plaintext into a column the design says is encrypted. These
types turn both into errors at write time.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Text, TypeDecorator

from personal_agent_core.timeutil import parse_rfc3339, to_rfc3339


class UtcTimestamp(TypeDecorator[datetime]):
    """An aware UTC instant stored as RFC 3339 text.

    Text keeps backups and restore drills readable without a driver, and sorts
    correctly because every value is normalised to UTC with a `Z` suffix.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Any
    ) -> str | None:
        if value is None:
            return None
        return to_rfc3339(value)

    def process_result_value(
        self, value: str | None, dialect: Any
    ) -> datetime | None:
        if value is None:
            return None
        return parse_rfc3339(value)


class EncryptedEnvelope(TypeDecorator[dict[str, Any]]):
    """An AES-256-GCM envelope, per technical design 8.5.

    DEV-007 owns the cipher. This type owns the shape, and refuses anything that
    is not a sealed envelope, so a column named `encrypted_*` cannot end up
    holding plaintext while the crypto layer is still being built.
    """

    impl = Text
    cache_ok = True

    REQUIRED_KEYS = frozenset({"v", "kid", "nonce", "ciphertext", "tag"})

    def process_bind_param(
        self, value: dict[str, Any] | None, dialect: Any
    ) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(
                "encrypted columns take a sealed envelope, not a plain value"
            )
        missing = self.REQUIRED_KEYS.difference(value)
        if missing:
            raise ValueError(
                f"envelope is missing {sorted(missing)}; "
                "refusing to store an unsealed value"
            )
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def process_result_value(
        self, value: str | None, dialect: Any
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return json.loads(value)
