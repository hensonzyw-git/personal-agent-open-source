"""Loading the payload-encryption key ring the Finance MCP process holds.

Separate from `server/keys.py`, which loads the *public* ES256 material used to
verify the Agent backend's per-call signature. This ring is symmetric and
secret: it seals operational payloads and duplicate-check candidate ids, which
name real ledger rows.

The material is injected the same way every other secret is -- a systemd
credential in production, an environment variable pointing at a mode-600 file
locally. No key is ever generated on the fly for a real run: a process that
minted its own key would seal rows that the next process could not open, and a
duplicate decision has to survive the gap between asking Henson and his answer.

One key is `active` and does all sealing; retired kids stay loadable so an
interrupted rotation leaves nothing unopenable.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Final

from personal_agent_core.crypto import KeyEntry, KeyRing


SERVICE: Final[str] = "personal_data_mcp"

ACTIVE_KID_ENV: Final[str] = "PERSONAL_DATA_MCP_DATA_ACTIVE_KID"
ACTIVE_KEY_ENV: Final[str] = "PERSONAL_DATA_MCP_DATA_ACTIVE_KEY_PATH"
#: Optional `kid=path` entries, whitespace-separated, kept decrypt-only.
PREVIOUS_ENV: Final[str] = "PERSONAL_DATA_MCP_DATA_PREVIOUS_KEYS"

KEY_BYTES: Final[int] = 32


class DataKeyConfigError(RuntimeError):
    """The payload-encryption key material is missing or unusable."""


def _read_key(path: str) -> bytes:
    """Read one base64 key file, refusing anything that is not a 256-bit key."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DataKeyConfigError(f"cannot read data key at {path}") from exc
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise DataKeyConfigError("data key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        # Failing here rather than at first use: a short key would otherwise
        # surface as an unopenable row long after the mistake was made.
        raise DataKeyConfigError(
            f"data key must be {KEY_BYTES} bytes, got {len(key)}"
        )
    return key


def load_data_keyring(env: dict[str, str] | None = None) -> KeyRing:
    """Build the sealing key ring from configuration, or refuse to run."""
    env = env if env is not None else dict(os.environ)

    active_kid = env.get(ACTIVE_KID_ENV)
    active_path = env.get(ACTIVE_KEY_ENV)
    if not active_kid or not active_path:
        raise DataKeyConfigError(
            "no payload encryption key configured; set "
            f"{ACTIVE_KID_ENV} and {ACTIVE_KEY_ENV}"
        )

    entries = [KeyEntry(kid=active_kid, key=_read_key(active_path), state="active")]
    for item in (env.get(PREVIOUS_ENV) or "").split():
        kid, _, path = item.partition("=")
        if not kid or not path:
            raise DataKeyConfigError(
                f"malformed retired-key entry {item!r}; expected kid=path"
            )
        entries.append(
            KeyEntry(kid=kid, key=_read_key(path), state="decrypt_only")
        )

    return KeyRing(entries, service=SERVICE)
