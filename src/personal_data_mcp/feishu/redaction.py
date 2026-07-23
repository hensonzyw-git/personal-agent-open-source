"""Scrub secrets and resource identifiers out of anything that gets logged.

The acceptance for the connector is that logs carry no secret and no resource
identifier (technical design 8.4). The adapter never logs bodies or tokens in
the first place, but any diagnostic string it does build passes through here, so
a Base token, table id, field id, record id, app secret or bearer token cannot
reach a log line even by accident.

The patterns match Feishu's identifier prefixes. This is redaction for logs, not
a claim that these values are cryptographically secret.
"""

from __future__ import annotations

import re
from typing import Final


_PLACEHOLDER: Final[str] = "«redacted»"

# app secret / tokens carried in an Authorization header or a JSON field.
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._-]+")
_SECRET_FIELD = re.compile(
    r'("?(?:app_secret|tenant_access_token|token|app_id)"?\s*[:=]\s*"?)'
    r"[A-Za-z0-9._-]+",
    re.IGNORECASE,
)
# Feishu resource id prefixes: bascn.../bascb... base tokens, tbl..., fld...,
# rec..., opt... .
_RESOURCE_ID = re.compile(r"\b(?:bas[a-z]?[A-Za-z0-9]{6,}|(?:tbl|fld|rec|opt)[A-Za-z0-9]{6,})\b")


def redact_for_log(text: str) -> str:
    """Return `text` with secrets and resource ids masked."""
    text = _BEARER.sub(f"Bearer {_PLACEHOLDER}", text)
    text = _SECRET_FIELD.sub(rf"\1{_PLACEHOLDER}", text)
    text = _RESOURCE_ID.sub(_PLACEHOLDER, text)
    return text
