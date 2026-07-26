"""DEV-028 slice B: the two control reads the daily review needs.

The rule these inherit from the existing client is the one that matters most
here: an answer that cannot be understood is an error, never an empty result.
For the review that distinction is the whole job -- "nothing was written
yesterday" means *do not create a card and do not push*, so a control plane that
is merely unreachable must not be able to produce that same silence.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
    RecordUnavailable,
)
from personal_agent_core.control_token import MAX_RECORD_BATCH
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


# --- one card's current values -----------------------------------------------


FOUND_RECORD = {
    "table_kind": "expense",
    "record_id": "recA",
    "values": {"amount": "20.00", "name": "\u5348\u996d"},
    "unreadable_fields": [],
}


def read_one(handler):
    """One pointer through the batch read: the only record path there is."""
    return asyncio.run(
        control_client(handler).get_record_fields_batch([("expense", "recA")])
    )[0]


def batch_body(record=None, **overrides):
    entry = {
        "status": "found",
        "table_kind": "expense",
        "record_id": "recA",
        "record": FOUND_RECORD if record is None else record,
    }
    entry.update(overrides)
    return {"records": [entry]}


def test_unreadable_fields_are_carried_rather_than_dropped() -> None:
    body = batch_body(
        {
            **FOUND_RECORD,
            "values": {"name": "\u5348\u996d"},
            "unreadable_fields": ["amount"],
        }
    )

    record = read_one(lambda request: httpx.Response(200, json=body))

    assert record.unreadable_fields == ("amount",)
    assert "amount" not in record.values


def test_a_body_describing_another_record_is_refused() -> None:
    """Otherwise one card could show another entry's values."""
    body = batch_body({**FOUND_RECORD, "record_id": "recB"})

    with pytest.raises(ControlPlaneError):
        read_one(lambda request: httpx.Response(200, json=body))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"records": [{"status": "found"}]}),
        httpx.Response(200, json={"records": [{"status": "surprise"}]}),
        httpx.Response(
            200,
            json={"records": [{"status": "found", "record": {"table_kind": "expense"}}]},
        ),
        httpx.Response(
            200,
            json={
                "records": [
                    {
                        "status": "found",
                        "record": {**FOUND_RECORD, "unreadable_fields": "amount"},
                    }
                ]
            },
        ),
        httpx.Response(200, json={"records": []}),
        httpx.Response(200, json={"records": "recA"}),
    ],
)
def test_an_unreadable_record_answer_is_an_error(response) -> None:
    with pytest.raises(ControlPlaneError):
        read_one(lambda request: response)


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
                    {
                        "status": "found",
                        "table_kind": "expense",
                        "record_id": "recA",
                        "record": FOUND_RECORD,
                    },
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


def test_a_card_larger_than_the_cap_is_split_instead_of_refused() -> None:
    """The server refuses an oversized batch, so the client must not send one.

    A day with more entries than the cap would otherwise produce a card that can
    never be opened, and the failure would be indistinguishable from Finance
    being unreachable.
    """
    requests: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["records"]
        if len(sent) > MAX_RECORD_BATCH:
            # Exactly what the real control route does with an oversized body.
            return httpx.Response(
                400, json={"error": {"code": "INVALID_ARGUMENT"}}
            )
        requests.append(sent)
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "status": "found",
                        "table_kind": item["table_kind"],
                        "record_id": item["record_id"],
                        "record": {
                            "table_kind": item["table_kind"],
                            "record_id": item["record_id"],
                            "values": {"amount": "1.00"},
                            "unreadable_fields": [],
                        },
                    }
                    for item in sent
                ]
            },
        )

    pointers = [("expense", f"rec{index}") for index in range(MAX_RECORD_BATCH + 5)]
    results = asyncio.run(
        control_client(handler).get_record_fields_batch(pointers)
    )

    assert len(results) == len(pointers)
    # Order is preserved across the split, so item N is still record N.
    assert [record.record_id for record in results] == [p[1] for p in pointers]
    assert [len(sent) for sent in requests] == [MAX_RECORD_BATCH, 5]


def test_an_empty_card_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an empty card must not reach the control plane")

    assert asyncio.run(control_client(handler).get_record_fields_batch([])) == []


def test_a_slow_record_batch_outlives_the_metadata_read_timeout() -> None:
    """A real socket guards the batch-specific budget.

    The metadata control reads intentionally fail after five seconds. A card
    validates schema and reads source records, so inheriting that budget made a
    healthy Finance service look unavailable.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            time.sleep(5.5)
            payload = json.dumps(
                {
                    "records": [
                        {
                            "status": "not_found",
                            "table_kind": item["table_kind"],
                            "record_id": item["record_id"],
                        }
                        for item in body["records"]
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = FinanceControlClient(
        base_url=f"http://127.0.0.1:{server.server_port}",
        signing_ring=ring(),
    )
    try:
        started = time.monotonic()
        result = asyncio.run(
            client.get_record_fields_batch([("expense", "recA")])
        )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert result == [None]
    assert elapsed >= 5.5
