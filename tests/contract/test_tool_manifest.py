"""DEV-002: the generated tool manifest is reproducible and detects hand edits.

These tests also pin the product rules that a schema is allowed to encode. A
default value is a way for the model to stay silent about a field, so the
absence of a default on `is_family_expense` is a contract, not a style choice.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest

from personal_agent_core import manifest as manifest_module
from personal_agent_core.errors import ErrorCode
from personal_agent_core.finance_tools import (
    FINANCE_READ_TOOLS,
    FINANCE_TOOLS,
    FINANCE_WRITE_TOOLS,
)
from personal_agent_core.manifest import (
    MANIFEST_PATH,
    build_manifest,
    load_manifest,
    render_manifest,
    sha256_of,
)
from personal_agent_core.money import AMOUNT_PATTERN, SIGNED_AMOUNT_PATTERN
from personal_agent_core.tool_ir import (
    ALLOWED_EXPENSE_CATEGORIES,
    CLIENT_WIRE_VERSION_HEADER,
    TOOL_CONTRACTS,
)


OBSOLETE_TOOL_NAMES = (
    "finance.query_transactions",
    "finance.analyze_period",
    "meta.get_capabilities",
)
HOST_ONLY_FIELDS = (
    "request_id",
    "idempotency_key",
    "user_id",
    "device_id",
    "granted_scopes",
    "timezone",
    "trace_id",
    "duplicate_override",
)

#: Host-only names a contract may re-declare as one of its own business fields.
#: `calendar.create_event`'s `timezone` is the *event's* IANA timezone (design
#: §2.2); the Host Context's `timezone` is the message day-boundary. They share
#: a name and nothing else. The exemption is read from the schema itself (a
#: host-only key survives only where the tool's own closed schema declares it at
#: top level), and this map is pinned literally — widening it must be a
#: deliberate edit, not a side effect of adding a field.
DECLARED_HOST_ONLY_FIELDS = {"calendar.create_event": ["timezone"]}


def tools() -> list[dict[str, Any]]:
    return load_manifest()["tools"]


def tool(name: str) -> dict[str, Any]:
    for entry in tools():
        if entry["name"] == name:
            return entry
    raise AssertionError(f"missing tool {name}")


# --- reproducibility -------------------------------------------------------


def test_checked_in_artifact_matches_the_ir() -> None:
    assert MANIFEST_PATH.read_text(encoding="utf-8") == render_manifest()


def test_generation_is_deterministic() -> None:
    assert render_manifest() == render_manifest()
    assert build_manifest() == build_manifest()


def test_manifest_embeds_no_timestamp_or_environment() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8").lower()
    for volatile in ("generated_at", "timestamp", "hostname", "/users/"):
        assert volatile not in text


# --- drift detection -------------------------------------------------------


def test_a_hand_edited_schema_breaks_its_stored_hash() -> None:
    entry = tool("finance.log_expense")
    tampered = json.loads(json.dumps(entry["model_input_schema"]))
    tampered["properties"]["is_family_expense"]["default"] = False

    assert sha256_of(entry["model_input_schema"]) == entry["input_schema_hash"]
    assert sha256_of(tampered) != entry["input_schema_hash"]


def test_check_mode_rejects_a_tampered_artifact(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tampered_path = tmp_path / "tool_manifest.json"
    document = json.loads(render_manifest())
    document["tools"][0]["model_input_schema"]["properties"]["name"][
        "maxLength"
    ] = 4000
    tampered_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manifest_module, "MANIFEST_PATH", tampered_path)
    monkeypatch.setattr("sys.argv", ["personal-agent-generate-contracts", "--check"])

    with pytest.raises(SystemExit) as excinfo:
        manifest_module.main()
    assert "out of date" in str(excinfo.value)


# --- enablement ------------------------------------------------------------


def test_six_finance_tools_with_only_batch_disabled() -> None:
    document = load_manifest()
    finance = [t for t in document["tools"] if t["domain"] == "finance"]
    assert len(finance) == 6
    assert document["disabled_tools"] == ["finance.log_expense_batch"]
    assert set(document["enabled_tools"]) == {
        "finance.log_expense",
        "finance.log_income",
        "finance.update_family_fund",
        "finance.update_expense_category",
        "finance.query_expenses",
        "meta.capabilities",
        "calendar.create_event",
        "calendar.query_events",
        "calendar.ingest_events",
    }


def test_the_model_is_never_offered_the_category_update() -> None:
    """Enabled is not the same permission as model-callable.

    `finance.update_expense_category` rewrites a field on an already-committed
    ledger row. That authority belongs to Henson tapping a picker, and the route
    that carries it is device-authenticated and off the model channel entirely.
    A model able to call it could re-categorise history from inference alone.

    Asserted as a literal set rather than re-derived from `model_callable`: a
    test that recomputes the production expression passes by construction and
    would have said nothing the day a new write tool defaulted into the model's
    reach.
    """
    document = load_manifest()
    assert set(document["model_callable_tools"]) == {
        "finance.log_expense",
        "finance.log_income",
        "finance.update_family_fund",
        "finance.query_expenses",
        "meta.capabilities",
        "calendar.create_event",
        "calendar.query_events",
    }
    assert "finance.update_expense_category" not in document["model_callable_tools"]
    # And it really is live -- this is a narrowing of who may call it, not a
    # disabled tool wearing a different label.
    assert "finance.update_expense_category" in document["enabled_tools"]


def test_every_model_callable_tool_is_enabled() -> None:
    document = load_manifest()
    assert set(document["model_callable_tools"]) <= set(document["enabled_tools"])


def test_finance_tool_effect_partition_matches_the_manifest() -> None:
    finance = [entry for entry in tools() if entry["domain"] == "finance"]
    assert {entry["name"] for entry in finance} == FINANCE_TOOLS
    assert {
        entry["name"] for entry in finance if entry["effect"] == "read"
    } == FINANCE_READ_TOOLS
    assert {
        entry["name"] for entry in finance if entry["effect"] != "read"
    } == FINANCE_WRITE_TOOLS


def test_disabled_batch_states_why_it_is_off() -> None:
    batch = tool("finance.log_expense_batch")
    assert batch["enabled"] is False
    assert "原子" in batch["disabled_reason"]


def test_allowed_tools_version_covers_only_enabled_contracts() -> None:
    document = load_manifest()
    expected = sha256_of(
        [
            {"name": t["name"], "contract_hash": t["contract_hash"]}
            for t in document["tools"]
            if t["enabled"]
        ]
    )
    assert document["allowed_tools_version"] == expected

    # Including the disabled batch tool must produce a different version, which
    # is what makes enabling it invalidate every outstanding access token.
    including_batch = sha256_of(
        [
            {"name": t["name"], "contract_hash": t["contract_hash"]}
            for t in document["tools"]
        ]
    )
    assert including_batch != document["allowed_tools_version"]


# --- product rules the schema must encode ----------------------------------


def test_family_scope_is_required_and_has_no_default() -> None:
    single = tool("finance.log_expense")["model_input_schema"]
    batched = tool("finance.log_expense_batch")["model_input_schema"]
    for entry in (single, batched["properties"]["entries"]["items"]):
        field = entry["properties"]["is_family_expense"]
        assert "is_family_expense" in entry["required"]
        assert field["type"] == "boolean"
        assert "default" not in field, "a default lets the model omit the scope"
        assert "不得从历史" in field["description"]


def test_income_never_borrows_expense_semantics() -> None:
    properties = tool("finance.log_income")["model_input_schema"]["properties"]
    for forbidden in ("is_family_expense", "entry_kind", "category", "trip_tag"):
        assert forbidden not in properties


def test_write_date_is_optional_only_for_the_host_receipt_default() -> None:
    """The Host must bind the dynamic default before it calls the MCP server."""
    for name in ("finance.log_expense", "finance.log_income"):
        schema = tool(name)["model_input_schema"]
        assert "occurred_on" not in schema["required"]
        assert "default" not in schema["properties"]["occurred_on"]
        assert "Asia/Shanghai 接收日" in schema["properties"]["occurred_on"]["description"]


def test_expense_categories_match_the_verified_ledger() -> None:
    field = tool("finance.log_expense")["model_input_schema"]["properties"]["category"]
    assert field["enum"] == [*ALLOWED_EXPENSE_CATEGORIES, None]
    assert len(ALLOWED_EXPENSE_CATEGORIES) == 8


def test_amount_patterns_come_from_the_shared_money_module() -> None:
    expense = tool("finance.log_expense")["model_input_schema"]["properties"]
    assert expense["input_amount"]["pattern"] == AMOUNT_PATTERN
    assert expense["settlement_amount_cny"]["pattern"] == AMOUNT_PATTERN

    amount_range = tool("finance.query_expenses")["model_input_schema"][
        "properties"
    ]["personal_amount_cny"]["properties"]
    assert amount_range["min"]["pattern"] == SIGNED_AMOUNT_PATTERN
    assert amount_range["max"]["pattern"] == SIGNED_AMOUNT_PATTERN
    assert AMOUNT_PATTERN != SIGNED_AMOUNT_PATTERN


def test_normal_expense_requires_a_category_and_nonzero_amount() -> None:
    schema = tool("finance.log_expense")["model_input_schema"]
    valid = {
        "name": "午饭",
        "input_amount": "45.00",
        "input_currency": "CNY",
        "occurred_on": "2026-07-23",
        "is_family_expense": False,
        "entry_kind": "expense",
        "category": "餐饮",
    }
    jsonschema.validate(valid, schema)
    # Dynamic defaults are Host-resolved from the durable receipt timestamp, so
    # omission is valid model output.  An explicit malformed value is not
    # silently repaired and still fails at the schema boundary.
    jsonschema.validate(
        {key: value for key, value in valid.items() if key != "occurred_on"},
        schema,
    )
    for invalid_date in ("今天", "", None):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**valid, "occurred_on": invalid_date}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                key: value
                for key, value in valid.items()
                if key != "category"
            },
            schema,
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**valid, "input_amount": "0.00"}, schema)

    # Refund and AA category may remain absent until the server finds a unique
    # original transaction; only ordinary expense requires it at the boundary.
    jsonschema.validate(
        {
            **{key: value for key, value in valid.items() if key != "category"},
            "entry_kind": "refund",
        },
        schema,
    )


def test_family_fund_modes_are_dispatched_on_mode() -> None:
    schema = tool("finance.update_family_fund")["model_input_schema"]

    # "Not applicable" may be spelled either way. Omitting the other mode's
    # field and sending it as an explicit null mean the same thing, and
    # finance.log_expense already accepts explicit nulls, so both tools have to
    # agree about it.
    for valid in (
        {"mode": "top_up", "recharge_amount_cny": "10000.00"},
        {
            "mode": "top_up",
            "recharge_amount_cny": "10000.00",
            "target_balance_cny": None,
        },
        {"mode": "top_up", "recharge_amount_cny": "10000.00", "note": "年终"},
        {"mode": "interest_reconcile", "target_balance_cny": "30000.00"},
        {
            "mode": "interest_reconcile",
            "target_balance_cny": "30000.00",
            "recharge_amount_cny": None,
            "note": None,
        },
    ):
        jsonschema.validate(valid, schema)

    for invalid in (
        # The mode's own field is genuinely missing.
        {"mode": "top_up"},
        {"mode": "top_up", "recharge_amount_cny": None},
        {"mode": "top_up", "recharge_amount_cny": "0"},
        # Both modes' fields carry real values.
        {
            "mode": "top_up",
            "recharge_amount_cny": "10000.00",
            "target_balance_cny": "30000.00",
        },
        # The 利息补齐 note is fixed server side, so the model may not write one.
        {
            "mode": "interest_reconcile",
            "target_balance_cny": "30000.00",
            "note": "模型不应覆盖固定备注",
        },
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_a_bad_amount_reports_the_amount_not_the_other_mode() -> None:
    # Under oneOf every failure surfaced as "'interest_reconcile' was expected",
    # including a plain bad amount. That text would have reached clarification
    # prompts and alerts, where it says nothing true about the problem.
    schema = tool("finance.update_family_fund")["model_input_schema"]
    with pytest.raises(jsonschema.ValidationError) as excinfo:
        jsonschema.validate(
            {"mode": "top_up", "recharge_amount_cny": "0.00"}, schema
        )
    assert "interest_reconcile" not in excinfo.value.message
    assert "0.00" in excinfo.value.message


def test_batch_requires_at_least_two_entries() -> None:
    entries = tool("finance.log_expense_batch")["model_input_schema"]["properties"][
        "entries"
    ]
    assert entries["minItems"] == 2


# --- governance ------------------------------------------------------------


def test_host_context_is_never_visible_to_the_model() -> None:
    """Host-only names are stripped from model arguments at the bridge.

    The strip is per top level, because that is the only level it operates on:
    a host-only name nested inside a business array lives in that array's own
    closed schema and is business data, not a Host field.

    One contract may legitimately declare such a name for itself, and the set
    of those declarations is pinned below rather than re-derived — a tool that
    quietly starts accepting `device_id` must fail here.
    """
    declared_by_tool: dict[str, list[str]] = {}
    for entry in tools():
        properties = entry["model_input_schema"].get("properties", {})
        declared = set(properties).intersection(HOST_ONLY_FIELDS)
        if declared:
            declared_by_tool[entry["name"]] = sorted(declared)
        allowed = set(DECLARED_HOST_ONLY_FIELDS.get(entry["name"], ()))
        leaked = declared - allowed
        assert leaked == set(), f"{entry['name']} exposes host fields {sorted(leaked)}"
    assert declared_by_tool == DECLARED_HOST_ONLY_FIELDS


def test_host_context_schema_is_closed_and_pins_the_timezone() -> None:
    schema = load_manifest()["host_context_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["timezone"] == {"const": "Asia/Shanghai"}


def test_every_model_input_schema_is_closed() -> None:
    for entry in tools():
        assert entry["model_input_schema"]["additionalProperties"] is False


def test_write_tools_declare_host_injected_idempotency() -> None:
    """Every model-reachable write is a keyed, R2, unconfirmed write.

    `calendar.ingest_events` is an update-effect tool that legitimately holds
    `not_applicable`: its replay safety is structural (last_modified-arbitrated
    upsert, identical batch replays identically), not a host-injected key. That
    is acceptable *only* because it never reaches the model channel — the test
    below pins that premise, and the loop here deliberately covers only tools a
    model could actually choose.
    """
    for entry in tools():
        if entry["effect"] in {"create", "update"} and entry["model_callable"]:
            assert entry["idempotency"]["key_source"] == "host_injected_uuid4"
            assert entry["risk_level"] == "R2"
            assert entry["confirmation"] == "never"


def test_calendar_create_event_is_a_device_executed_write() -> None:
    """The Apple-calendar write happens on the phone, not through the bridge.

    The contract must say so (`executor="device"`) so the dispatch fork is
    derived from the IR, and the MCP-side handler is a fail-closed guard that
    must never be reachable in production composition.
    """
    document = load_manifest()
    entry = next(t for t in document["tools"] if t["name"] == "calendar.create_event")
    assert entry["executor"] == "device"
    assert entry["domain"] == "calendar"
    assert entry["effect"] == "create"
    assert entry["risk_level"] == "R2"
    assert entry["enabled"] is True
    assert entry["model_callable"] is True
    assert entry["required_scopes"] == ["calendar.event.write"]
    # The output schema must not claim external evidence the tool does not
    # produce at dispatch: the event identifier arrives with the device's
    # report, not with the issued action.
    assert "record_id" not in entry["output_schema"].get("properties", {})


def test_calendar_query_events_is_a_read_against_the_mirror() -> None:
    document = load_manifest()
    entry = next(t for t in document["tools"] if t["name"] == "calendar.query_events")
    assert entry["executor"] == "mcp"
    assert entry["effect"] == "read"
    assert entry["required_scopes"] == ["calendar.event.read"]
    # The freshness contract is part of the tool, not a courtesy of the model.
    assert {"data_as_of", "mirror_stale"} <= set(entry["output_schema"]["required"])
    # The Finance strict decoder keys on this const; a calendar read must not
    # carry it, or a valid calendar result would fail closed inside the wrong
    # projection.
    assert "metric" not in entry["output_schema"]["properties"]


def test_calendar_ingest_events_is_not_model_callable() -> None:
    """A model able to write the mirror could fabricate the calendar it is
    later asked to summarise. The ingest is a deterministic device-sync action,
    so it is enabled for the sync route and absent from the model channel."""
    document = load_manifest()
    entry = next(
        t for t in document["tools"] if t["name"] == "calendar.ingest_events"
    )
    assert entry["model_callable"] is False
    assert entry["enabled"] is True
    assert entry["idempotency"]["key_source"] == "not_applicable"


def test_ir_version_is_the_calendar_contract_revision() -> None:
    assert load_manifest()["ir_version"] == "0.3.0"


def test_calendar_query_declares_the_calendar_name_for_the_card() -> None:
    """The list card reads 「标题 · 日期时间 · 日历名」 (design §9.2), and the
    only place a name exists is the device's directory. The field is required
    on the row — always present, null when the device has no name for that
    identifier — because a missing key and「没有名字」are different facts, and
    only one of them is true.
    """
    items = tool("calendar.query_events")["output_schema"]["properties"]["events"][
        "items"
    ]
    assert "calendar_title" in items["required"]
    assert items["properties"]["calendar_title"]["type"] == ["string", "null"]
    # An identifier is not a name: the contract states the null case rather
    # than inviting a renderer to substitute the EventKit UUID.
    assert "UUID" in items["properties"]["calendar_title"]["description"]


def test_calendar_create_routes_by_business_calendar() -> None:
    """Routing is a required model decision, never a default.

    A default would let the model stay silent and have the server pick — and
    every silent pick is 「日常安排」, which is exactly the funnel the PRD
    forbids. 【飞行计划】 stays in the enum because it is the only way a flight
    request gets the honest refusal; the server is the single refusal point.
    """
    schema = tool("calendar.create_event")["model_input_schema"]
    field = schema["properties"]["calendar"]
    assert "calendar" in schema["required"]
    assert field["enum"] == ["日常安排", "出游计划", "演出&活动", "飞行计划"]
    assert "default" not in field, "a default lets the model omit routing"


def test_calendar_timezone_is_the_events_own_field() -> None:
    """`timezone` here is the event's IANA zone (Q11), not the Host's day
    boundary — and it is model-facing, so `HOST_ONLY_FIELDS` must exempt it
    through the schema (see `DECLARED_HOST_ONLY_FIELDS`)."""
    field = tool("calendar.create_event")["model_input_schema"]["properties"][
        "timezone"
    ]
    assert field["type"] == ["string", "null"]
    assert field["default"] is None
    assert "Asia/Shanghai" in field["description"]


def test_all_day_events_travel_as_dates_and_never_a_timezone() -> None:
    schema = tool("calendar.create_event")["model_input_schema"]
    all_day = {
        "title": "东京行",
        "start": "2027-01-01T00:00:00+08:00",
        "end": "2027-01-04T00:00:00+08:00",
        "all_day": True,
        "calendar": "出游计划",
        "start_date": "2027-01-01",
        "end_date": "2027-01-04",
    }
    jsonschema.validate(all_day, schema)
    # An all-day event has no owner timezone: the probe proved EventKit keeps
    # them floating, and setting `timeZone` flips `isAllDay` to false.
    jsonschema.validate({**all_day, "timezone": None}, schema)

    for invalid in (
        {**all_day, "timezone": "Asia/Tokyo"},
        {key: value for key, value in all_day.items() if key != "start_date"},
        {key: value for key, value in all_day.items() if key != "end_date"},
        {**all_day, "start_date": None},
        {**all_day, "end_date": "2027-1-4"},
        {**all_day, "start_date": "2027/01/01"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_timed_events_carry_an_instant_not_dates() -> None:
    schema = tool("calendar.create_event")["model_input_schema"]
    timed = {
        "title": "网球",
        "start": "2026-09-12T15:00:00+08:00",
        "end": "2026-09-12T16:30:00+08:00",
        "all_day": False,
        "calendar": "日常安排",
    }
    jsonschema.validate(timed, schema)
    jsonschema.validate({**timed, "timezone": "Asia/Tokyo"}, schema)
    # "Not applicable" may be spelled either way, exactly as the family-fund
    # modes already allow.
    jsonschema.validate({**timed, "start_date": None, "end_date": None}, schema)

    for invalid in (
        {**timed, "start_date": "2026-09-12"},
        {**timed, "end_date": "2026-09-13"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_a_calendar_write_without_a_route_is_refused_at_the_schema() -> None:
    schema = tool("calendar.create_event")["model_input_schema"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "title": "网球",
                "start": "2026-09-12T15:00:00+08:00",
                "end": "2026-09-12T16:30:00+08:00",
                "all_day": False,
            },
            schema,
        )


def test_the_calendar_action_declares_the_client_wire_version_it_needs() -> None:
    """R1-F1: a client that cannot honour the new fields must not receive the
    action. The requirement is a contract field, so the dispatcher and the
    projection read it from the IR instead of a hand-maintained tool list."""
    versions = {entry["name"]: entry["wire_version"] for entry in tools()}
    assert versions["calendar.create_event"] == 2
    assert {n for n, v in versions.items() if v != 1} == {"calendar.create_event"}
    assert CLIENT_WIRE_VERSION_HEADER == "X-Client-Wire-Version"


def test_device_executor_forks_the_dispatch_path() -> None:
    """Every contract declares an executor, and exactly one calendar write is
    device-executed while everything else stays connector-backed."""
    executors = {entry["name"]: entry["executor"] for entry in tools()}
    assert set(executors.values()) == {"mcp", "device"}
    assert {n for n, e in executors.items() if e == "device"} == {
        "calendar.create_event"
    }


def test_scopes_are_split_per_capability() -> None:
    assert tool("finance.log_expense")["required_scopes"] == [
        "finance.expense.write"
    ]
    assert tool("finance.log_income")["required_scopes"] == [
        "finance.income.write"
    ]
    assert tool("finance.update_family_fund")["required_scopes"] == [
        "finance.family_fund.write"
    ]
    assert tool("finance.query_expenses")["required_scopes"] == [
        "finance.expense.read"
    ]


def test_declared_errors_are_all_stable_codes() -> None:
    known = {code.value for code in ErrorCode}
    for entry in tools():
        assert set(entry["errors"]).issubset(known)


def test_no_obsolete_tool_names_survive_in_the_manifest() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    for obsolete in OBSOLETE_TOOL_NAMES:
        assert obsolete not in text


def test_contract_objects_reject_unknown_fields() -> None:
    contract = TOOL_CONTRACTS[0]
    with pytest.raises(Exception):
        type(contract)(**{**contract.model_dump(), "surprise": True})
