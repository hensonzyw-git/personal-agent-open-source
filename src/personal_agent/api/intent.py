"""The resolved write intent, sealed so a decision can resume it later.

A duplicate decision or a clarification is answered by a *new* operation, often
minutes later (design 5.2.1). For `write anyway` to re-issue the exact same write
without re-invoking the model, the resolved intent -- the tool and its model
arguments -- is persisted, sealed, on the parked operation's `api_request` and
copied onto the new operation. It is personal data, so it is never stored in the
clear: it is AEAD-sealed under the Agent key ring, with the envelope bound by AAD
to the exact row that holds it, so a sealed intent cannot be lifted to another
request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from personal_agent_core.crypto import KeyRing
from personal_agent_core.manifest import canonical_json


_TABLE = "api_requests"
_COLUMN = "encrypted_request_payload"


@dataclass(frozen=True)
class WriteIntent:
    """A fully resolved tool call, ready to dispatch to Finance MCP.

    `model_args` are exactly the model-facing arguments (never Host-injected
    fields); the override that authorises writing past a duplicate travels
    separately, as a Host value bound to the operation, and is never sealed into
    the model's arguments.
    """

    tool: str
    model_args: dict[str, Any]


def seal_intent(
    keyring: KeyRing, *, request_id: str, intent: WriteIntent
) -> dict[str, Any]:
    """Seal a write intent for storage on `request_id`'s api_request row."""
    plaintext = canonical_json(
        {"tool": intent.tool, "model_args": intent.model_args}
    ).encode("utf-8")
    return keyring.encrypt(
        plaintext, table=_TABLE, column=_COLUMN, row_id=request_id
    )


def open_intent(
    keyring: KeyRing, *, request_id: str, envelope: dict[str, Any]
) -> WriteIntent:
    """Open a sealed write intent bound to `request_id`'s api_request row."""
    plaintext = keyring.decrypt(
        envelope, table=_TABLE, column=_COLUMN, row_id=request_id
    )
    data = json.loads(plaintext.decode("utf-8"))
    return WriteIntent(tool=data["tool"], model_args=data["model_args"])
