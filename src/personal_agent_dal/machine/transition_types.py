"""Transition-level shared types: the receipt codes and the refusal exception.

`ReceiptCodes` and `TransitionRefused` are used both by the engine's write-set
appliers and by the hash-binding validators (`machine.binding`), which live in
separate modules. Keeping them here rather than in `engine.py` breaks the
otherwise-circular import `engine -> binding -> engine` while leaving exactly
one definition of each code — a refusal code spelled in two places would drift
apart and a replay or audit would mis-classify it.
"""

from __future__ import annotations

from typing import Any, Final


class ReceiptCodes:
    """Transition receipt codes. Only `APPLIED` is ever persisted."""

    APPLIED: Final[str] = "APPLIED"
    POLICY_DENIED: Final[str] = "POLICY_DENIED"
    ILLEGAL_TRANSITION: Final[str] = "ILLEGAL_TRANSITION"
    TERMINAL_STATE: Final[str] = "TERMINAL_STATE"
    VERSION_CONFLICT: Final[str] = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT: Final[str] = "IDEMPOTENCY_CONFLICT"
    #: An approval is invalid for consumption: already consumed, revoked, or
    #: expired. Distinct from POLICY_DENIED (an actor/evidence refusal) so a
    #: replay or audit can tell "the approval was bad" from "the caller was
    #: not allowed to ask".
    APPROVAL_INVALID: Final[str] = "APPROVAL_INVALID"
    #: The decision the command names is stale: its version does not match
    #: the server's current version, or it has expired. The approval may be
    #: valid, but acting on a stale decision would bind the wrong state.
    DECISION_STALE: Final[str] = "DECISION_STALE"


class TransitionRefused(Exception):
    """A refusal carrying the receipt code to report. Never partially applied."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        latest_projection: dict[str, Any] | None = None,
        validation_stage: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.latest_projection = latest_projection
        self.validation_stage = validation_stage
        super().__init__(f"{code}: {detail}")
