"""DEV-024: family fund — top up, reconcile to a target, and never over-write.

The fake mirrors the real table's two quirks, both confirmed against it: the
create echo carries no formula value, and `家庭基金余额` is a table-wide running
total that grows by twice each recharge. So a test that reconciles to a target
exercises the exact arithmetic the tool depends on.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.family_fund import FundOutcome, update_family_fund
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.onboarding import observed_from_snapshot
from personal_data_mcp.finance.schema_validator import validate_schema
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import acquire_resource_lock
from personal_data_mcp.storage.models import ResourceLock, ToolExecution


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads((LEDGER_FIXTURES / "config.synthetic.json").read_text("utf-8"))
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text("utf-8")
)
VALIDATION = validate_schema(CONFIG, observed_from_snapshot(SNAPSHOT))
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)


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


@pytest.fixture()
def keyring():
    return KeyRing([generate_key("fund-2026")], service="personal_data_mcp")


class FakeFund:
    """A doubling running-total family-fund table."""

    def __init__(self, initial: str = "1000.00") -> None:
        self.total = Decimal(initial)
        self.recharges: list[Decimal] = []
        self.record_seq = 0
        self.by_token: dict[str, str] = {}
        self.records: dict[str, dict] = {}
        #: If set, applied to the total right after a create, to model an
        #: external concurrent change before read-back.
        self.perturb: Decimal | None = None

    def _balance_cell(self):
        return {"type": 2, "value": [float(self.total)]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t", "expire": 7200}
            )
        if request.method == "POST" and path.endswith("/search"):
            # The active ledger has an initial-balance row even before a top-up.
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"record_id": "r", "fields": {"家庭基金余额": self._balance_cell()}}]},
                },
            )
        if request.method == "POST" and path.endswith("/records"):
            token = request.url.params.get("client_token")
            if token in self.by_token:
                rid = self.by_token[token]
            else:
                recharge = Decimal(str(json.loads(request.content)["fields"]["充值金额"]))
                self.recharges.append(recharge)
                self.total += recharge * 2  # the ledger formula
                if self.perturb is not None:
                    self.total += self.perturb
                    self.perturb = None
                self.record_seq += 1
                rid = f"fund{self.record_seq:06d}"
                self.by_token[token] = rid
                self.records[rid] = copy.deepcopy(
                    json.loads(request.content)["fields"]
                )
            # The create echo carries NO formula value, like the real table.
            return httpx.Response(
                200, json={"code": 0, "data": {"record": {"record_id": rid, "fields": {}}}}
            )
        if request.method == "GET" and "/records/" in path:
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": rid,
                            "fields": {
                                **self.records[rid],
                                "家庭基金余额": self._balance_cell(),
                            },
                        }
                    },
                },
            )
        raise AssertionError(f"unexpected {request.method} {path}")


def adapter_for(fake):
    return FeishuAdapter(
        FeishuCredentials(app_id="c", app_secret="s"),
        transport=httpx.MockTransport(fake.handler),
        now=clock,
    )


async def do(fake, sessions, keyring, *, key="fund-1", **kw):
    async with adapter_for(fake) as adapter:
        return await update_family_fund(
            sessions=sessions,
            adapter=adapter,
            config=CONFIG,
            validation=VALIDATION,
            source=SOURCE,
            idempotency_key=key,
            request_fingerprint="fp",
            trace_id="t",
            keyring=keyring,
            **kw,
        )


def state_of(sessions, key="fund-1"):
    with sessions() as s:
        ex = s.get(ToolExecution, key)
        return ex.state if ex else None


# --- top up ------------------------------------------------------------------


def test_top_up_writes_the_recharge_and_reports_the_doubled_balance(
    sessions, keyring
) -> None:
    fake = FakeFund(initial="1000.00")
    out = run(do(fake, sessions, keyring, mode="top_up", recharge_amount_cny=Decimal("500.00")))
    assert isinstance(out, FundOutcome)
    assert out.status == "created"
    assert out.recharge_amount_cny == Decimal("500.00")
    # The formula doubles the recharge: 1000 + 2*500 = 2000.
    assert out.balance_after_cny == Decimal("2000.00")
    assert state_of(sessions) == "succeeded"


def test_top_up_never_writes_a_formula_or_initial_balance_field(
    sessions, keyring
) -> None:
    fake = FakeFund()
    captured = {}
    original = fake.handler

    def capture(request):
        if request.method == "POST" and request.url.path.endswith("/records"):
            captured["fields"] = json.loads(request.content)["fields"]
        return original(request)

    fake.handler = capture
    run(do(fake, sessions, keyring, mode="top_up", recharge_amount_cny=Decimal("10.00")))
    assert set(captured["fields"]) <= {"充值金额", "日期", "备注"}
    assert "实际入账" not in captured["fields"]
    assert "家庭基金余额" not in captured["fields"]
    assert "初始余额" not in captured["fields"]


def test_a_non_positive_top_up_is_refused(sessions, keyring) -> None:
    fake = FakeFund()
    with pytest.raises(AppError) as caught:
        run(do(fake, sessions, keyring, mode="top_up", recharge_amount_cny=Decimal("0")))
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert fake.recharges == []


# --- interest reconcile ------------------------------------------------------


def test_reconcile_writes_half_the_difference_and_lands_on_target(
    sessions, keyring
) -> None:
    fake = FakeFund(initial="1000.00")
    # Seed one row so a current balance is readable.
    run(do(fake, sessions, keyring, key="seed", mode="top_up", recharge_amount_cny=Decimal("0.01")))
    current = fake.total  # 1000 + 0.02 = 1000.02
    out = run(
        do(fake, sessions, keyring, mode="interest_reconcile", target_balance_cny=current + Decimal("100"))
    )
    assert out.mode == "interest_reconcile"
    # recharge = (target - current) / 2 = 50; balance += 100 -> exactly target.
    assert out.recharge_amount_cny == Decimal("50")
    assert out.balance_after_cny == current + Decimal("100")
    assert out.note == "利息补齐"


def test_reconcile_to_the_current_balance_writes_nothing(sessions, keyring) -> None:
    fake = FakeFund(initial="1000.00")
    run(do(fake, sessions, keyring, key="seed", mode="top_up", recharge_amount_cny=Decimal("5")))
    before = fake.total
    with pytest.raises(AppError) as caught:
        run(do(fake, sessions, keyring, mode="interest_reconcile", target_balance_cny=before))
    assert caught.value.code is ErrorCode.NO_CHANGE_REQUIRED
    assert fake.total == before  # nothing written


def test_reconcile_below_the_current_balance_is_refused(sessions, keyring) -> None:
    fake = FakeFund(initial="1000.00")
    run(do(fake, sessions, keyring, key="seed", mode="top_up", recharge_amount_cny=Decimal("5")))
    before = fake.total
    with pytest.raises(AppError) as caught:
        run(do(fake, sessions, keyring, mode="interest_reconcile", target_balance_cny=before - Decimal("1")))
    assert caught.value.code is ErrorCode.TARGET_BELOW_CURRENT_BALANCE
    assert fake.total == before


def test_a_concurrent_change_means_the_target_is_not_reached(sessions, keyring) -> None:
    fake = FakeFund(initial="1000.00")
    run(do(fake, sessions, keyring, key="seed", mode="top_up", recharge_amount_cny=Decimal("5")))
    current = fake.total
    # An external change perturbs the total right after our create, so the
    # post-write balance will not equal the target.
    fake.perturb = Decimal("7.00")
    with pytest.raises(AppError) as caught:
        run(do(fake, sessions, keyring, mode="interest_reconcile", target_balance_cny=current + Decimal("20")))
    assert caught.value.code is ErrorCode.SOURCE_COMMITTED_MISMATCH
    # The row was created and is kept; the tool did not write a second one.
    assert state_of(sessions) == "needs_manual_review"


def test_reconcile_uses_exact_decimal_not_cents(sessions, keyring) -> None:
    fake = FakeFund(initial="1000.00")
    run(do(fake, sessions, keyring, key="seed", mode="top_up", recharge_amount_cny=Decimal("0.01")))
    current = fake.total
    # A target 0.01 above current needs a 0.005 recharge -- three decimals.
    out = run(
        do(fake, sessions, keyring, mode="interest_reconcile", target_balance_cny=current + Decimal("0.01"))
    )
    assert out.recharge_amount_cny == Decimal("0.005")
    assert out.balance_after_cny == current + Decimal("0.01")


# --- idempotency and the lock ------------------------------------------------


def test_a_replay_returns_the_same_record_and_balance_without_recharging(
    sessions, keyring
) -> None:
    fake = FakeFund(initial="1000.00")
    first = run(do(fake, sessions, keyring, mode="top_up", recharge_amount_cny=Decimal("500")))
    total_after_first = fake.total
    second = run(do(fake, sessions, keyring, mode="top_up", recharge_amount_cny=Decimal("500")))
    assert second.status == "idempotent_replay"
    assert second.record_id == first.record_id
    assert second.balance_after_cny == first.balance_after_cny
    # No second recharge: the total did not move.
    assert fake.total == total_after_first
    assert len(fake.recharges) == 1


def test_the_lock_is_released_after_a_successful_write(sessions, keyring) -> None:
    fake = FakeFund()
    run(do(fake, sessions, keyring, mode="top_up", recharge_amount_cny=Decimal("1")))
    with sessions() as s:
        assert s.query(ResourceLock).count() == 0


def test_the_lock_is_released_even_when_no_change_is_required(sessions, keyring) -> None:
    fake = FakeFund(initial="1000.00")
    run(do(fake, sessions, keyring, key="seed", mode="top_up", recharge_amount_cny=Decimal("5")))
    with pytest.raises(AppError):
        run(do(fake, sessions, keyring, mode="interest_reconcile", target_balance_cny=fake.total))
    with sessions() as s:
        assert s.query(ResourceLock).count() == 0


def test_a_source_not_bound_to_the_validated_config_is_refused(
    sessions, keyring
) -> None:
    fake = FakeFund()
    wrong = BaseSource(
        base_token="bas_other",
        ledger_kind="production",
        tables=SOURCE.tables,
    )

    async def scenario():
        async with adapter_for(fake) as adapter:
            return await update_family_fund(
                mode="top_up",
                recharge_amount_cny=Decimal("10"),
                sessions=sessions,
                adapter=adapter,
                config=CONFIG,
                validation=VALIDATION,
                source=wrong,
                idempotency_key="wrong-source",
                request_fingerprint="fp",
                trace_id="t",
                keyring=keyring,
            )

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED
    assert fake.recharges == []


def test_a_stale_family_fund_schema_validation_is_refused(
    sessions, keyring
) -> None:
    fake = FakeFund()

    async def scenario():
        async with adapter_for(fake) as adapter:
            return await update_family_fund(
                mode="top_up",
                recharge_amount_cny=Decimal("10"),
                sessions=sessions,
                adapter=adapter,
                config=CONFIG,
                validation=replace(
                    VALIDATION, config_checksum="stale-checksum"
                ),
                source=SOURCE,
                idempotency_key="stale-schema",
                request_fingerprint="fp",
                trace_id="t",
                keyring=keyring,
            )

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED
    assert fake.recharges == []


def test_a_silently_ignored_recharge_never_reports_success(
    sessions, keyring
) -> None:
    fake = FakeFund()
    original = fake.handler

    def ignore_create(request):
        if request.method == "POST" and request.url.path.endswith("/records"):
            token = request.url.params.get("client_token")
            fake.record_seq += 1
            rid = f"fund{fake.record_seq:06d}"
            fake.by_token[token] = rid
            fake.records[rid] = {}
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"record": {"record_id": rid, "fields": {}}},
                },
            )
        return original(request)

    fake.handler = ignore_create
    with pytest.raises(AppError) as caught:
        run(
            do(
                fake,
                sessions,
                keyring,
                mode="top_up",
                recharge_amount_cny=Decimal("500"),
            )
        )
    assert caught.value.code is ErrorCode.SOURCE_COMMITTED_MISMATCH
    assert state_of(sessions) == "needs_manual_review"


def test_success_and_the_sealed_replay_result_are_atomic(
    sessions, keyring
) -> None:
    fake = FakeFund()
    first = run(
        do(
            fake,
            sessions,
            keyring,
            mode="top_up",
            recharge_amount_cny=Decimal("20"),
        )
    )
    with sessions() as session:
        execution = session.get(ToolExecution, "fund-1")
        assert execution.state == "succeeded"
        assert execution.encrypted_result is not None

    replay = run(
        do(
            fake,
            sessions,
            keyring,
            mode="top_up",
            recharge_amount_cny=Decimal("20"),
        )
    )
    assert replay.balance_after_cny == first.balance_after_cny


def test_a_legacy_success_without_a_sealed_result_fails_closed(
    sessions, keyring
) -> None:
    fake = FakeFund()
    run(
        do(
            fake,
            sessions,
            keyring,
            mode="top_up",
            recharge_amount_cny=Decimal("20"),
        )
    )
    with sessions() as session:
        session.get(ToolExecution, "fund-1").encrypted_result = None
        session.commit()

    with pytest.raises(AppError) as caught:
        run(
            do(
                fake,
                sessions,
                keyring,
                mode="top_up",
                recharge_amount_cny=Decimal("20"),
            )
        )
    assert caught.value.code is ErrorCode.SOURCE_COMMITTED_MISMATCH
    assert len(fake.recharges) == 1


def test_the_family_fund_lease_survives_more_than_the_old_30_seconds(
    sessions, keyring
) -> None:
    fake = FakeFund()
    moment = [datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)]
    original = fake.handler

    def advance_after_balance_read(request):
        response = original(request)
        if request.method == "POST" and request.url.path.endswith("/search"):
            moment[0] += timedelta(seconds=31)
        return response

    fake.handler = advance_after_balance_read
    outcome = run(
        do(
            fake,
            sessions,
            keyring,
            mode="top_up",
            recharge_amount_cny=Decimal("1"),
            now=lambda: moment[0],
        )
    )
    assert outcome.status == "created"


def test_a_live_foreign_lock_is_not_released_by_this_invocation(
    sessions, keyring
) -> None:
    fake = FakeFund()
    moment = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    with sessions() as session:
        assert acquire_resource_lock(
            session,
            lock_key=f"family_fund:{CONFIG.ledger_year}",
            owner="family-fund:other-worker",
            now=moment,
            seconds=120,
        )
        session.commit()

    with pytest.raises(AppError) as caught:
        run(
            do(
                fake,
                sessions,
                keyring,
                mode="top_up",
                recharge_amount_cny=Decimal("1"),
                owner="family-fund",
                now=lambda: moment,
            )
        )
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE
    with sessions() as session:
        lock = session.get(
            ResourceLock, f"family_fund:{CONFIG.ledger_year}"
        )
        assert lock.owner == "family-fund:other-worker"


def test_an_unknown_family_fund_write_blocks_the_next_operation(
    sessions, keyring
) -> None:
    fake = FakeFund()
    original = fake.handler
    failures = [1]

    def fail_first_create(request):
        if (
            failures[0]
            and request.method == "POST"
            and request.url.path.endswith("/records")
        ):
            failures[0] -= 1
            raise httpx.ConnectTimeout("unknown create")
        return original(request)

    fake.handler = fail_first_create
    with pytest.raises(AppError) as first:
        run(
            do(
                fake,
                sessions,
                keyring,
                key="fund-unknown",
                mode="top_up",
                recharge_amount_cny=Decimal("10"),
            )
        )
    assert first.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN

    with pytest.raises(AppError) as second:
        run(
            do(
                fake,
                sessions,
                keyring,
                key="fund-next",
                mode="top_up",
                recharge_amount_cny=Decimal("5"),
            )
        )
    assert second.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN
    assert fake.recharges == []
