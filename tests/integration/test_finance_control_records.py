"""DEV-028 slice A: the current-value control read behind a review card.

Design 7.7 step 5 says the card reads the record's fields again when it is
opened, so a correction Henson made in Feishu on the computer shows immediately.
That makes this endpoint a *read of live personal data*, and the interesting
cases are all about what it must refuse:

- a record this service did not write and verify;
- a token minted for another record, another table, or another action;
- a service that has no ledger config at all, which must not answer "not found"
  and let a missing capability read as a missing record.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fixtures.service_keys import SignedCaller
from personal_agent_core.control_token import (
    ControlAction,
    record_batch_resource,
    sign_control_token,
)
from personal_data_mcp.server.app import build_app
from personal_data_mcp.server.config import ServerConfig
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import (
    prepare_execution,
    record_receipt,
    transition,
)


COMMIT_UTC = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def seed_verified_write(
    session,
    *,
    key: str,
    record_id: str,
    table_kind: str = "expense",
    tool: str = "finance.log_expense",
    verified: bool = True,
) -> None:
    prepare_execution(
        session,
        idempotency_key=key,
        tool=tool,
        request_fingerprint="fp",
        client_token=str(uuid.uuid4()),
        encrypted_payload=None,
        now=COMMIT_UTC,
    )
    version = transition(
        session,
        idempotency_key=key,
        current_state="prepared",
        current_version=1,
        target_state="submitting",
        now=COMMIT_UTC,
    )
    record_receipt(
        session,
        receipt_id=f"rc_{key}",
        idempotency_key=key,
        table_kind=table_kind,
        record_id=record_id,
        now=COMMIT_UTC,
        verified=verified,
    )
    version = transition(
        session,
        idempotency_key=key,
        current_state="submitting",
        current_version=version,
        target_state="committed_unverified",
        now=COMMIT_UTC,
    )
    if verified:
        transition(
            session,
            idempotency_key=key,
            current_state="committed_unverified",
            current_version=version,
            target_state="succeeded",
            now=COMMIT_UTC,
        )
    session.commit()


@pytest.fixture()
def caller() -> SignedCaller:
    return SignedCaller()


@pytest.fixture()
def sf(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


class RecordingReader:
    """A stand-in for the composed Feishu read, so calls can be counted."""

    def __init__(self, values: dict | None = None, error: Exception | None = None):
        self.calls: list[tuple[str, str]] = []
        self._values = values or {"amount": "20.00", "name": "午饭"}
        self._error = error

    async def __call__(
        self, records: list[tuple[str, str]]
    ) -> list[dict]:
        self.calls.extend(records)
        if self._error is not None:
            raise self._error
        return [
            {
                "status": "found",
                "record": {
                    "table_kind": table_kind,
                    "record_id": record_id,
                    "values": self._values,
                    "unreadable_fields": [],
                },
            }
            for table_kind, record_id in records
        ]


def make_client(caller, sf, reader):
    app = build_app(
        ServerConfig(),
        verification_ring=caller.ring,
        session_factory=sf,
        record_reader=reader,
    )
    transport = httpx.ASGITransport(app=app)

    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=transport, base_url="http://control.local"
        )

    return make


def get_record(client, caller, *, table_kind, record_id, resource=None, action=None):
    resource = resource if resource is not None else f"{table_kind}:{record_id}"
    action = action or ControlAction.GET_RECORD_FIELDS
    token = sign_control_token(caller.ring, action=action, resource=resource)

    async def scenario():
        async with client() as c:
            return await c.get(
                f"/internal/v1/records/{table_kind}/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
            )

    return run(scenario())


def post_batch(client, caller, *, records, authorised_records=None):
    authorised = authorised_records if authorised_records is not None else records
    token = sign_control_token(
        caller.ring,
        action=ControlAction.GET_RECORD_FIELDS_BATCH,
        resource=record_batch_resource(authorised),
    )

    async def scenario():
        async with client() as c:
            return await c.post(
                "/internal/v1/records:batch",
                json={
                    "records": [
                        {"table_kind": table_kind, "record_id": record_id}
                        for table_kind, record_id in records
                    ]
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    return run(scenario())


def test_a_verified_record_is_read_live(caller, sf) -> None:
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(session, key="k1", record_id="recA")

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recA",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "found"
    assert body["record"]["values"]["name"] == "午饭"
    # The point of the endpoint: the values came from the source just now.
    assert reader.calls == [("expense", "recA")]


def test_a_record_this_service_never_wrote_is_not_readable(caller, sf) -> None:
    """Otherwise this is a general ledger reader with a nicer name."""
    reader = RecordingReader()

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recSomeoneElse",
    )

    assert resp.json() == {"status": "not_found"}
    assert reader.calls == []


def test_an_unverified_receipt_is_not_readable(caller, sf) -> None:
    """A write still awaiting read-back is not a reviewable record."""
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(
            session, key="k2", record_id="recPending", verified=False
        )

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recPending",
    )

    assert resp.json() == {"status": "not_found"}
    assert reader.calls == []


def test_the_receipt_must_match_the_table_too(caller, sf) -> None:
    """A record id is only unique within its table."""
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(session, key="k3", record_id="recA")

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="income",
        record_id="recA",
    )

    assert resp.json() == {"status": "not_found"}
    assert reader.calls == []


def test_a_token_for_another_record_is_refused(caller, sf) -> None:
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(session, key="k4", record_id="recA")
        seed_verified_write(session, key="k5", record_id="recB")

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recA",
        resource="expense:recB",
    )

    assert resp.status_code == 403
    assert reader.calls == []


def test_a_batch_token_cannot_be_replayed_with_another_body(caller, sf) -> None:
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(session, key="batch-a", record_id="recA")
        seed_verified_write(session, key="batch-b", record_id="recB")

    resp = post_batch(
        make_client(caller, sf, reader),
        caller,
        records=[("expense", "recB")],
        authorised_records=[("expense", "recA")],
    )

    assert resp.status_code == 403
    assert reader.calls == []


def test_a_token_for_the_same_id_in_another_table_is_refused(caller, sf) -> None:
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(session, key="k6", record_id="recA")

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recA",
        resource="income:recA",
    )

    assert resp.status_code == 403
    assert reader.calls == []


def test_an_execution_token_cannot_read_a_record(caller, sf) -> None:
    reader = RecordingReader()
    with sf() as session:
        seed_verified_write(session, key="k7", record_id="recA")

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recA",
        action=ControlAction.GET_EXECUTION,
    )

    assert resp.status_code == 403
    assert reader.calls == []


def test_a_missing_token_is_refused(caller, sf) -> None:
    client = make_client(caller, sf, RecordingReader())

    async def scenario():
        async with client() as c:
            return await c.get("/internal/v1/records/expense/recA")

    resp = run(scenario())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "HOST_CONTEXT_MISMATCH"


def test_a_service_without_a_ledger_config_refuses_rather_than_saying_not_found(
    caller, sf
) -> None:
    """A missing capability must not be readable as a missing record."""
    with sf() as session:
        seed_verified_write(session, key="k8", record_id="recA")

    resp = get_record(
        make_client(caller, sf, None),
        caller,
        table_kind="expense",
        record_id="recA",
    )

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_a_source_failure_surfaces_as_a_stable_code_not_a_stack(caller, sf) -> None:
    from personal_agent_core.errors import AppError, ErrorCode

    reader = RecordingReader(
        error=AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED, internal_detail="renamed 名称"
        )
    )
    with sf() as session:
        seed_verified_write(session, key="k9", record_id="recA")

    resp = get_record(
        make_client(caller, sf, reader),
        caller,
        table_kind="expense",
        record_id="recA",
    )

    body = resp.json()
    assert body["error"]["code"] == "SOURCE_SCHEMA_CHANGED"
    assert "renamed" not in str(body)
