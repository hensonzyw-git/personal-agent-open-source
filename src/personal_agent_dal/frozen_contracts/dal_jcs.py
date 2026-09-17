#!/usr/bin/env python3
"""Small dependency-free RFC 8785 JSON Canonicalization Scheme encoder.

The DAL documentation tools need stable cross-language hashes before the runtime
exists.  Python's ``json.dumps(sort_keys=True)`` is not sufficient: RFC 8785
orders object keys by UTF-16 code units and uses ECMAScript number spelling.
This module implements those rules and rejects values outside I-JSON.
"""

from __future__ import annotations

import json
import math


MAX_SAFE_INTEGER = 2**53 - 1


class JCSCanonicalizationError(ValueError):
    """The supplied value cannot be represented as RFC 8785 I-JSON."""


def _validate_string(value: str) -> None:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise JCSCanonicalizationError("lone UTF-16 surrogate is not valid I-JSON")


def _string(value: str) -> str:
    _validate_string(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utf16_sort_key(value: str) -> bytes:
    _validate_string(value)
    return value.encode("utf-16-be")


def _number(value: int | float) -> str:
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise JCSCanonicalizationError("integer exceeds the I-JSON exact range")
        return str(value)
    if not math.isfinite(value):
        raise JCSCanonicalizationError("NaN and Infinity are not valid I-JSON")
    if value == 0:
        return "0"

    negative = value < 0
    raw = repr(abs(value)).lower()
    if "e" in raw:
        mantissa, exponent_text = raw.split("e", 1)
        exponent = int(exponent_text)
    else:
        mantissa, exponent = raw, 0
    if "." in mantissa:
        whole, fraction = mantissa.split(".", 1)
    else:
        whole, fraction = mantissa, ""
    digits = (whole + fraction).lstrip("0") or "0"
    decimal_exponent = exponent - len(fraction)
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
        decimal_exponent += 1
    magnitude = abs(value)

    if 1e-6 <= magnitude < 1e21:
        point = len(digits) + decimal_exponent
        if point <= 0:
            rendered = "0." + ("0" * -point) + digits
        elif point >= len(digits):
            rendered = digits + ("0" * (point - len(digits)))
        else:
            rendered = digits[:point] + "." + digits[point:]
    else:
        scientific_exponent = len(digits) + decimal_exponent - 1
        coefficient = digits[0] + (("." + digits[1:]) if len(digits) > 1 else "")
        sign = "+" if scientific_exponent >= 0 else ""
        rendered = f"{coefficient}e{sign}{scientific_exponent}"
    return ("-" if negative else "") + rendered


def _encode(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise JCSCanonicalizationError("JSON object keys must be strings")
        keys = sorted(value, key=_utf16_sort_key)
        return "{" + ",".join(f"{_string(key)}:{_encode(value[key])}" for key in keys) + "}"
    raise JCSCanonicalizationError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    """Return the RFC 8785 JCS UTF-8 representation of an I-JSON value."""
    return _encode(value).encode("utf-8")
