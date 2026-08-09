#!/usr/bin/env python3
"""RFC 8785 regression vectors for the dependency-free DAL canonicalizer."""

from __future__ import annotations

import math
import struct

from dal_jcs import JCSCanonicalizationError, canonical_bytes


def assert_equal(value: object, expected: str) -> None:
    actual = canonical_bytes(value).decode("utf-8")
    if actual != expected:
        raise AssertionError(f"JCS mismatch: {actual!r} != {expected!r}")


def main() -> None:
    # RFC 8785 section 3.2.2 sample, including ECMAScript number spelling.
    assert_equal(
        {
            "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
            "string": "\u20ac$\x0f\nA'B\"\\\"/",
            "literals": [None, True, False],
        },
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"€$\\u000f\\nA\'B\\\"\\\\\\\"/"}',
    )
    # UTF-16 ordering differs from Unicode code-point ordering for non-BMP keys.
    assert_equal({"\ufb33": 1, "😀": 2, "€": 3}, '{"€":3,"😀":2,"דּ":1}')
    assert_equal([1e-7, 1e-6, 1e20, 1e21, -0.0], '[1e-7,0.000001,100000000000000000000,1e+21,0]')
    appendix_b = {
        "0000000000000001": "5e-324",
        "8000000000000001": "-5e-324",
        "7fefffffffffffff": "1.7976931348623157e+308",
        "ffefffffffffffff": "-1.7976931348623157e+308",
        "4340000000000000": "9007199254740992",
        "4430000000000000": "295147905179352830000",
        "44b52d02c7e14af5": "9.999999999999997e+22",
        "44b52d02c7e14af6": "1e+23",
        "44b52d02c7e14af7": "1.0000000000000001e+23",
        "444b1ae4d6e2ef4e": "999999999999999700000",
        "444b1ae4d6e2ef4f": "999999999999999900000",
        "444b1ae4d6e2ef50": "1e+21",
    }
    for binary64, expected in appendix_b.items():
        value = struct.unpack(">d", bytes.fromhex(binary64))[0]
        assert_equal(value, expected)
    for invalid in (math.nan, math.inf, -math.inf, 2**53, "\ud800"):
        try:
            canonical_bytes(invalid)
        except JCSCanonicalizationError:
            continue
        raise AssertionError(f"invalid I-JSON value accepted: {invalid!r}")
    print("RFC8785 JCS vectors: PASS")


if __name__ == "__main__":
    main()
