"""DEV-028 slice D: the daily-review HTTP surface (design 5.3).

A real Agent database and the real routes; the ledger read is injected, because
its own failures are already covered where it lives. What is tested here is the
contract the iPhone will depend on:

- a card opens with values read *now*, not with anything cached at write time;
- one unreadable row degrades that row and not the card;
- `ack` and `defer` move a status column and nothing else -- in particular they
  never touch an operation, a record or the outbox;
- every route needs an active device, like the rest of the API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.control_client import RecordFields, RecordUnavailable
from personal_agent.auth.tokens import SigningKey, TokenKeyRing, issue_access_token
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    DailyReview,
    DailyReviewItem,
    Device,
    NotificationOutbox,
)
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 7, 26, 16, 5, tzinfo=timezone.utc)
COMMITTED = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def token_ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(active=SigningKey("tok-2026", private, private.public_key()))


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")], service="personal-agent"
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="THUMB",
                status="active",
                scopes='["finance.write"]',
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.add(
            DailyReview(
                review_id="rev-1",
                review_date="2026-07-25",
                status="pending",
                created_at=NOW,
            )
        )
        session.add(
            DailyReviewItem(
                item_id="item-1",
                review_id="rev-1",
                tool="finance.log_expense",
                record_id="recA",
                committed_at=COMMITTED,
            )
        )
        session.add(
            DailyReviewItem(
                item_id="item-2",
                review_id="rev-1",
                tool="finance.log_income",
                record_id="recB",
                committed_at=COMMITTED,
            )
        )
        session.add(
            DailyReview(
                review_id="rev-old",
                review_date="2026-07-24",
                status="reviewed",
                created_at=NOW,
                reviewed_at=NOW,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


class Reader:
    def __init__(self, values=None, *, missing=(), broken=()) -> None:
        self.values = values or {"amount": "20.00", "name": "午饭"}
        self.missing = set(missing)
        self.broken = set(broken)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, records: list[tuple[str, str]]):
        self.calls.extend(records)
        results = []
        for table_kind, record_id in records:
            if record_id in self.broken:
                results.append(RecordUnavailable(table_kind, record_id))
            elif record_id in self.missing:
                results.append(None)
            else:
                results.append(
                    RecordFields(
                        table_kind=table_kind,
                        record_id=record_id,
                        values=dict(self.values),
                        unreadable_fields=(),
                    )
                )
        return results


def _client(engine, token_ring, keyring, *, read_record=None) -> TestClient:
    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        build_interpreter=lambda auth: None,
        build_dispatcher=lambda auth, trace_id: None,
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=lambda auth: [],
        now=lambda: NOW,
        read_record=read_record,
    )
    return TestClient(build_app(deps))


def _auth(token_ring, *, device_id: str = "dev-1") -> dict:
    token = issue_access_token(
        token_ring,
        device_id=device_id,
        device_key_thumbprint="THUMB",
        scopes=["finance.write"],
        allowed_tools_version="v1",
        now=NOW,
    )
    return {"Authorization": f"Bearer {token}"}


# --- listing -----------------------------------------------------------------


def test_the_list_is_local_and_reads_no_ledger(engine, token_ring, keyring) -> None:
    reader = Reader()
    client = _client(engine, token_ring, keyring, read_record=reader)

    response = client.get("/v1/daily-reviews", headers=_auth(token_ring))

    assert response.status_code == 200
    reviews = response.json()["reviews"]
    assert [r["review_date"] for r in reviews] == ["2026-07-25", "2026-07-24"]
    assert reviews[0]["item_count"] == 2
    assert reader.calls == []


def test_the_list_can_be_filtered_to_pending(engine, token_ring, keyring) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    response = client.get(
        "/v1/daily-reviews", params={"status": "pending"}, headers=_auth(token_ring)
    )

    assert [r["review_id"] for r in response.json()["reviews"]] == ["rev-1"]


def test_an_unknown_status_filter_is_a_client_error(engine, token_ring, keyring) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    response = client.get(
        "/v1/daily-reviews", params={"status": "acknowledged"}, headers=_auth(token_ring)
    )

    assert response.status_code == 400


# --- opening a card ----------------------------------------------------------


def test_opening_a_card_reads_every_record_live(engine, token_ring, keyring) -> None:
    reader = Reader()
    client = _client(engine, token_ring, keyring, read_record=reader)

    body = client.get("/v1/daily-reviews/rev-1", headers=_auth(token_ring)).json()

    assert body["review_date"] == "2026-07-25"
    assert [item["record_id"] for item in body["items"]] == ["recA", "recB"]
    assert body["items"][0]["values"]["name"] == "午饭"
    # The tool decides the table, and both were read from the source just now.
    assert reader.calls == [("expense", "recA"), ("income", "recB")]


def test_one_unreadable_row_does_not_sink_the_card(engine, token_ring, keyring) -> None:
    reader = Reader(broken={"recB"})
    client = _client(engine, token_ring, keyring, read_record=reader)

    body = client.get("/v1/daily-reviews/rev-1", headers=_auth(token_ring)).json()

    assert body["items"][0]["values"]["name"] == "午饭"
    assert body["items"][1]["unavailable"] == "source_unavailable"
    assert "values" not in body["items"][1]
    # The count still matches what was written, so nothing looks lost.
    assert body["item_count"] == 2


def test_a_record_finance_has_no_receipt_for_is_marked_not_hidden(
    engine, token_ring, keyring
) -> None:
    reader = Reader(missing={"recA"})
    client = _client(engine, token_ring, keyring, read_record=reader)

    body = client.get("/v1/daily-reviews/rev-1", headers=_auth(token_ring)).json()

    assert body["items"][0]["unavailable"] == "no_receipt"
    assert body["item_count"] == 2


def test_opening_a_card_without_a_composed_reader_says_so(
    engine, token_ring, keyring
) -> None:
    client = _client(engine, token_ring, keyring, read_record=None)

    response = client.get("/v1/daily-reviews/rev-1", headers=_auth(token_ring))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_an_unknown_review_is_a_client_error(engine, token_ring, keyring) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    response = client.get("/v1/daily-reviews/rev-nope", headers=_auth(token_ring))

    assert response.status_code == 400


# --- ack and defer -----------------------------------------------------------


def test_ack_marks_the_card_and_touches_nothing_else(
    engine, token_ring, keyring
) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    body = client.post("/v1/daily-reviews/rev-1/ack", headers=_auth(token_ring)).json()

    assert body["status"] == "reviewed"
    assert body["reviewed_at"] is not None
    with session_factory(engine)() as session:
        assert session.get(DailyReview, "rev-1").status == "reviewed"
        # No ledger effect, and no notification invented on the way past.
        assert list(session.query(NotificationOutbox)) == []
        assert len(list(session.query(DailyReviewItem))) == 2


def test_ack_is_idempotent_and_keeps_the_first_instant(
    engine, token_ring, keyring
) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    first = client.post("/v1/daily-reviews/rev-1/ack", headers=_auth(token_ring)).json()
    second = client.post("/v1/daily-reviews/rev-1/ack", headers=_auth(token_ring)).json()

    assert first == second


def test_defer_keeps_the_card_outstanding(engine, token_ring, keyring) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    body = client.post(
        "/v1/daily-reviews/rev-1/defer", headers=_auth(token_ring)
    ).json()

    assert body["status"] == "deferred"
    assert body["reviewed_at"] is None


def test_a_reviewed_card_cannot_be_deferred_back(engine, token_ring, keyring) -> None:
    """Otherwise `reviewed_at` would describe a review being asked for again."""
    client = _client(engine, token_ring, keyring, read_record=Reader())

    response = client.post("/v1/daily-reviews/rev-old/defer", headers=_auth(token_ring))

    assert response.status_code == 400
    with session_factory(engine)() as session:
        assert session.get(DailyReview, "rev-old").status == "reviewed"


# --- auth --------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/v1/daily-reviews"),
        ("get", "/v1/daily-reviews/rev-1"),
        ("post", "/v1/daily-reviews/rev-1/ack"),
        ("post", "/v1/daily-reviews/rev-1/defer"),
    ],
)
def test_every_review_route_needs_an_active_device(
    engine, token_ring, keyring, method, path
) -> None:
    client = _client(engine, token_ring, keyring, read_record=Reader())

    assert getattr(client, method)(path).status_code == 401

    with session_factory(engine)() as session:
        device = session.get(Device, "dev-1")
        device.status = "revoked"
        device.revoked_at = NOW
        session.commit()

    response = getattr(client, method)(path, headers=_auth(token_ring))
    assert response.status_code == 401
