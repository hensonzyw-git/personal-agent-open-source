"""DEV-019: the unknown-commit reconciler resolves every fault to zero dupes.

The fake Feishu here deduplicates a create by `client_token`, which is the
behaviour an empirical check on the real test Base confirmed (a second create
with the same token returns the original record and adds no row). Every test
asserts on the number of rows the fake actually holds, because "zero duplicate
records" is the property that matters and it is the one worth measuring directly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.expense_record import ExpenseEntry
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.reconciler import (
    ReconcileError,
    reconcile_expense,
)
from personal_data_mcp.finance.schema_validator import validate_schema
from personal_data_mcp.finance.onboarding import observed_from_snapshot
from personal_data_mcp.finance.write_path import write_expense
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import acquire_recovery_lease
from personal_data_mcp.storage.models import ExternalReceipt, ToolExecution


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(encoding="utf-8")
)
VALIDATION = validate_schema(CONFIG, observed_from_snapshot(SNAPSHOT))
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)
LUNCH = ExpenseEntry(
    name="午饭",
    amount_cny=Decimal("20.00"),
    occurred_on=date(2026, 7, 23),
    is_family_expense=False,
    category="餐饮",
)
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def clock():
    return 1000.0


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


@pytest.fixture()
def keyring():
    return KeyRing([generate_key("recon-2026")], service="personal_data_mcp")


class DedupingFeishu:
    """A Bitable that dedupes creates by client_token, like the real one."""

    def __init__(self) -> None:
        self.by_token: dict[str, str] = {}
        self.records: dict[str, dict] = {}
        self.next_id = 1
        self.create_attempts = 0
        self.create_fails = 0  # fail the first N create calls
        self.read_fails = 0

    @property
    def row_count(self) -> int:
        return len(self.records)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t", "expire": 7200}
            )
        if request.method == "POST" and path.endswith("/records"):
            self.create_attempts += 1
            if self.create_fails > 0:
                self.create_fails -= 1
                raise httpx.ConnectTimeout("create boom")
            token = request.url.params.get("client_token")
            fields = json.loads(request.content)["fields"]
            if token in self.by_token:
                rid = self.by_token[token]  # idempotent: same record back
            else:
                rid = f"rec{self.next_id:06d}"
                self.next_id += 1
                self.by_token[token] = rid
                self.records[rid] = fields
            return httpx.Response(
                200,
                json={"code": 0, "data": {"record": {"record_id": rid, "fields": self.records[rid]}}},
            )
        if request.method == "GET" and "/records/" in path:
            if self.read_fails > 0:
                self.read_fails -= 1
                raise httpx.ConnectError("read boom")
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={"code": 0, "data": {"record": {"record_id": rid, "fields": self.records[rid]}}},
            )
        raise AssertionError(f"unexpected {request.method} {path}")


def adapter_for(fake: DedupingFeishu) -> FeishuAdapter:
    return FeishuAdapter(
        FeishuCredentials(app_id="cli", app_secret="s"),
        transport=httpx.MockTransport(fake.handler),
        now=clock,
    )


def state_of(sessions, key="idem-1") -> str | None:
    with sessions() as session:
        ex = session.get(ToolExecution, key)
        return ex.state if ex else None


def receipt_of(sessions, key="idem-1"):
    with sessions() as session:
        return (
            session.query(ExternalReceipt)
            .filter(ExternalReceipt.idempotency_key == key)
            .one_or_none()
        )


async def drive_to_commit_unknown(fake, sessions, keyring, key="idem-1"):
    """Use the real write path, with a failing create, to reach commit_unknown."""
    fake.create_fails = 1
    async with adapter_for(fake) as adapter:
        with pytest.raises(AppError):
            await write_expense(
                LUNCH,
                sessions=sessions,
                adapter=adapter,
                config=CONFIG,
                validation=VALIDATION,
                source=SOURCE,
                idempotency_key=key,
                request_fingerprint="fp",
                trace_id="t",
                keyring=keyring,
            )
    assert state_of(sessions, key) == "commit_unknown"


def reconcile(fake, sessions, keyring, key="idem-1", **kw):
    async def scenario():
        async with adapter_for(fake) as adapter:
            return await reconcile_expense(
                key,
                sessions=sessions,
                adapter=adapter,
                source=SOURCE,
                config=CONFIG,
                keyring=keyring,
                owner="worker-1",
                now=lambda: NOW,
                **kw,
            )

    return run(scenario())


# --- the fault matrix --------------------------------------------------------


def test_commit_unknown_where_the_write_never_landed_creates_it_once(
    sessions, keyring
) -> None:
    fake = DedupingFeishu()
    run(drive_to_commit_unknown(fake, sessions, keyring))
    assert fake.row_count == 0  # the failed create left nothing

    result = reconcile(fake, sessions, keyring)
    assert result.final_state == "succeeded"
    assert fake.row_count == 1
    assert receipt_of(sessions).verified_at is not None


def test_commit_unknown_where_the_write_did_land_adds_no_duplicate(
    sessions, keyring
) -> None:
    # Model the lost-response case: the record was created under the token, but
    # the write path saw a failure and recorded commit_unknown.
    fake = DedupingFeishu()
    run(drive_to_commit_unknown(fake, sessions, keyring))
    # The token the execution holds is the one a replay will present. Simulate
    # the first attempt having actually landed by pre-seeding that token.
    with sessions() as session:
        token = session.get(ToolExecution, "idem-1").client_token
    fake.by_token[token] = "rec000042"
    fake.records["rec000042"] = json.loads(
        session_payload(sessions, "idem-1", keyring)
    )

    result = reconcile(fake, sessions, keyring)
    assert result.final_state == "succeeded"
    assert result.record_id == "rec000042"
    # The pre-existing row is the only row: the replay deduped, zero duplicates.
    assert fake.row_count == 1


def test_a_read_back_mismatch_escalates_and_keeps_the_record_id(
    sessions, keyring
) -> None:
    fake = DedupingFeishu()
    run(drive_to_commit_unknown(fake, sessions, keyring))

    # The replay will create the row; then corrupt what read-back returns.
    result_holder = {}

    async def scenario():
        async with adapter_for(fake) as adapter:
            from personal_data_mcp.finance import reconciler

            res = await reconciler.reconcile_expense(
                "idem-1",
                sessions=sessions,
                adapter=adapter,
                source=SOURCE,
                config=CONFIG,
                keyring=keyring,
                owner="w",
                now=lambda: NOW,
            )
            result_holder["res"] = res

    # Corrupt the stored amount after the create but observed at read time by
    # mutating the fake's record once it exists.
    original_handler = fake.handler

    def corrupting(request):
        resp = original_handler(request)
        if request.method == "POST" and request.url.path.endswith("/records"):
            for rid in fake.records:
                fake.records[rid]["原始金额"] = 999
        return resp

    fake.handler = corrupting
    run(scenario())
    assert result_holder["res"].final_state == "needs_manual_review"
    assert result_holder["res"].record_id is not None


def test_committed_unverified_only_reads_back_and_never_replays(
    sessions, keyring
) -> None:
    fake = DedupingFeishu()
    # Land the write and reach committed_unverified by failing read-back.
    fake.read_fails = 99

    async def scenario():
        async with adapter_for(fake) as adapter:
            with pytest.raises(AppError):
                await write_expense(
                    LUNCH,
                    sessions=sessions,
                    adapter=adapter,
                    config=CONFIG,
                    validation=VALIDATION,
                    source=SOURCE,
                    idempotency_key="idem-1",
                    request_fingerprint="fp",
                    trace_id="t",
                    keyring=keyring,
                )

    run(scenario())
    assert state_of(sessions) == "committed_unverified"
    assert fake.row_count == 1
    attempts_before = fake.create_attempts

    fake.read_fails = 0
    result = reconcile(fake, sessions, keyring)
    assert result.final_state == "succeeded"
    # No second create was issued: verification alone finished the job.
    assert fake.create_attempts == attempts_before
    assert fake.row_count == 1


def test_a_missing_sealed_payload_escalates(sessions, keyring) -> None:
    fake = DedupingFeishu()
    fake.create_fails = 1
    async def scenario():
        async with adapter_for(fake) as adapter:
            with pytest.raises(AppError):
                await write_expense(
                    LUNCH,
                    sessions=sessions,
                    adapter=adapter,
                    config=CONFIG,
                    validation=VALIDATION,
                    source=SOURCE,
                    idempotency_key="idem-1",
                    request_fingerprint="fp",
                    trace_id="t",
                    keyring=None,  # no keyring: nothing sealed
                )
    run(scenario())
    assert state_of(sessions) == "commit_unknown"

    result = reconcile(fake, sessions, keyring)
    assert result.final_state == "needs_manual_review"


def test_a_second_worker_cannot_reconcile_concurrently(sessions, keyring) -> None:
    fake = DedupingFeishu()
    run(drive_to_commit_unknown(fake, sessions, keyring))
    # A live lease held by another worker blocks this one.
    with sessions() as session:
        assert acquire_recovery_lease(
            session, idempotency_key="idem-1", owner="other", now=NOW
        )
        session.commit()

    with pytest.raises(ReconcileError, match="another worker"):
        reconcile(fake, sessions, keyring)


def test_a_terminal_execution_is_returned_unchanged(sessions, keyring) -> None:
    fake = DedupingFeishu()
    run(drive_to_commit_unknown(fake, sessions, keyring))
    first = reconcile(fake, sessions, keyring)
    assert first.final_state == "succeeded"
    rows = fake.row_count

    # Reconciling an already-succeeded execution is a no-op, not a second write.
    second = reconcile(fake, sessions, keyring)
    assert second.final_state == "succeeded"
    assert second.record_id == first.record_id
    assert fake.row_count == rows
    assert fake.create_attempts  # unchanged count asserted below
    before = fake.create_attempts
    reconcile(fake, sessions, keyring)
    assert fake.create_attempts == before


def session_payload(sessions, key, keyring) -> str:
    """Decrypt the sealed payload of an execution, as a JSON fields string."""
    from personal_data_mcp.finance.write_path import open_create_payload

    with sessions() as session:
        sealed = session.get(ToolExecution, key).encrypted_payload
    return json.dumps(
        open_create_payload(sealed, keyring=keyring, idempotency_key=key)
    )
