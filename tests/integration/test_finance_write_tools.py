"""DEV-027: the Finance write tools as real MCP handlers.

Everything runs through `dispatch`, so each case exercises the same path a
governed call takes: the Host Context gate, the model-input schema, the handler,
and the outward result shape. A fake Bitable stands in for Feishu and counts
what left the process, because "how many creates happened" is the only claim
that matters after a refusal.

The cases are chosen from the failure modes, not from the happy path:

- a duplicate must refuse with a *bare* code, and the `duplicate_check_id` must
  not appear anywhere on the model-facing wire;
- the override must work only from the signed Host Context -- an argument-level
  one is stripped, and a header without a matching claim is refused;
- a clarification, a drifted schema and an unavailable FX rate must each leave
  the ledger untouched and no execution row behind;
- a converted amount must be stored as CNY with the original expression kept in
  the name, after the trip tag, with the quote recorded exactly once.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from fixtures.service_keys import SignedCaller
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import ErrorCode
from personal_agent_core.manifest import load_manifest
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.server.app import dispatch
from personal_data_mcp.server.finance_write import (
    FinanceWriteDependencies,
    build_expense_handler,
    build_family_fund_handler,
    build_income_handler,
)
from personal_data_mcp.server.handlers import ToolRegistry
from personal_data_mcp.server.meta import build_handler as build_meta_handler
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import (
    DuplicateCheck,
    FxEvidence,
    ToolExecution,
)


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(encoding="utf-8")
)
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)
NOW = datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc)
SCOPES = (
    "finance.expense.write",
    "finance.income.write",
    "finance.family_fund.write",
    "meta.capabilities.read",
)
LUNCH = {
    "name": "午饭",
    "input_amount": "20.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-24",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}
TABLE_BY_ID = {table_id: kind for kind, table_id in SOURCE.tables.items()}


def run(coro):
    return asyncio.run(coro)


def clock() -> float:
    return 1000.0


def now() -> datetime:
    return NOW


class FakeBitable:
    """A Bitable that lists fields, searches, creates and reads back."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict]] = {
            kind: {} for kind in SOURCE.tables
        }
        self.creates: list[httpx.Request] = []
        self.next_id = 1
        self.fields = copy.deepcopy(SNAPSHOT)

    def add_row(self, kind: str, fields: dict) -> str:
        record_id = f"rec{self.next_id:06d}"
        self.next_id += 1
        self.rows[kind][record_id] = fields
        return record_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        kind = self._table_of(path)
        if request.method == "GET" and path.endswith("/fields"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": self.fields[kind], "has_more": False},
                },
            )
        if request.method == "POST" and path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"record_id": rid, "fields": fields}
                            for rid, fields in self.rows[kind].items()
                        ],
                        "has_more": False,
                    },
                },
            )
        if request.method == "POST" and path.endswith("/records"):
            self.creates.append(request)
            fields = json.loads(request.content)["fields"]
            stored = copy.deepcopy(fields)
            if kind == "family_fund":
                # The real table's balance is a formula that doubles the
                # recharge; a fake that stored only what was sent would make the
                # write path's own balance verification vacuous.
                stored["家庭基金余额"] = {
                    "type": 2,
                    "value": [
                        float(
                            self._balance()
                            + Decimal(str(fields["充值金额"])) * 2
                        )
                    ],
                }
            record_id = self.add_row(kind, stored)
            fields = stored
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {"record_id": record_id, "fields": fields}
                    },
                },
            )
        if request.method == "GET" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": record_id,
                            "fields": self.rows[kind][record_id],
                        }
                    },
                },
            )
        raise AssertionError(f"unexpected call {request.method} {path}")

    def _balance(self) -> Decimal:
        rows = list(self.rows["family_fund"].values())
        if not rows:
            return Decimal("0")
        return Decimal(str(rows[-1]["家庭基金余额"]["value"][0]))

    def _table_of(self, path: str) -> str:
        for table_id, kind in TABLE_BY_ID.items():
            if table_id in path:
                return kind
        raise AssertionError(f"unknown table in {path}")


