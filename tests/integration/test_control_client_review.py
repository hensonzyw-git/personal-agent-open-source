"""DEV-028 slice B: the two control reads the daily review needs.

The rule these inherit from the existing client is the one that matters most
here: an answer that cannot be understood is an error, never an empty result.
For the review that distinction is the whole job -- "nothing was written
yesterday" means *do not create a card and do not push*, so a control plane that
is merely unreachable must not be able to produce that same silence.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
    RecordUnavailable,
)
from personal_agent_core.host_context import ServiceKey, ServiceKeyRing


def ring() -> ServiceKeyRing:
    key = ec.generate_private_key(ec.SECP256R1())
    return ServiceKeyRing(active=ServiceKey("svc-1", key, key.public_key()))


def control_client(handler, *, base_url: str = "http://127.0.0.1:8848"):
    return FinanceControlClient(
        base_url=base_url,
        signing_ring=ring(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


WRITE = {
    "tool": "finance.log_expense",
    "table_kind": "expense",
    "record_id": "recA",
    "committed_at": "2026-07-25T06:00:00.000000Z",
}


# --- successful writes for a ledger day --------------------------------------


def test_the_day_is_read_with_a_date_bound_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200, json={"write_date": "2026-07-25", "writes": [WRITE]}
        )

    writes = asyncio.run(
        control_client(handler).list_successful_writes("2026-07-25")
    )

    assert [w.record_id for w in writes] == ["recA"]
    assert writes[0].tool == "finance.log_expense"
    assert seen["path"] == "/internal/v1/successful-writes"
    assert seen["query"] == {"write_date": "2026-07-25"}
    assert seen["auth"].startswith("Bearer ")


def test_an_empty_day_is_a_real_answer() -> None:
    client = control_client(
        lambda request: httpx.Response(
            200, json={"write_date": "2026-07-25", "writes": []}
        )
    )

    assert asyncio.run(client.list_successful_writes("2026-07-25")) == []


def test_an_answer_for_another_day_is_refused() -> None:
    """A card built from the wrong day's writes would be silently wrong."""
    client = control_client(
        lambda request: httpx.Response(
            200, json={"write_date": "2026-07-24", "writes": [WRITE]}
        )
    )

    with pytest.raises(ControlPlaneError):
        asyncio.run(client.list_successful_writes("2026-07-25"))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"error": {"code": "HOST_CONTEXT_MISMATCH"}}),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"write_date": "2026-07-25"}),
        httpx.Response(200, json={"write_date": "2026-07-25", "writes": {}}),
        httpx.Response(
            200, json={"write_date": "2026-07-25", "writes": ["recA"]}
        ),
        httpx.Response(
            200,
            json={
                "write_date": "2026-07-25",
                "writes": [{k: v for k, v in WRITE.items() if k != "record_id"}],
            },
        ),
        httpx.Response(
            200,
            json={"write_date": "2026-07-25", "writes": [{**WRITE, "record_id": ""}]},
        ),
    ],
)
def test_an_unreadable_day_is_never_an_empty_day(response) -> None:
    client = control_client(lambda request: response)
    with pytest.raises(ControlPlaneError):
        asyncio.run(client.list_successful_writes("2026-07-25"))


def test_a_transport_failure_is_not_an_empty_day() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ControlPlaneError):
        asyncio.run(control_client(handler).list_successful_writes("2026-07-25"))


# --- one record's current values ---------------------------------------------


FOUND = {
    "status": "found",
    "record": {
        "table_kind": "expense",
        "record_id": "recA",
        "values": {"amount": "20.00", "name": "午饭"},
        "unreadable_fields": [],
    },
}


def read(handler):
    return asyncio.run(
        control_client(handler).get_record_fields(
            table_kind="expense", record_id="recA"
        )
    )


def test_the_record_is_read_with_a_record_bound_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=FOUND)

    record = read(handler)

    assert record.values["name"] == "午饭"
    assert record.unreadable_fields == ()
    assert seen["path"] == "/internal/v1/records/expense/recA"


def test_a_record_finance_has_no_receipt_for_is_none() -> None:
    assert read(lambda request: httpx.Response(200, json={"status": "not_found"})) is None


def test_unreadable_fields_are_carried_rather_than_dropped() -> None:
    body = {
        "status": "found",
        "record": {
            **FOUND["record"],
            "values": {"name": "午饭"},
            "unreadable_fields": ["amount"],
        },
    }

    record = read(lambda request: httpx.Response(200, json=body))

    assert record.unreadable_fields == ("amount",)
    assert "amount" not in record.values


def test_a_body_describing_another_record_is_refused() -> None:
    """Otherwise one card could show another entry's values."""
    body = {
        "status": "found",
        "record": {**FOUND["record"], "record_id": "recB"},
    }

    with pytest.raises(ControlPlaneError):
        read(lambda request: httpx.Response(200, json=body))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": "found"}),
        httpx.Response(200, json={"status": "surprise", "record": FOUND["record"]}),
        httpx.Response(
            200,
            json={"status": "found", "record": {"table_kind": "expense"}},
        ),
        httpx.Response(
            200,
            json={
                "status": "found",
                "record": {**FOUND["record"], "unreadable_fields": "amount"},
            },
        ),
    ],
)
def test_an_unreadable_record_answer_is_an_error(response) -> None:
    with pytest.raises(ControlPlaneError):
        read(lambda request: response)


def test_a_card_is_read_in_one_bound_batch_request() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "records": [
                    FOUND,
                    {
                        "status": "not_found",
                        "table_kind": "income",
                        "record_id": "recB",
                    },
                    {
                        "status": "unavailable",
                        "table_kind": "family_fund",
                        "record_id": "recC",
                    },
                ]
            },
        )

    results = asyncio.run(
        control_client(handler).get_record_fields_batch(
            [
                ("expense", "recA"),
                ("income", "recB"),
                ("family_fund", "recC"),
            ]
        )
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/internal/v1/records:batch"
    assert results[0].record_id == "recA"
    assert results[1] is None
    assert isinstance(results[2], RecordUnavailable)


def test_a_batch_entry_that_changes_its_pointer_is_refused() -> None:
    body = {
        "records": [
            {
                "status": "not_found",
                "table_kind": "income",
                "record_id": "recA",
            }
        ]
    }
    with pytest.raises(ControlPlaneError):
        asyncio.run(
            control_client(lambda request: httpx.Response(200, json=body))
            .get_record_fields_batch([("expense", "recA")])
        )
