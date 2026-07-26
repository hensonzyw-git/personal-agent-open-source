"""DEV-028 slice A: the composed read behind the current-value control route.

The route decides *whether* a record may be read; this is what actually reads
it. The failure that matters is the quiet one: the record's cells are addressed
by field *name*, so if the live schema has drifted, the same names may now sit
on different columns. The reader must refuse in that case rather than return a
card of empty fields, which is exactly what the write path does for the same
reason.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.server.finance_write import FinanceWriteDependencies
from personal_data_mcp.server.record_reader import build_record_reader
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads((LEDGER_FIXTURES / "config.synthetic.json").read_text("utf-8"))
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text("utf-8")
)
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)
TABLE_BY_ID = {table_id: kind for kind, table_id in SOURCE.tables.items()}
NOW = datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc)
# 2026-07-23 22:00 Asia/Shanghai.
OCCURRED_MILLIS = 1784815200000


def run(coro):
    return asyncio.run(coro)


class FakeLedger:
    """Just enough Bitable to list fields and read one record back."""

    def __init__(self) -> None:
        self.fields = copy.deepcopy(SNAPSHOT)
        self.records: dict[str, dict] = {
            "recLunch": {
                "原始金额": "20",
                "名称": "午饭",
                "日期": OCCURRED_MILLIS,
                "是否家庭支出": False,
                "分类": "餐饮",
                "个人支出": {"type": 2, "value": [20]},
            }
        }
        self.reads: list[str] = []
        self.field_reads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        kind = next(k for tid, k in TABLE_BY_ID.items() if tid in path)
        if request.method == "GET" and path.endswith("/fields"):
            self.field_reads += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": self.fields[kind], "has_more": False},
                },
            )
        if request.method == "GET" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            self.reads.append(record_id)
            record = self.records.get(record_id)
            if record is None:
                return httpx.Response(200, json={"code": 0, "data": {}})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {"record_id": record_id, "fields": record}
                    },
                },
            )
        raise AssertionError(f"unexpected call {request.method} {path}")


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


async def read_many(fake: FakeLedger, sessions, records: list[tuple[str, str]]):
    async with FeishuAdapter(
        FeishuCredentials(app_id="cli", app_secret="s"),
        transport=httpx.MockTransport(fake.handler),
        now=lambda: 1000.0,
    ) as adapter:
        async with FxConnector(
            now=lambda: NOW, transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as fx:
            deps = FinanceWriteDependencies(
                adapter=adapter,
                source=SOURCE,
                config=CONFIG,
                sessions=sessions,
                keyring=KeyRing(
                    [generate_key("dup-2026-01")], service="personal_data_mcp"
                ),
                fx=fx,
                now=lambda: NOW,
            )
            return await build_record_reader(deps)(records)


async def read(fake: FakeLedger, sessions, *, table_kind: str, record_id: str):
    result = (await read_many(fake, sessions, [(table_kind, record_id)]))[0]
    if result["status"] != "found":
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="record unavailable",
        )
    return result["record"]


def test_the_current_values_come_back_projected(sessions) -> None:
    fake = FakeLedger()

    view = run(read(fake, sessions, table_kind="expense", record_id="recLunch"))

    assert view["record_id"] == "recLunch"
    assert view["values"]["name"] == "午饭"
    assert view["values"]["amount"] == "20.00"
    assert view["values"]["occurred_on"] == "2026-07-23"
    assert view["unreadable_fields"] == []
    assert fake.reads == ["recLunch"]


def test_a_manual_correction_in_feishu_is_what_the_card_shows(sessions) -> None:
    """The whole point of reading live: the card must not show the write-time value."""
    fake = FakeLedger()
    fake.records["recLunch"]["原始金额"] = "35.5"
    fake.records["recLunch"]["名称"] = "午饭（改）"

    view = run(read(fake, sessions, table_kind="expense", record_id="recLunch"))

    assert view["values"]["amount"] == "35.50"
    assert view["values"]["name"] == "午饭（改）"


def test_a_card_batch_validates_the_schema_only_once(sessions) -> None:
    fake = FakeLedger()
    fake.records["recDinner"] = dict(fake.records["recLunch"])

    results = run(
        read_many(
            fake,
            sessions,
            [("expense", "recLunch"), ("expense", "recDinner")],
        )
    )

    assert [result["status"] for result in results] == ["found", "found"]
    assert fake.reads == ["recLunch", "recDinner"]
    assert fake.field_reads == len(SOURCE.tables)


def test_a_drifted_schema_refuses_instead_of_showing_empty_fields(sessions) -> None:
    fake = FakeLedger()
    for field in fake.fields["expense"]:
        if field["field_name"] == "名称":
            field["field_name"] = "名称2"

    with pytest.raises(AppError) as caught:
        run(read(fake, sessions, table_kind="expense", record_id="recLunch"))

    assert caught.value.code is ErrorCode.SOURCE_SCHEMA_CHANGED
    # The record was never fetched: the refusal happens before the read.
    assert fake.reads == []


def test_a_table_kind_outside_the_config_is_refused(sessions) -> None:
    fake = FakeLedger()

    with pytest.raises(AppError) as caught:
        run(read(fake, sessions, table_kind="wardrobe", record_id="recLunch"))

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert fake.reads == []


def test_a_response_without_a_record_is_unavailable_not_empty(sessions) -> None:
    fake = FakeLedger()

    with pytest.raises(AppError) as caught:
        run(read(fake, sessions, table_kind="expense", record_id="recGone"))

    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE
