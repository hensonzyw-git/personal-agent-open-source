"""DEV-018 acceptance: one write, one record, and success only on proof.

Every case runs against a mock Feishu and a real SQLite file, so the execution
rows and the HTTP traffic are both observable. The two properties that matter
most are counted directly rather than asserted about in prose: how many creates
left the process, and whether an outcome claimed success without a verified
read-back.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.expense_record import (
    ExpenseEntry,
    build_expense_payload,
)
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.schema_validator import (
    SchemaValidation,
    Drift,
    DriftKind,
    validate_schema,
)
from personal_data_mcp.finance.onboarding import observed_from_snapshot
from personal_data_mcp.finance.write_path import write_expense
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
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

PLACEHOLDER_CREDENTIALS = None  # set in the fixture


def clock():
    return 1000.0


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


class FakeFeishu:
    """A minimal Bitable that records what it was asked to do."""

    def __init__(self) -> None:
        self.creates: list[httpx.Request] = []
        self.reads: list[httpx.Request] = []
        self.records: dict[str, dict] = {}
        self.next_id = 1
        #: Set to rewrite what the table "stores", to model a mismatch.
        self.mutate_stored = None
        self.create_fails_with: Exception | None = None
        self.read_fails = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        if request.method == "POST" and path.endswith("/records"):
            self.creates.append(request)
            if self.create_fails_with is not None:
                raise self.create_fails_with
            fields = json.loads(request.content)["fields"]
            stored = copy.deepcopy(fields)
            if self.mutate_stored is not None:
                stored = self.mutate_stored(stored)
            record_id = f"rec{self.next_id:06d}"
            self.next_id += 1
            self.records[record_id] = stored
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"record": {"record_id": record_id, "fields": stored}},
                },
            )
        if request.method == "GET" and "/records/" in path:
            self.reads.append(request)
            if self.read_fails > 0:
                self.read_fails -= 1
                raise httpx.ConnectError("read boom")
            record_id = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": record_id,
                            "fields": self.records[record_id],
                        }
                    },
                },
            )
        raise AssertionError(f"unexpected call {request.method} {path}")


def adapter_for(fake: FakeFeishu) -> FeishuAdapter:
    from personal_data_mcp.feishu.credentials import FeishuCredentials

    return FeishuAdapter(
        FeishuCredentials(app_id="cli_test", app_secret="shh"),
        transport=httpx.MockTransport(fake.handler),
        now=clock,
    )


async def do_write(
    fake: FakeFeishu,
    sessions,
    *,
    entry: ExpenseEntry = LUNCH,
    key: str = "idem-1",
    fingerprint: str = "fp-1",
    validation: SchemaValidation = VALIDATION,
    config=CONFIG,
    source=SOURCE,
):
    async with adapter_for(fake) as adapter:
        return await write_expense(
            entry,
            sessions=sessions,
            adapter=adapter,
            config=config,
            validation=validation,
            source=source,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            trace_id="trace-1",
        )


def state_of(sessions, key: str = "idem-1") -> str | None:
    with sessions() as session:
        execution = session.get(ToolExecution, key)
        return execution.state if execution else None


def receipts_of(sessions, key: str = "idem-1") -> list[ExternalReceipt]:
    with sessions() as session:
        return list(
            session.query(ExternalReceipt).filter(
                ExternalReceipt.idempotency_key == key
            )
        )


# --- the happy path ----------------------------------------------------------


def test_a_verified_write_succeeds_and_returns_external_evidence(sessions) -> None:
    fake = FakeFeishu()
    outcome = run(do_write(fake, sessions))

    assert outcome.status == "created"
    assert outcome.record_id == "rec000001"
    assert state_of(sessions) == "succeeded"
    receipts = receipts_of(sessions)
    assert len(receipts) == 1
    assert receipts[0].record_id == "rec000001"
    assert receipts[0].verified_at is not None
    assert len(fake.creates) == 1
    assert len(fake.reads) == 1


def test_the_payload_carries_exactly_the_configured_writable_fields(
    sessions,
) -> None:
    fake = FakeFeishu()
    run(do_write(fake, sessions))
    fields = json.loads(fake.creates[0].content)["fields"]

    assert set(fields) == {"原始金额", "名称", "日期", "是否家庭支出", "分类"}
    # No formula or auto-number column is addressable at all.
    assert "个人支出" not in fields and "ID" not in fields
    assert fields["原始金额"] == 20.0
    assert fields["名称"] == "午饭"
    assert fields["是否家庭支出"] is False
    assert fields["分类"] == "餐饮"
    # 2026-07-23 00:00 Asia/Shanghai
    assert fields["日期"] == 1784736000000


def test_the_create_sends_the_persisted_client_token_and_consistency_flag(
    sessions,
) -> None:
    fake = FakeFeishu()
    run(do_write(fake, sessions))
    params = fake.creates[0].url.params

    with sessions() as session:
        execution = session.get(ToolExecution, "idem-1")
        assert params.get("client_token") == execution.client_token
    assert params.get("ignore_consistency_check") == "false"


# --- success requires proof --------------------------------------------------


def test_a_read_back_mismatch_is_never_success_and_never_rewrites(
    sessions,
) -> None:
    fake = FakeFeishu()

    def corrupt(stored):
        stored["原始金额"] = 200.0
        return stored

    fake.mutate_stored = corrupt

    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions))

    assert caught.value.code is ErrorCode.SOURCE_COMMITTED_MISMATCH
    assert state_of(sessions) == "needs_manual_review"
    # The record id is kept as evidence, and no correcting write was attempted.
    assert receipts_of(sessions)[0].record_id == "rec000001"
    assert receipts_of(sessions)[0].verified_at is None
    assert len(fake.creates) == 1


def test_a_missing_record_id_is_an_unknown_commit_not_a_failure(sessions) -> None:
    fake = FakeFeishu()
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "POST" and request.url.path.endswith("/records"):
            return httpx.Response(
                200, json={"code": 0, "data": {"record": {"fields": {}}}}
            )
        return response

    fake.handler = handler
    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions))

    assert caught.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN
    assert state_of(sessions) == "commit_unknown"
    assert receipts_of(sessions) == []


def test_a_lost_response_becomes_commit_unknown_with_one_attempt(sessions) -> None:
    fake = FakeFeishu()
    fake.create_fails_with = httpx.ConnectTimeout("boom")

    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions))

    assert caught.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN
    assert state_of(sessions) == "commit_unknown"
    # Exactly one create left the process: a lost response is never retried
    # here, because a retry is how a second ledger row appears.
    assert len(fake.creates) == 1


def test_an_unverifiable_write_stays_committed_unverified(sessions) -> None:
    fake = FakeFeishu()
    fake.read_fails = 99

    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions))

    assert caught.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN
    assert state_of(sessions) == "committed_unverified"
    # The id is durable even though verification did not happen.
    assert receipts_of(sessions)[0].record_id == "rec000001"
    assert receipts_of(sessions)[0].verified_at is None


def test_a_transient_read_failure_still_verifies(sessions) -> None:
    fake = FakeFeishu()
    fake.read_fails = 2

    outcome = run(do_write(fake, sessions))
    assert outcome.status == "created"
    assert state_of(sessions) == "succeeded"
    assert len(fake.creates) == 1


# --- idempotency -------------------------------------------------------------


def test_replaying_a_succeeded_key_writes_nothing_and_returns_the_same_id(
    sessions,
) -> None:
    fake = FakeFeishu()
    first = run(do_write(fake, sessions))
    second = run(do_write(fake, sessions))

    assert second.status == "idempotent_replay"
    assert second.record_id == first.record_id
    assert len(fake.creates) == 1


def test_the_same_key_with_a_different_request_is_a_conflict(sessions) -> None:
    fake = FakeFeishu()
    run(do_write(fake, sessions))

    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions, fingerprint="fp-2"))
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(fake.creates) == 1


def test_an_unknown_commit_is_left_to_the_reconciler(sessions) -> None:
    fake = FakeFeishu()
    fake.create_fails_with = httpx.ConnectTimeout("boom")
    with pytest.raises(AppError):
        run(do_write(fake, sessions))

    # A second call on the same key must not drive it forward itself, and above
    # all must not create a second record.
    fake.create_fails_with = None
    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions))
    assert caught.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN
    assert state_of(sessions) == "commit_unknown"
    assert len(fake.creates) == 1


# --- refusals cost nothing ---------------------------------------------------


def test_a_drifted_schema_refuses_before_any_row_or_request(sessions) -> None:
    drifted = SchemaValidation(
        config_version=CONFIG.config_version,
        config_checksum=CONFIG.checksum(),
        snapshot_checksum="test-drifted-snapshot",
        drifts=(
            Drift("expense", "category", DriftKind.OPTIONS_CHANGED, "changed"),
        ),
    )
    fake = FakeFeishu()
    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions, validation=drifted))

    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED
    assert state_of(sessions) is None
    assert fake.creates == []


def test_an_unknown_category_refuses_before_any_row_or_request(sessions) -> None:
    fake = FakeFeishu()
    entry = ExpenseEntry(
        name="午饭",
        amount_cny=Decimal("20.00"),
        occurred_on=date(2026, 7, 23),
        is_family_expense=False,
        category="不存在的分类",
    )
    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions, entry=entry))

    assert caught.value.code is ErrorCode.CATEGORY_NOT_ALLOWED
    assert state_of(sessions) is None
    assert fake.creates == []


def test_a_validation_from_another_same_version_config_is_refused() -> None:
    document = CONFIG.model_dump(mode="json")
    document["tables"]["expense"]["fields"]["amount"]["id"] = "fldOTHERAMOUNT"
    other_config = load_ledger_config(document)
    assert other_config.config_version == CONFIG.config_version
    assert other_config.checksum() != CONFIG.checksum()

    with pytest.raises(AppError) as caught:
        build_expense_payload(
            LUNCH, config=other_config, validation=VALIDATION
        )
    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED


def test_a_source_not_bound_to_the_validated_config_is_refused(
    sessions,
) -> None:
    other_source = BaseSource(
        base_token="basDIFFERENT",
        ledger_kind=SOURCE.ledger_kind,
        tables=SOURCE.tables,
    )
    fake = FakeFeishu()
    with pytest.raises(AppError) as caught:
        run(do_write(fake, sessions, source=other_source))

    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED
    assert state_of(sessions) is None
    assert fake.creates == []


# --- read-back normalisation -------------------------------------------------


def test_rich_text_segments_read_back_as_the_same_name(sessions) -> None:
    fake = FakeFeishu()

    def as_segments(stored):
        stored["名称"] = [{"text": "午饭", "type": "text"}]
        return stored

    fake.mutate_stored = as_segments
    outcome = run(do_write(fake, sessions))
    assert outcome.status == "created"
    assert state_of(sessions) == "succeeded"


# --- the duplicate gate sits in front of the write ---------------------------


def keyring_for():
    from personal_agent_core.crypto import KeyRing, generate_key

    return KeyRing([generate_key("dup-test")], service="personal_data_mcp")


async def do_submit(
    fake: FakeFeishu,
    sessions,
    *,
    rows,
    key: str = "idem-1",
    override: str | None = None,
    keyring=None,
):
    from personal_data_mcp.finance.write_path import submit_expense

    async with adapter_for(fake) as adapter:
        return await submit_expense(
            LUNCH,
            sessions=sessions,
            adapter=adapter,
            config=CONFIG,
            validation=VALIDATION,
            source=SOURCE,
            idempotency_key=key,
            request_fingerprint="fp-1",
            trace_id="trace-1",
            ledger_rows=rows,
            keyring=keyring or keyring_for(),
            duplicate_override=override,
        )


def existing_lunch_row():
    from personal_data_mcp.finance.ledger_reader import LedgerExpense

    return LedgerExpense(
        record_id="rec-existing",
        name="午饭",
        amount_cny=Decimal("20.00"),
        occurred_on=date(2026, 7, 23),
        category="餐饮",
    )


def test_an_exact_duplicate_stops_the_write_entirely(sessions) -> None:
    from personal_data_mcp.finance.duplicate_check import DuplicateFinding

    fake = FakeFeishu()
    outcome = run(do_submit(fake, sessions, rows=[existing_lunch_row()]))

    assert isinstance(outcome, DuplicateFinding)
    assert [c.record_id for c in outcome.candidates] == ["rec-existing"]
    # Nothing was written, and no execution row exists to reconcile later.
    assert fake.creates == []
    assert state_of(sessions) is None


def test_no_candidate_writes_straight_through(sessions) -> None:
    fake = FakeFeishu()
    outcome = run(do_submit(fake, sessions, rows=[]))
    assert outcome.status == "created"
    assert len(fake.creates) == 1


def test_a_valid_decision_releases_exactly_one_write(sessions) -> None:
    from personal_data_mcp.finance.duplicate_check import DuplicateFinding

    keyring = keyring_for()
    fake = FakeFeishu()
    rows = [existing_lunch_row()]

    finding = run(do_submit(fake, sessions, rows=rows, keyring=keyring))
    assert isinstance(finding, DuplicateFinding)
    assert fake.creates == []

    outcome = run(
        do_submit(
            fake, sessions, rows=rows, override=finding.check_id, keyring=keyring
        )
    )
    assert outcome.status == "created"
    assert len(fake.creates) == 1


def test_an_override_is_refused_when_the_candidate_set_becomes_empty(
    sessions,
) -> None:
    from personal_data_mcp.finance.duplicate_check import (
        DuplicateFinding,
        OverrideRefused,
    )

    keyring = keyring_for()
    fake = FakeFeishu()
    finding = run(
        do_submit(
            fake,
            sessions,
            rows=[existing_lunch_row()],
            keyring=keyring,
        )
    )
    assert isinstance(finding, DuplicateFinding)

    with pytest.raises(OverrideRefused, match="candidate set changed"):
        run(
            do_submit(
                fake,
                sessions,
                rows=[],
                override=finding.check_id,
                keyring=keyring,
            )
        )
    assert fake.creates == []
    assert state_of(sessions) is None


def test_a_forged_override_does_not_release_the_write(sessions) -> None:
    from personal_data_mcp.finance.duplicate_check import OverrideRefused

    fake = FakeFeishu()
    with pytest.raises(OverrideRefused):
        run(
            do_submit(
                fake,
                sessions,
                rows=[existing_lunch_row()],
                override="00000000-0000-0000-0000-000000000000",
            )
        )
    assert fake.creates == []
    assert state_of(sessions) is None


def test_a_replay_is_never_treated_as_its_own_duplicate(sessions) -> None:
    """The subtle one: idempotency must survive the gate.

    After a successful write the ledger contains the row that write created. A
    replay of the same key scans that row, and if the gate ran again it would
    call the write a duplicate of itself and refuse -- turning idempotency into
    a failure. Design 7.6 rule 8 puts the check before the first `prepared`
    only.
    """
    fake = FakeFeishu()
    first = run(do_submit(fake, sessions, rows=[]))
    assert first.status == "created"

    # The ledger now contains what was just written.
    replay = run(do_submit(fake, sessions, rows=[existing_lunch_row()]))
    assert replay.status == "idempotent_replay"
    assert replay.record_id == first.record_id
    assert len(fake.creates) == 1


def test_both_live_read_shapes_verify_as_the_same_record() -> None:
    """Regression against what the real test Base actually returned at G3.

    The two read endpoints do not agree on JSON types for the same stored row:
    `get_record` returned the amount as the string `"20"` and the name as plain
    text, while `search_records` returned the amount as the number `20` and the
    name as rich-text segments. Comparing raw JSON would have called one of
    them a mismatch and sent a perfectly good write to manual review, so both
    shapes are pinned here as fixtures.
    """
    from personal_data_mcp.finance.expense_record import verify_expense_record

    get_record_shape = {
        "ID": "NO.001",
        "分类": "餐饮",
        "原始金额": "20",
        "名称": "午饭",
        "日期": 1784736000000,
        "是否家庭支出": False,
    }
    search_records_shape = {
        "ID": "NO.001",
        "个人支出": {"type": 2, "value": [20]},
        "分类": "餐饮",
        "原始金额": 20,
        "名称": [{"text": "午饭", "type": "text"}],
        "家庭基金变动": {"type": 2, "value": [0]},
        "日期": 1784736000000,
        "是否家庭支出": False,
    }

    assert verify_expense_record(LUNCH, get_record_shape, config=CONFIG) == []
    assert verify_expense_record(LUNCH, search_records_shape, config=CONFIG) == []


def test_the_g3_operator_command_refuses_a_production_config(tmp_path) -> None:
    """The write CLI is synthetic-only, checked before any credential is read."""
    from personal_data_mcp.feishu.base_source import LedgerSourceError
    from personal_data_mcp.finance import write_expense_cli

    document = json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
    document["ledger_kind"] = "production"
    config_path = tmp_path / "production.json"
    config_path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(LedgerSourceError, match="G5"):
        run(
            write_expense_cli.run(
                LUNCH,
                config_path=config_path,
                db_path=tmp_path / "finance.sqlite",
                idempotency_key="idem-prod",
            )
        )


def test_the_g3_command_states_semantics_and_resolves_nothing_itself() -> None:
    """The CLI collects what a human says; the ledger decides the rest.

    The accounting sign, the trip tag and any inherited category are applied by
    `resolve_expense`, so `build_entry` must pass the raw statement through
    unchanged -- including leaving the amount unsigned.
    """
    import argparse

    from personal_data_mcp.finance import write_expense_cli

    def parse(**overrides):
        args = argparse.Namespace(
            name="午饭",
            amount="20",
            date="2026-07-23",
            scope="personal",
            category="餐饮",
            entry_kind="expense",
            trip_tag=None,
            destination=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return write_expense_cli.build_entry(args)

    assert parse().input_amount == "20"
    assert parse(entry_kind="refund").input_amount == "20"
    assert parse(scope="family").is_family_expense is True
    assert parse().is_family_expense is False
    assert parse().occurred_on == date(2026, 7, 23)


def test_the_raw_entry_carries_the_statement_unresolved() -> None:
    from personal_data_mcp.finance.write_expense_cli import RawEntry

    raw = RawEntry(
        name="机票",
        input_amount="2000",
        occurred_on=date(2026, 7, 23),
        is_family_expense=True,
        entry_kind="expense",
        category=None,
        trip_tag=None,
        destination="东京",
    )
    # A bare destination is left for the resolver; the CLI does not turn it into
    # a tag itself.
    assert raw.destination == "东京"
    assert raw.trip_tag is None


def test_a_late_evening_entry_keeps_its_shanghai_ledger_date(sessions) -> None:
    fake = FakeFeishu()
    entry = ExpenseEntry(
        name="夜宵",
        amount_cny=Decimal("38.50"),
        occurred_on=date(2026, 1, 1),
        is_family_expense=True,
        category="餐饮",
    )
    outcome = run(do_write(fake, sessions, entry=entry))
    assert outcome.status == "created"
    fields = json.loads(fake.creates[0].content)["fields"]
    # 2026-01-01 00:00+08:00 == 2025-12-31 16:00Z; read back in Asia/Shanghai it
    # must still be 2026-01-01, which the successful verification proves.
    assert fields["日期"] == 1767196800000
    assert state_of(sessions) == "succeeded"
