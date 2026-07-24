"""DEV-024: family fund — top up, reconcile to a target, and never over-write.

The fake mirrors the real table's two quirks, both confirmed against it: the
create echo carries no formula value, and `家庭基金余额` is a table-wide running
total that grows by twice each recharge. So a test that reconciles to a target
exercises the exact arithmetic the tool depends on.
"""

from __future__ import annotations

import asyncio
import json
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
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import ResourceLock, ToolExecution


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads((LEDGER_FIXTURES / "config.synthetic.json").read_text("utf-8"))
)
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
            # Balance read: one row carrying the current total (or none).
            if not self.recharges:
                return httpx.Response(200, json={"code": 0, "data": {"items": []}})
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
            # The create echo carries NO formula value, like the real table.
            return httpx.Response(
                200, json={"code": 0, "data": {"record": {"record_id": rid, "fields": {}}}}
            )
        if request.method == "GET" and "/records/" in path:
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={"code": 0, "data": {"record": {"record_id": rid, "fields": {"家庭基金余额": self._balance_cell()}}}},
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
