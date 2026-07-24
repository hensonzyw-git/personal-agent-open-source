"""DEV-024: the income write reuses the crash-safe skeleton, with income rules.

The point of these cases is that income carries none of the expense semantics —
positive only, no family field, policy-decided name and category — while still
getting the same read-back-is-success guarantee. The mock Feishu records what it
was asked to write so the payload can be checked directly.
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
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.income_policy import IncomeClarification
from personal_data_mcp.finance.income_write import write_income
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.schema_validator import validate_schema
from personal_data_mcp.finance.onboarding import observed_from_snapshot
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import ToolExecution


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads((LEDGER_FIXTURES / "config.synthetic.json").read_text("utf-8"))
)
SNAPSHOT = json.loads((LEDGER_FIXTURES / "snapshot.synthetic.json").read_text("utf-8"))
VALIDATION = validate_schema(CONFIG, observed_from_snapshot(SNAPSHOT))
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)
DAY = date(2026, 7, 23)


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


class FakeIncome:
    def __init__(self) -> None:
        self.creates: list[httpx.Request] = []
        self.records: dict[str, dict] = {}
        self.next_id = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t", "expire": 7200}
            )
        if request.method == "POST" and path.endswith("/records"):
            self.creates.append(request)
            fields = json.loads(request.content)["fields"]
            rid = f"inc{self.next_id:06d}"
            self.next_id += 1
            self.records[rid] = copy.deepcopy(fields)
            return httpx.Response(
                200, json={"code": 0, "data": {"record": {"record_id": rid, "fields": fields}}}
            )
        if request.method == "GET" and "/records/" in path:
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={"code": 0, "data": {"record": {"record_id": rid, "fields": self.records[rid]}}},
            )
        raise AssertionError(f"unexpected {request.method} {path}")


def adapter_for(fake):
    return FeishuAdapter(
        FeishuCredentials(app_id="c", app_secret="s"),
        transport=httpx.MockTransport(fake.handler),
        now=clock,
    )


async def do(fake, sessions, *, description, amount="30000.00", key="inc-1"):
    async with adapter_for(fake) as adapter:
        return await write_income(
            description=description,
            amount_cny=Decimal(amount),
            occurred_on=DAY,
            sessions=sessions,
            adapter=adapter,
            config=CONFIG,
            validation=VALIDATION,
            source=SOURCE,
            idempotency_key=key,
            request_fingerprint="fp",
            trace_id="t",
        )


def state_of(sessions, key="inc-1"):
    with sessions() as s:
        ex = s.get(ToolExecution, key)
        return ex.state if ex else None


def test_salary_income_writes_the_policy_name_and_category(sessions) -> None:
    fake = FakeIncome()
    outcome = run(do(fake, sessions, description="今天发工资"))
    assert outcome.status == "created"
    assert state_of(sessions) == "succeeded"
    fields = json.loads(fake.creates[0].content)["fields"]
    assert set(fields) == {"金额", "名称", "日期", "分类"}
    assert fields["名称"] == "工资"
    assert fields["分类"] == "工资"
    assert fields["金额"] == 30000.0
    # No expense-only field is ever present on the income table.
    assert "是否家庭支出" not in fields


def test_other_income_keeps_its_subject(sessions) -> None:
    fake = FakeIncome()
    outcome = run(do(fake, sessions, description="公积金入账", amount="2500.00"))
    assert outcome.status == "created"
    fields = json.loads(fake.creates[0].content)["fields"]
    assert fields["名称"] == "公积金"
    assert fields["分类"] == "其他"


def test_a_description_with_no_subject_writes_nothing(sessions) -> None:
    fake = FakeIncome()
    outcome = run(do(fake, sessions, description="到账了"))
    assert outcome is IncomeClarification.NO_SUBJECT
    assert fake.creates == []
    assert state_of(sessions) is None


def test_a_negative_income_amount_is_refused_before_any_write(sessions) -> None:
    fake = FakeIncome()
    with pytest.raises(AppError) as caught:
        run(do(fake, sessions, description="工资", amount="-1.00"))
    # money.parse rejects the sign upstream; either way nothing is written.
    assert fake.creates == []
    assert state_of(sessions) is None
    assert caught.value.code in (
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.SOURCE_SCHEMA_CHANGED,
    )


def test_a_replay_writes_nothing_new(sessions) -> None:
    fake = FakeIncome()
    first = run(do(fake, sessions, description="工资"))
    second = run(do(fake, sessions, description="工资"))
    assert second.status == "idempotent_replay"
    assert second.record_id == first.record_id
    assert len(fake.creates) == 1


def test_a_read_back_mismatch_is_manual_review(sessions) -> None:
    fake = FakeIncome()

    def corrupt(request):
        resp = FakeIncome.handler(fake, request)
        if request.method == "POST" and request.url.path.endswith("/records"):
            for rid in fake.records:
                fake.records[rid]["金额"] = 999
        return resp

    fake.handler = corrupt
    with pytest.raises(AppError) as caught:
        run(do(fake, sessions, description="工资"))
    assert caught.value.code is ErrorCode.SOURCE_COMMITTED_MISMATCH
    assert state_of(sessions) == "needs_manual_review"
