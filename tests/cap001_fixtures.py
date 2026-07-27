"""Shared `CAP-001` test material: the two new HMAC key purposes.

Design 5.6 keeps the cursor signer, the identifier/lineage HMAC and the data key
as separate material, and `load_cursor_key` refuses two purposes sharing one
secret. Tests therefore need distinct bytes per purpose, not one convenient
constant, or they would prove key separation against a fixture that does not
have it.
"""

from __future__ import annotations

from personal_agent.keys import HmacKey


CURSOR_KEY = HmacKey(kid="cursor:test-1", secret=b"\x11" * 32)
IDENTIFIER_KEY = HmacKey(kid="identifier:test-1", secret=b"\x22" * 32)
