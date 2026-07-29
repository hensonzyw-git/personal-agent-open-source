"""One framing primitive for every model-visible untrusted data block.

The tags are prompt structure rather than a security boundary by themselves,
so source text must never be able to forge either marker.  Keeping the
neutralisation here prevents auxiliary model calls from drifting away from the
Context Builder's framing rules.
"""

from __future__ import annotations

import re
from typing import Final

from personal_agent_core.errors import AppError, ErrorCode


UNTRUSTED_OPEN: Final[str] = '<untrusted_data kind="{kind}" ref="{ref}">'
UNTRUSTED_OPEN_WITHOUT_REF: Final[str] = '<untrusted_data kind="{kind}">'
UNTRUSTED_CLOSE: Final[str] = "</untrusted_data>"

_FORGERY_PATTERNS: Final[tuple[str, ...]] = (
    "<untrusted_data",
    "</untrusted_data>",
)
_FRAME_ATTRIBUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_.:-]{1,160}"
)


def frame_untrusted_data(kind: str, ref: str | None, body: str) -> str:
    """Wrap recorded content while neutralising forged frame delimiters."""
    attributes = (("kind", kind),)
    if ref is not None:
        attributes += (("ref", ref),)
    for label, value in attributes:
        if (
            not isinstance(value, str)
            or _FRAME_ATTRIBUTE_RE.fullmatch(value) is None
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail=f"untrusted frame {label} is malformed",
            )
    if not isinstance(body, str):
        raise AppError(
            ErrorCode.CONTEXT_UNAVAILABLE,
            internal_detail="untrusted frame body is malformed",
        )
    safe = body
    for pattern in _FORGERY_PATTERNS:
        safe = safe.replace(pattern, pattern.replace("<", "﹤"))
    opening = (
        UNTRUSTED_OPEN.format(kind=kind, ref=ref)
        if ref is not None
        else UNTRUSTED_OPEN_WITHOUT_REF.format(kind=kind)
    )
    return "\n".join((opening, safe, UNTRUSTED_CLOSE))
