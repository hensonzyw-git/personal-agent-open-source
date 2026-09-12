"""Seal and open the device action an operation was issued, on its own row.

Review R6, 2026-09-08: the chat response used to be the device action's only
delivery channel, so a request that timed out at 202 handed the client an
operation id and an action nobody would ever deliver. The seal on
`operations.encrypted_device_action` closes that hole: the orchestrator writes
it in the same committed transition that parks the operation at
`source_in_progress`, and the operation projection becomes the one delivery
door -- the 200 reply, the by-id poll and a replay all converge on the same
parked-state read, while a settled operation refuses to hand the action over.

Sealed for the same reason as `encrypted_result_record`: the event fields are
the user's personal schedule and the model-authorised intent itself, and no
column named `encrypted_*` may hold plaintext while the crypto layer exists.

Bound to `operation_id` as additional data, like every other sealed column
here, so a ciphertext lifted onto another operation fails to open rather than
handing one write's action to a different turn.
"""

from __future__ import annotations

import json
from typing import Any

from personal_agent_core.crypto import CryptoError, KeyRing
from personal_agent_core.manifest import canonical_json

_TABLE = "operations"
_COLUMN = "encrypted_device_action"


class DeviceActionProjectionError(ValueError):
    """A sealed action could not be opened, or opened to the wrong shape.

    Raised internally; the projection catches it and omits the field. The
    failure mode is silence-with-sweep, never a guessed action.
    """


def seal_device_action(
    keyring: KeyRing, *, operation_id: str, action: dict[str, Any]
) -> dict[str, Any]:
    """Seal the authorised device action onto its operation row."""
    return keyring.encrypt(
        canonical_json(action).encode("utf-8"),
        table=_TABLE,
        column=_COLUMN,
        row_id=operation_id,
    )


def open_device_action(
    keyring: KeyRing, *, operation_id: str, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    """Open a sealed device action, or `None` when it will not open cleanly.

    `None` is the fail-closed direction and is safe for the same reason the
    absent field is: the operation stays parked at `source_in_progress`, the
    client gets no action, and the timeout sweep remains the witness. It never
    raises on a read path that is otherwise a plain projection, and it never
    reconstructs an action the envelope will not attest.
    """
    try:
        plaintext = keyring.decrypt(
            envelope, table=_TABLE, column=_COLUMN, row_id=operation_id
        )
    except (CryptoError, ValueError, TypeError):
        return None
    try:
        action = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not _well_formed(action):
        return None
    return action


def _well_formed(action: object) -> bool:
    """The minimal shape a hand-off may travel in.

    Not the full schema -- the authorised arguments were schema-validated
    before the action was issued, and the seal is their attestation. This
    checks only that the envelope opened to a dict of exactly the wire keys,
    so a tampered or misdirected plaintext cannot become a hand-off.
    """
    if not isinstance(action, dict):
        return False
    if set(action) != {"action_id", "tool", "wire_version", "event"}:
        return False
    event = action["event"]
    if not isinstance(event, dict):
        return False
    if not isinstance(action["action_id"], str) or not action["action_id"]:
        return False
    if not isinstance(action["tool"], str) or not action["tool"]:
        return False
    # `wire_version` says what the action's *fields* mean, so an unreadable one
    # is not a degraded hand-off, it is an unreadable action: the delivery gate
    # compares it against the client's own version, and a missing or nonsense
    # value would compare as "no requirement" and hand a v2 action to a v1
    # client -- the write-wrong-calendar path this field exists to close.
    if not isinstance(action["wire_version"], int) or isinstance(
        action["wire_version"], bool
    ):
        return False
    return action["wire_version"] >= 1
