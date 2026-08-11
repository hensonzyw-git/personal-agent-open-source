"""The Finance query cursor key is optional to advertise, strict when present."""

from __future__ import annotations

import base64

import pytest

from personal_data_mcp.finance.query_cursor_secret import (
    CURSOR_SECRET_ENV,
    QueryCursorSecretError,
    load_query_cursor_secret,
)


def test_missing_optional_key_leaves_query_disabled() -> None:
    assert load_query_cursor_secret({}, required=False) is None


def test_missing_required_key_is_refused() -> None:
    with pytest.raises(QueryCursorSecretError):
        load_query_cursor_secret({})


def test_a_valid_base64url_key_round_trips() -> None:
    encoded = base64.urlsafe_b64encode(b"q" * 32).decode("ascii")
    assert load_query_cursor_secret({CURSOR_SECRET_ENV: encoded}) == b"q" * 32


@pytest.mark.parametrize(
    "raw",
    [
        "not valid base64!!",
        "   ",
        " " + base64.urlsafe_b64encode(b"q" * 32).decode("ascii"),
        "aGVsbG8=",
        base64.urlsafe_b64encode(b"q" * 32).decode("ascii") + "=",
        base64.urlsafe_b64encode(b"q" * 32).decode("ascii").rstrip("=")[:-1]
        + "F",
    ],
)
def test_a_present_invalid_key_is_refused(raw: str) -> None:
    with pytest.raises(QueryCursorSecretError):
        load_query_cursor_secret({CURSOR_SECRET_ENV: raw}, required=False)
