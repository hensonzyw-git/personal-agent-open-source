"""Seal and open the resolved request an operation was authorised to make.

Design 3.3 (review R1-F4). `intent.py` seals a `WriteIntent` onto the
*api_request* row, which is right for a duplicate decision: the turn is parked,
nothing was written, and `write anyway` resumes it minutes later from the same
request. A calendar override cannot use that column. The original write has
already happened and settled -- the phone's report is what says the event was a
duplicate -- and `api_requests.encrypted_request_payload` holds the chat request
the turn replays, so writing an intent there would cost the request its own
idempotent replay.

So the intent gets its own column on the operation: written in the same
committed transition that issues the device action, and *retained* after
settlement. That retention is the whole point. The device-action seal beside it
is cleared the moment the operation leaves `source_in_progress`, and the
duplicate is only learned afterwards, so a seal with the action's lifetime would
be gone exactly when the override needs it.

Sealed under the same rules as every other `encrypted_*` column: the arguments
name the user's calendar, title and time, and binding the envelope to
`operation_id` as additional data means a ciphertext lifted onto another
operation fails to open rather than re-issuing one turn's write under another's
authority.

Nothing here decides *whether* an override is allowed -- that is the endpoint's
section of design 3.3. This module only keeps the raw material honest.
"""

from __future__ import annotations

import json
from typing import Any

from personal_agent.api.intent import WriteIntent
from personal_agent_core.crypto import CryptoError, KeyRing
from personal_agent_core.manifest import canonical_json

_TABLE = "operations"
_COLUMN = "encrypted_request"


class OperationRequestError(ValueError):
    """A retained request could not be opened, or opened to the wrong shape.

    Raised to the caller rather than swallowed: unlike the device-action
    projection, there is no fail-closed *delivery* here to fall back on. An
    override that cannot read the request it is supposed to resume has nothing
    to resume, and inventing one would be exactly the re-derivation design 3.3
    exists to prevent.
    """


def seal_operation_request(
    keyring: KeyRing, *, operation_id: str, intent: WriteIntent
) -> dict[str, Any]:
    """Seal the authorised tool call onto its operation row."""
    plaintext = canonical_json(
        {"tool": intent.tool, "model_args": intent.model_args}
    ).encode("utf-8")
    return keyring.encrypt(
        plaintext, table=_TABLE, column=_COLUMN, row_id=operation_id
    )


def open_operation_request(
    keyring: KeyRing, *, operation_id: str, envelope: dict[str, Any]
) -> WriteIntent:
    """Open the request sealed for this operation, or raise.

    The shape check is deliberately strict. These arguments are about to be
    re-authorised and re-dispatched as a *new* write, so anything the seal
    cannot attest -- a non-string tool, arguments that are not an object -- has
    to stop here rather than reach the dispatcher as a plausible-looking call.
    """
    try:
        plaintext = keyring.decrypt(
            envelope, table=_TABLE, column=_COLUMN, row_id=operation_id
        )
    except (CryptoError, ValueError, TypeError) as exc:
        raise OperationRequestError(
            f"the retained request for {operation_id} would not open"
        ) from exc
    try:
        data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperationRequestError(
            f"the retained request for {operation_id} is not JSON"
        ) from exc
    if not isinstance(data, dict):
        raise OperationRequestError(
            f"the retained request for {operation_id} is not an object"
        )
    tool = data.get("tool")
    model_args = data.get("model_args")
    if not isinstance(tool, str) or not tool:
        raise OperationRequestError(
            f"the retained request for {operation_id} names no tool"
        )
    if not isinstance(model_args, dict):
        raise OperationRequestError(
            f"the retained request for {operation_id} carries no arguments"
        )
    return WriteIntent(tool=tool, model_args=model_args)