def fx_transport(rate: str | None = "0.04141", status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.frankfurter.dev"
        if rate is None:
            return httpx.Response(status, json={"error": "no"})
        return httpx.Response(
            status,
            json={
                "amount": 1.0,
                "base": request.url.params.get("base"),
                "date": "2026-07-23",
                "rates": {"CNY": float(rate)},
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing([generate_key("dup-2026-01")], service="personal_data_mcp")


@pytest.fixture()
def caller() -> SignedCaller:
    return SignedCaller(scopes=SCOPES)


def dependencies(fake, sessions, keyring, *, adapter, fx) -> FinanceWriteDependencies:
    return FinanceWriteDependencies(
        adapter=adapter,
        source=SOURCE,
        config=CONFIG,
        sessions=sessions,
        keyring=keyring,
        fx=fx,
        now=now,
    )


def registry_for(deps) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))
    registry.register("finance.log_expense", build_expense_handler(deps))
    registry.register("finance.log_income", build_income_handler(deps))
    registry.register(
        "finance.update_family_fund", build_family_fund_handler(deps)
    )
    return registry


async def call(
    fake: FakeBitable,
    sessions,
    keyring,
    caller: SignedCaller,
    *,
    tool: str = "finance.log_expense",
    arguments: dict | None = None,
    idempotency_key: str | None = None,
    duplicate_override: str | None = None,
    fx_rate: str | None = "0.04141",
    headers: dict | None = None,
):
    arguments = LUNCH if arguments is None else arguments
    key = idempotency_key or str(uuid.uuid4())
    async with FeishuAdapter(
        FeishuCredentials(app_id="cli", app_secret="s"),
        transport=httpx.MockTransport(fake.handler),
        now=clock,
    ) as adapter:
        async with FxConnector(
            now=lambda: NOW, transport=fx_transport(fx_rate)
        ) as fx:
            deps = dependencies(
                fake, sessions, keyring, adapter=adapter, fx=fx
            )
            registry = registry_for(deps)
            sent = headers or caller.headers(
                tool,
                arguments,
                host=caller.host_context(
                    tool,
                    idempotency_key=key,
                    duplicate_override=duplicate_override,
                ),
            )
            # The transport guard lower-cases headers before dispatch sees them.
            result = await dispatch(
                registry,
                caller.authorizer(),
                tool,
                arguments,
                {key.lower(): value for key, value in sent.items()},
            )
    return result, key


def error_code(result) -> str:
    assert result.isError
    return json.loads(result.content[0].text)["error"]["code"]


def payload(result) -> dict:
    assert not result.isError, result.content[0].text
    return result.structuredContent


def contract(name: str) -> dict:
    for entry in load_manifest()["tools"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(name)


def executions(sessions) -> list[ToolExecution]:
    with sessions() as session:
        return list(session.scalars(select(ToolExecution)))


def checks(sessions) -> list[DuplicateCheck]:
    with sessions() as session:
        return list(session.scalars(select(DuplicateCheck)))


# --- the write itself --------------------------------------------------------


def test_an_expense_is_written_and_matches_the_frozen_output_schema(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    result, key = run(call(fake, sessions, keyring, caller))

    body = payload(result)
    Draft202012Validator(
        contract("finance.log_expense")["output_schema"],
        format_checker=FormatChecker(),
    ).validate(body)
    assert body["status"] == "created"
    assert body["evidence"] == {
        "kind": "feishu_record",
        "external_id": body["record_id"],
    }
    assert len(fake.creates) == 1
    assert [e.state for e in executions(sessions)] == ["succeeded"]
    written = json.loads(fake.creates[0].content)["fields"]
    assert written["名称"] == "午饭"
    assert Decimal(str(written["原始金额"])) == Decimal("20.00")


def test_replaying_the_same_key_writes_nothing_further(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    first, key = run(call(fake, sessions, keyring, caller))
    second, _ = run(call(fake, sessions, keyring, caller, idempotency_key=key))

    assert payload(second)["status"] == "idempotent_replay"
    assert payload(second)["record_id"] == payload(first)["record_id"]
    # A replay reports the receipt, not what this call happened to resolve.
    assert payload(second)["record"] == {}
    assert len(fake.creates) == 1


# --- the duplicate gate, and the channel the id travels on -------------------


def test_a_duplicate_refuses_bare_and_never_names_the_check_on_the_wire(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    run(call(fake, sessions, keyring, caller))
    result, key = run(call(fake, sessions, keyring, caller))

    assert error_code(result) == ErrorCode.POSSIBLE_DUPLICATE.value
    raised = checks(sessions)
    assert len(raised) == 1
    assert raised[0].idempotency_key == key
    wire = result.content[0].text
    assert raised[0].check_id not in wire
    # Still exactly one create, and the blocked attempt left no execution row.
    assert len(fake.creates) == 1
    assert len(executions(sessions)) == 1


def test_the_signed_override_releases_exactly_that_check(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    run(call(fake, sessions, keyring, caller))
    blocked, _ = run(call(fake, sessions, keyring, caller))
    assert error_code(blocked) == ErrorCode.POSSIBLE_DUPLICATE.value
    check_id = checks(sessions)[0].check_id

    released, _ = run(
        call(fake, sessions, keyring, caller, duplicate_override=check_id)
    )
    assert payload(released)["status"] == "created"
    assert len(fake.creates) == 2

    # Spent: the same id cannot authorise a third write. It is refused as a
    # duplicate, not as an internal error, and the wire still names no check.
    again, _ = run(
        call(fake, sessions, keyring, caller, duplicate_override=check_id)
    )
    assert error_code(again) == ErrorCode.POSSIBLE_DUPLICATE.value
    assert check_id not in again.content[0].text
    assert len(fake.creates) == 2


def test_a_forged_override_is_refused_as_a_duplicate(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    run(call(fake, sessions, keyring, caller))
    forged = "00000000-0000-4000-8000-000000000000"

    result, _ = run(
        call(fake, sessions, keyring, caller, duplicate_override=forged)
    )

    assert error_code(result) == ErrorCode.POSSIBLE_DUPLICATE.value
    assert len(fake.creates) == 1
    assert len(executions(sessions)) == 1


def test_an_override_supplied_as_a_model_argument_does_not_release(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    run(call(fake, sessions, keyring, caller))
    run(call(fake, sessions, keyring, caller))
    check_id = checks(sessions)[0].check_id

    hostile = {**LUNCH, "duplicate_override": check_id}
    result, _ = run(call(fake, sessions, keyring, caller, arguments=hostile))

    # Refused at the raw MCP boundary as a Host-only field, and in any case the
    # gate would still have blocked it.
    assert error_code(result) == ErrorCode.HOST_CONTEXT_MISMATCH.value
    assert len(fake.creates) == 1


def test_an_override_header_without_a_signed_claim_is_refused(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    run(call(fake, sessions, keyring, caller))
    run(call(fake, sessions, keyring, caller))
    check_id = checks(sessions)[0].check_id

    # A token signed for a plain call, with the header bolted on afterwards.
    headers = caller.headers("finance.log_expense", LUNCH)
    headers["X-Duplicate-Override"] = check_id
    result, _ = run(call(fake, sessions, keyring, caller, headers=headers))

    assert error_code(result) == ErrorCode.HOST_CONTEXT_MISMATCH.value
    assert len(fake.creates) == 1


def test_a_signed_override_without_the_header_is_refused(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    run(call(fake, sessions, keyring, caller))
    run(call(fake, sessions, keyring, caller))
    check_id = checks(sessions)[0].check_id

    headers = caller.headers(
        "finance.log_expense",
        LUNCH,
        host=caller.host_context(
            "finance.log_expense", duplicate_override=check_id
        ),
    )
    headers.pop("X-Duplicate-Override")
    result, _ = run(call(fake, sessions, keyring, caller, headers=headers))

    assert error_code(result) == ErrorCode.HOST_CONTEXT_MISMATCH.value
    assert len(fake.creates) == 1


# --- questions and refusals write nothing ------------------------------------


def test_an_ambiguous_trip_asks_and_writes_nothing(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    for tag in ("东京01", "东京02"):
        fake.add_row(
            "expense",
            {
                "名称": f"机票 #{tag}",
                "原始金额": 2000,
                "日期": 1753286400000,
                "分类": "旅行",
                "是否家庭支出": True,
            },
        )
    arguments = {
        "name": "打车",
        "input_amount": "100",
        "input_currency": "CNY",
        "occurred_on": "2026-07-24",
        "is_family_expense": False,
        "entry_kind": "expense",
        "category": "旅行",
        "trip_tag": "东京",
    }
    result, _ = run(
        call(fake, sessions, keyring, caller, arguments=arguments)
    )

    assert error_code(result) == ErrorCode.CLARIFICATION_REQUIRED.value
    assert fake.creates == []
    assert executions(sessions) == []


def test_a_sole_matching_trip_is_reused_and_tagged_in_the_name(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    fake.add_row(
        "expense",
        {
            "名称": "机票 #东京",
            "原始金额": 2000,
            "日期": 1753286400000,
            "分类": "旅行",
            "是否家庭支出": True,
        },
    )
    arguments = {
        "name": "酒店",
        "input_amount": "1500",
        "input_currency": "CNY",
        "occurred_on": "2026-07-24",
        "is_family_expense": True,
        "entry_kind": "expense",
        "category": "旅行",
        "trip_tag": "东京",
    }
    result, _ = run(call(fake, sessions, keyring, caller, arguments=arguments))

    assert payload(result)["record"]["name"] == "酒店 #东京"


def test_a_drifted_schema_refuses_before_any_create(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    for field in fake.fields["expense"]:
        if field["field_name"] == "名称":
            field["field_name"] = "名稱"
    result, _ = run(call(fake, sessions, keyring, caller))

    assert error_code(result) == ErrorCode.SOURCE_SCHEMA_CHANGED.value
    assert fake.creates == []
    assert executions(sessions) == []


# --- FX runs inside the call, and only when it is needed ---------------------


def test_a_foreign_amount_is_converted_suffixed_and_evidenced(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    arguments = {
        "name": "机票",
        "input_amount": "10000",
        "input_currency": "JPY",
        "occurred_on": "2026-07-24",
        "is_family_expense": True,
        "entry_kind": "expense",
        "category": "旅行",
        "trip_tag": "东京",
    }
    result, key = run(call(fake, sessions, keyring, caller, arguments=arguments))

    body = payload(result)
    # 10000 JPY * 0.04141 = 414.10, and the suffix lands after the trip tag.
    assert body["record"]["amount_cny"] == "414.10"
    assert body["record"]["name"] == "机票 #东京（10,000 JPY）"
    with sessions() as session:
        evidence = list(session.scalars(select(FxEvidence)))
    assert len(evidence) == 1
    assert evidence[0].idempotency_key == key
    assert evidence[0].rate == "0.04141"
    assert evidence[0].base_currency == "JPY"


def test_an_unavailable_rate_refuses_the_write(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    arguments = {
        "name": "机票",
        "input_amount": "10000",
        "input_currency": "JPY",
        "occurred_on": "2026-07-24",
        "is_family_expense": True,
        "entry_kind": "expense",
        "category": "旅行",
        "trip_tag": "东京",
    }
    result, _ = run(
        call(
            fake,
            sessions,
            keyring,
            caller,
            arguments=arguments,
            fx_rate=None,
        )
    )

    assert error_code(result) == ErrorCode.FX_RATE_UNAVAILABLE.value
    assert fake.creates == []
    assert executions(sessions) == []


def test_a_settled_cny_amount_never_queries_a_rate(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    arguments = {
        **LUNCH,
        "input_amount": "10000",
        "input_currency": "JPY",
        "settlement_amount_cny": "400.00",
    }
    # `fx_rate=None` would fail the request if a rate were fetched at all.
    result, _ = run(
        call(
            fake, sessions, keyring, caller, arguments=arguments, fx_rate=None
        )
    )

    body = payload(result)
    assert body["record"]["amount_cny"] == "400.00"
    assert body["record"]["name"] == "午饭"
    with sessions() as session:
        assert list(session.scalars(select(FxEvidence))) == []


# --- income and family fund share the same seam ------------------------------


def test_income_writes_through_the_closed_policy(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    result, _ = run(
        call(
            fake,
            sessions,
            keyring,
            caller,
            tool="finance.log_income",
            arguments={
                "income_description": "发工资",
                "input_amount": "100",
                "input_currency": "CNY",
                "occurred_on": "2026-07-24",
            },
        )
    )

    body = payload(result)
    Draft202012Validator(
        contract("finance.log_income")["output_schema"],
        format_checker=FormatChecker(),
    ).validate(body)
    written = json.loads(fake.creates[0].content)["fields"]
    assert written["名称"] == "工资"
    assert written["分类"] == "工资"


def test_a_family_fund_top_up_reports_both_balances(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    fake.add_row(
        "family_fund",
        {"充值金额": 100, "家庭基金余额": {"type": 2, "value": [1000]}},
    )
    result, _ = run(
        call(
            fake,
            sessions,
            keyring,
            caller,
            tool="finance.update_family_fund",
            arguments={"mode": "top_up", "recharge_amount_cny": "10"},
        )
    )

    body = payload(result)
    Draft202012Validator(
        contract("finance.update_family_fund")["output_schema"],
        format_checker=FormatChecker(),
    ).validate(body)
    assert body["mode"] == "top_up"
    assert body["recharge_amount_cny"] == "10"
    assert body["balance_before_cny"] == "1000"


def test_the_disabled_batch_tool_cannot_be_registered(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()

    async def scenario():
        async with FeishuAdapter(
            FeishuCredentials(app_id="cli", app_secret="s"),
            transport=httpx.MockTransport(fake.handler),
            now=clock,
        ) as adapter:
            async with FxConnector(
                now=lambda: NOW, transport=fx_transport()
            ) as fx:
                deps = dependencies(
                    fake, sessions, keyring, adapter=adapter, fx=fx
                )
                registry = registry_for(deps)
                assert "finance.log_expense_batch" not in registry.names()
                names = {entry["name"] for entry in registry.catalog()}
                return names

    assert "finance.log_expense_batch" not in run(scenario())


# --- the composition root refuses before a socket exists ---------------------


def test_a_non_synthetic_ledger_config_is_refused_at_composition(
    tmp_path: Path,
) -> None:
    """Production is a G5 decision, not a command-line flag."""
    from personal_data_mcp.feishu.base_source import LedgerSourceError
    from personal_data_mcp.server.composition import load_protected_config

    document = json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
    document["ledger_kind"] = "production"
    path = tmp_path / "ledger.production.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LedgerSourceError):
        load_protected_config(path)


def test_the_default_server_advertises_no_finance_tool() -> None:
    """Without composition, an enabled contract is still not executable."""
    from personal_data_mcp.server.app import build_registry

    names = build_registry().names()
    assert names == {"meta.capabilities"}
