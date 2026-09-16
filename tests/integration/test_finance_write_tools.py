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
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from fixtures.service_keys import SignedCaller
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import ErrorCode
from personal_agent_core.timeutil import ledger_day_epoch_millis
from personal_agent_core.manifest import load_manifest
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.server.app import dispatch
from personal_data_mcp.server.finance_write import (
    FinanceWriteDependencies,
    build_category_update_handler,
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
from write_switch_fixtures import shared_enabled_write_switch


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
        self.updates: list[httpx.Request] = []
        #: Injected side effect on the stored row *after* an update lands. This
        #: is how a concurrent Base automation, or a provider that touched more
        #: than it was asked to, is exercised against the read-back verifier.
        self.on_update: Callable[[dict], None] | None = None
        self.next_id = 1
        self.fields = copy.deepcopy(SNAPSHOT)
        #: How this Base answers the 个人支出 formula on read-back, as the raw
        #: cell. Overridable so a test can supply an envelope shape this build
        #: does not recognise, or no formula cell at all.
        self.personal_spend_formula: Callable[[Decimal], object] | None = (
            lambda amount: {"type": 2, "value": [float(amount - Decimal("1.11"))]}
        )

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
            if kind == "expense" and self.personal_spend_formula is not None:
                # 个人支出 is a Base formula, so the connector never sends it and
                # only ever reads it back. A fake that stored just what was sent
                # would let the `G1` receipt field pass vacuously.
                #
                # The arithmetic here is deliberately *not* the real ledger's
                # sharing rule -- this side does not know it, and inventing one
                # in a test is how an invented rule later reads as documented.
                # It is an offset no local computation would arrive at, so a
                # test asserting this number proves the value was carried
                # through from the read-back rather than recomputed.
                stored["个人支出"] = self.personal_spend_formula(
                    Decimal(str(fields["原始金额"]))
                )
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
        if request.method == "PUT" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            self.updates.append(request)
            if record_id not in self.rows[kind]:
                # Bitable answers a non-zero code for an unknown record, and the
                # adapter maps every non-zero code to SOURCE_UNAVAILABLE.
                return httpx.Response(200, json={"code": 1254043, "msg": "x"})
            sent = json.loads(request.content)["fields"]
            # Bitable's update is *partial*: named fields change, the rest of
            # the row is untouched. A fake that replaced the row would make the
            # single-field payload look load-bearing when it was not, and the
            # "an amount cannot be rewritten" property would pass vacuously.
            self.rows[kind][record_id].update(copy.deepcopy(sent))
            if self.on_update is not None:
                self.on_update(self.rows[kind][record_id])
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
        if request.method == "GET" and "/records/" in path:
            record_id = path.rsplit("/", 1)[1]
            if record_id not in self.rows[kind]:
                return httpx.Response(200, json={"code": 1254043, "msg": "x"})
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


def dependencies(
    fake, sessions, keyring, *, adapter, fx, fault_breakpoint=None
) -> FinanceWriteDependencies:
    return FinanceWriteDependencies(
        adapter=adapter,
        source=SOURCE,
        config=CONFIG,
        sessions=sessions,
        keyring=keyring,
        fx=fx,
        now=now,
        fault_breakpoint=fault_breakpoint,
    )


def registry_for(deps) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))
    registry.register("finance.log_expense", build_expense_handler(deps))
    registry.register("finance.log_income", build_income_handler(deps))
    registry.register(
        "finance.update_family_fund", build_family_fund_handler(deps)
    )
    registry.register(
        "finance.update_expense_category", build_category_update_handler(deps)
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
    fault_breakpoint=None,
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
                fake,
                sessions,
                keyring,
                adapter=adapter,
                fx=fx,
                fault_breakpoint=fault_breakpoint,
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
            shared_enabled_write_switch(),
        )
    return result, key


def error_code(result) -> str:
    assert result.is_error
    return json.loads(result.content[0].text)["error"]["code"]


def payload(result) -> dict:
    assert not result.is_error, result.content[0].text
    return result.structured_content


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


@pytest.mark.parametrize("tool", ("finance.log_expense", "finance.log_income"))
def test_mcp_refuses_a_missing_host_resolved_date_before_any_create(
    sessions, keyring, caller, tool
) -> None:
    """Only the Agent Host may apply the receipt-bound date default."""
    arguments = (
        {key: value for key, value in LUNCH.items() if key != "occurred_on"}
        if tool == "finance.log_expense"
        else {
            "income_description": "发工资",
            "input_amount": "100",
            "input_currency": "CNY",
        }
    )
    fake = FakeBitable()

    result, _ = run(
        call(fake, sessions, keyring, caller, tool=tool, arguments=arguments)
    )

    assert error_code(result) == ErrorCode.INVALID_ARGUMENT.value
    assert fake.creates == []
    assert executions(sessions) == []


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


# --- `G1`: the business fields the receipt card draws ------------------------


def test_the_receipt_carries_every_field_the_card_shows(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    result, _ = run(call(fake, sessions, keyring, caller))

    record = payload(result)["record"]
    assert record["name"] == "午饭"
    assert record["amount_cny"] == "20.00"
    assert record["occurred_on"] == "2026-07-24"
    assert record["is_family_expense"] is False
    assert record["category"] == "餐饮"


def test_personal_spend_comes_from_the_read_back_not_from_the_amount(
    sessions, keyring, caller
) -> None:
    """The one receipt field this side must never compute.

    个人支出 is a Base formula the config freezes read-only precisely so family
    sharing and refunds are the ledger's arithmetic, not ours. The fake answers
    with a number no local rule would produce, so this assertion fails the day
    someone "helpfully" derives the field from `amount_cny`.
    """
    fake = FakeBitable()
    result, _ = run(call(fake, sessions, keyring, caller))

    body = payload(result)
    assert body["record"]["amount_cny"] == "20.00"
    assert body["record"]["personal_spend_cny"] == "18.89"


def test_an_unevaluated_formula_omits_the_row_and_still_succeeds(
    sessions, keyring, caller
) -> None:
    """Feishu had not computed the formula yet. One missing card row is a much
    smaller error than a personal-spend figure the ledger never produced."""
    fake = FakeBitable()
    fake.personal_spend_formula = None
    result, _ = run(call(fake, sessions, keyring, caller))

    body = payload(result)
    assert body["status"] == "created"
    assert "personal_spend_cny" not in body["record"]


def test_an_unrecognised_formula_envelope_omits_the_row_rather_than_guessing(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    # A shape change on Feishu's side: not the documented `{"type": 2, ...}`
    # envelope. Reading `value[0]` out of it anyway would put an arbitrary
    # number on the receipt card.
    fake.personal_spend_formula = lambda amount: {
        "type": 19,
        "value": [{"text": str(amount)}],
    }
    result, _ = run(call(fake, sessions, keyring, caller))

    body = payload(result)
    assert body["status"] == "created"
    assert "personal_spend_cny" not in body["record"]


def test_a_bare_number_formula_cell_is_not_read_as_the_envelope(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    fake.personal_spend_formula = lambda amount: float(amount)
    result, _ = run(call(fake, sessions, keyring, caller))

    assert "personal_spend_cny" not in payload(result)["record"]


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
    assert json.loads(result.content[0].text)["error"][
        "clarification_question"
    ] == "这笔旅行支出对应哪一趟行程？"
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


def test_a_production_ledger_config_is_refused_without_the_g5_switch(
    tmp_path: Path,
) -> None:
    """A production write is a G5 decision: without the explicit switch, refused."""
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


def test_a_production_ledger_config_is_accepted_with_the_g5_switch(
    tmp_path: Path,
) -> None:
    """The G5 switch (PERSONAL_AGENT_ALLOW_PRODUCTION_WRITE=1) admits production."""
    from personal_data_mcp.server.composition import load_protected_config

    document = json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
    document["ledger_kind"] = "production"
    path = tmp_path / "ledger.production.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    config = load_protected_config(path, allow_production=True)
    assert config.ledger_kind == "production"


def test_the_default_server_advertises_no_finance_tool() -> None:
    """Without composition, an enabled contract is still not executable."""
    from personal_data_mcp.server.app import build_registry

    names = build_registry().names()
    assert names == {"meta.capabilities"}


# --- §13.2 fault breakpoint: the production wiring, not the seam -------------
#
# `test_finance_write_path.py` proves the four pause sites exist inside
# `execute_governed_write` by handing the breakpoint straight to `write_expense`.
# That is the seam, and a seam wired only in tests is not wiring (§7): the object
# still has to travel composition -> `FinanceWriteDependencies` -> each handler
# -> the write function. Drop `fault_breakpoint=` at any one of those four hops
# and every existing test stays green while the live drill silently never pauses
# -- and a drill that does not pause reports the operator's `kill` as a guess.
#
# These cases close that gap for all three write tools by dispatching a real
# governed call and requiring the *same instance* to be asked for a pause.


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("finance.log_expense", None),
        (
            "finance.log_income",
            {
                "income_description": "发工资",
                "input_amount": "100",
                "input_currency": "CNY",
                "occurred_on": "2026-07-24",
            },
        ),
        (
            "finance.update_family_fund",
            {"mode": "top_up", "recharge_amount_cny": "10"},
        ),
    ],
)
def test_every_write_tool_carries_the_fault_breakpoint_to_the_write_path(
    sessions, keyring, caller, tool, arguments
) -> None:
    from personal_agent_core.fault_breakpoint import (
        BREAKPOINT_BEFORE_PREPARE,
        BREAKPOINT_COMMITTED_UNVERIFIED,
        BREAKPOINT_PREPARED,
        BREAKPOINT_SUBMITTING,
        FaultBreakpoint,
    )

    fake = FakeBitable()
    if tool == "finance.update_family_fund":
        fake.add_row(
            "family_fund",
            {"充值金额": 100, "家庭基金余额": {"type": 2, "value": [1000]}},
        )

    # A path that cannot exist: the recorder replaces the sleep entirely, so the
    # test asserts on delivery of the object, never on wall-clock time.
    breakpoint = FaultBreakpoint(Path("/nonexistent/fault-breakpoint.json"))
    asked: list[str] = []

    async def recording(name: str) -> None:
        asked.append(name)
        return None

    breakpoint.pause_if_armed = recording  # type: ignore[method-assign]

    result, _ = run(
        call(
            fake,
            sessions,
            keyring,
            caller,
            tool=tool,
            arguments=arguments,
            fault_breakpoint=breakpoint,
        )
    )

    assert not result.is_error, result.content[0].text
    assert asked == [
        BREAKPOINT_BEFORE_PREPARE,
        BREAKPOINT_PREPARED,
        BREAKPOINT_SUBMITTING,
        BREAKPOINT_COMMITTED_UNVERIFIED,
    ]


def test_composition_hands_the_fault_breakpoint_to_the_write_dependencies(
    monkeypatch, tmp_path: Path
) -> None:
    """The first hop: `finance_tools(fault_breakpoint=…)` must not drop it.

    Composition opens real network clients and a recovery task, so this stops at
    the point the object is placed on `FinanceWriteDependencies` -- which is the
    hop no other test covers.
    """
    from personal_agent_core.fault_breakpoint import FaultBreakpoint
    from personal_data_mcp.server import composition

    breakpoint = FaultBreakpoint(Path("/nonexistent/fault-breakpoint.json"))
    seen: list[object] = []

    class Stop(RuntimeError):
        pass

    async def capture(deps):
        seen.append(deps.fault_breakpoint)
        raise Stop

    monkeypatch.setattr(composition, "fresh_validation", capture)
    monkeypatch.setattr(composition, "load_credentials", lambda: object())
    monkeypatch.setattr(composition, "load_data_keyring", lambda: object())
    monkeypatch.setattr(
        composition, "require_synthetic_test_base", lambda source, **_kwargs: SOURCE
    )
    monkeypatch.setattr(composition, "load_base_source", lambda: SOURCE)
    monkeypatch.setattr(
        composition, "FeishuAdapter", lambda *_args, **_kwargs: _NoopClient()
    )
    monkeypatch.setattr(
        composition, "FxConnector", lambda *_args, **_kwargs: _NoopClient()
    )

    async def drive():
        async with composition.finance_tools(
            config_path=LEDGER_FIXTURES / "config.synthetic.json",
            sessions=None,
            fault_breakpoint=breakpoint,
        ):
            pass

    with pytest.raises(Stop):
        run(drive())
    assert seen == [breakpoint]


class _NoopClient:
    """Stands in for the adapter and FX connector, which own HTTP clients."""

    async def aclose(self) -> None:
        return None


# --- `finance.update_expense_category`: correcting 分类 and nothing else ------
#
# Written as failure cases first, per `AGENTS.md` §5.1. This is the first tool
# that *modifies* committed ledger content, so the interesting question is never
# "does the happy path work" -- it is what the row looks like after every way
# the call can go wrong.


#: The ledger day the correction fixtures sit on, and its stored representation.
#: Derived rather than hard-coded so the date the test asserts and the cell the
#: fake holds cannot disagree -- an epoch literal copied from another fixture is
#: how this test first claimed 2026 for a 2025 timestamp.
CORRECTION_DAY = date(2026, 7, 24)
CORRECTION_DAY_MILLIS = ledger_day_epoch_millis(CORRECTION_DAY)


def a_recorded_expense(
    fake: FakeBitable,
    *,
    category: str | None = "餐饮",
    name: str = "午饭",
    amount: float = 20.0,
) -> str:
    fields = {
        "名称": name,
        "原始金额": amount,
        "日期": CORRECTION_DAY_MILLIS,
        "是否家庭支出": False,
    }
    if category is not None:
        fields["分类"] = category
    return fake.add_row("expense", fields)


def correct(
    fake: FakeBitable,
    sessions,
    keyring,
    caller,
    *,
    record_id: str,
    category: str,
    expected: str | None = "餐饮",
    idempotency_key: str | None = None,
):
    return run(
        call(
            fake,
            sessions,
            keyring,
            caller,
            tool="finance.update_expense_category",
            arguments={
                "record_id": record_id,
                "category": category,
                "expected_current_category": expected,
            },
            idempotency_key=idempotency_key,
        )
    )


def test_a_correction_changes_the_category_and_verifies_the_rest(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    record_id = a_recorded_expense(fake)

    result, _ = correct(
        fake, sessions, keyring, caller, record_id=record_id, category="购物"
    )

    body = payload(result)
    assert body["status"] == "updated"
    assert body["category"] == "购物"
    assert body["record_id"] == record_id
    assert body["evidence"] == {
        "kind": "feishu_record",
        "external_id": record_id,
    }
    assert fake.rows["expense"][record_id]["分类"] == "购物"
    assert [e.state for e in executions(sessions)] == ["succeeded"]


def test_the_request_body_can_only_address_the_category(
    sessions, keyring, caller
) -> None:
    """The primary safety property, asserted on the wire.

    Bitable's update is partial, so a body naming only 分类 cannot rewrite 名称,
    金额, 日期 or 是否家庭支出 -- not "does not", *cannot*. A correction that is
    wrong about the row still leaves the money alone.
    """
    fake = FakeBitable()
    record_id = a_recorded_expense(fake, amount=1234.56, name="重要的一笔")

    correct(fake, sessions, keyring, caller, record_id=record_id, category="购物")

    assert len(fake.updates) == 1
    sent = json.loads(fake.updates[0].content)["fields"]
    assert set(sent) == {"分类"}
    stored = fake.rows["expense"][record_id]
    assert stored["名称"] == "重要的一笔"
    assert Decimal(str(stored["原始金额"])) == Decimal("1234.56")


def test_the_receipt_row_is_read_back_not_echoed(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    record_id = a_recorded_expense(fake, name="午饭", amount=20.0)

    result, _ = correct(
        fake, sessions, keyring, caller, record_id=record_id, category="购物"
    )

    record = payload(result)["record"]
    assert record["category"] == "购物"
    assert record["name"] == "午饭"
    assert record["amount_cny"] == "20.00"
    assert record["occurred_on"] == CORRECTION_DAY.isoformat()
    assert record["is_family_expense"] is False
    assert record["category_updated_at"]
    # 个人支出 may depend on 分类 and Feishu may not have re-evaluated it yet, so
    # the card drops the row rather than showing a possibly pre-edit number.
    assert "personal_spend_cny" not in record


def test_a_stale_expectation_refuses_and_writes_nothing(
    sessions, keyring, caller
) -> None:
    """Someone changed 分类 elsewhere since the card was drawn.

    Overwriting would discard a decision this process cannot see, so the write
    is refused and the ledger is left exactly as it was.
    """
    fake = FakeBitable()
    record_id = a_recorded_expense(fake, category="旅行")

    result, _ = correct(
        fake,
        sessions,
        keyring,
        caller,
        record_id=record_id,
        category="购物",
        expected="餐饮",
    )

    assert error_code(result) == ErrorCode.CATEGORY_CHANGED_ELSEWHERE.value
    assert fake.updates == []
    assert fake.rows["expense"][record_id]["分类"] == "旅行"
    # Refused before any execution row exists: nothing to reconcile.
    assert executions(sessions) == []


def test_a_row_already_at_the_target_succeeds_without_sending_anything(
    sessions, keyring, caller
) -> None:
    """The replay-after-a-lost-reply case, and why no durable slot is needed.

    Reporting a failure here would push Henson to press the button again, which
    is exactly the loop idempotency exists to prevent.
    """
    fake = FakeBitable()
    record_id = a_recorded_expense(fake, category="购物")

    result, _ = correct(
        fake,
        sessions,
        keyring,
        caller,
        record_id=record_id,
        category="购物",
        expected="餐饮",
    )

    assert payload(result)["status"] == "already_current"
    assert fake.updates == []


def test_replaying_the_same_key_does_not_send_a_second_update(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    record_id = a_recorded_expense(fake)

    _, key = correct(
        fake, sessions, keyring, caller, record_id=record_id, category="购物"
    )
    second, _ = correct(
        fake,
        sessions,
        keyring,
        caller,
        record_id=record_id,
        category="购物",
        idempotency_key=key,
    )

    assert payload(second)["status"] == "already_current"
    assert len(fake.updates) == 1


def test_a_category_the_ledger_does_not_have_is_refused_before_any_call(
    sessions, keyring, caller
) -> None:
    """The connector never creates a select option, so this is a refusal."""
    fake = FakeBitable()
    record_id = a_recorded_expense(fake)

    result, _ = correct(
        fake, sessions, keyring, caller, record_id=record_id, category="咖啡"
    )

    assert error_code(result) in {
        ErrorCode.CATEGORY_NOT_ALLOWED.value,
        # The frozen input schema enumerates the options, so the dispatcher's
        # own validation may refuse it one layer earlier. Either is a refusal
        # with zero calls, which is what this test is about.
        ErrorCode.INVALID_ARGUMENT.value,
    }
    assert fake.updates == []
    assert executions(sessions) == []


def test_an_unknown_record_writes_nothing(sessions, keyring, caller) -> None:
    fake = FakeBitable()
    a_recorded_expense(fake)

    result, _ = correct(
        fake, sessions, keyring, caller, record_id="recNOPE", category="购物"
    )

    assert error_code(result) == ErrorCode.SOURCE_UNAVAILABLE.value
    assert fake.updates == []
    assert executions(sessions) == []


def test_another_field_moving_during_the_update_is_manual_review(
    sessions, keyring, caller
) -> None:
    """Verifying only the changed field would accept this silently.

    A Base automation, a concurrent edit or a provider that touched more than it
    was asked to all look identical from here, and none of them may resolve to
    a clean success on a receipt whose job is to be checkable.
    """
    fake = FakeBitable()
    record_id = a_recorded_expense(fake, amount=20.0)

    def also_change_the_amount(row: dict) -> None:
        row["原始金额"] = 999.0

    fake.on_update = also_change_the_amount

    result, _ = correct(
        fake, sessions, keyring, caller, record_id=record_id, category="购物"
    )

    assert error_code(result) == ErrorCode.SOURCE_COMMITTED_MISMATCH.value
    assert [e.state for e in executions(sessions)] == ["needs_manual_review"]


def test_an_update_that_did_not_take_is_manual_review_not_success(
    sessions, keyring, caller
) -> None:
    fake = FakeBitable()
    record_id = a_recorded_expense(fake)

    def revert_the_category(row: dict) -> None:
        row["分类"] = "餐饮"

    fake.on_update = revert_the_category

    result, _ = correct(
        fake, sessions, keyring, caller, record_id=record_id, category="购物"
    )

    assert error_code(result) == ErrorCode.SOURCE_COMMITTED_MISMATCH.value
    assert [e.state for e in executions(sessions)] == ["needs_manual_review"]


def test_a_refund_with_no_category_can_be_given_one(
    sessions, keyring, caller
) -> None:
    """`expected_current_category: null` is a real state, not a missing value."""
    fake = FakeBitable()
    record_id = a_recorded_expense(fake, category=None)

    result, _ = correct(
        fake,
        sessions,
        keyring,
        caller,
        record_id=record_id,
        category="购物",
        expected=None,
    )

    assert payload(result)["status"] == "updated"
    assert fake.rows["expense"][record_id]["分类"] == "购物"
