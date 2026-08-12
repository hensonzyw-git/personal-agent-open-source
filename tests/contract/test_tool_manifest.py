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
from personal_agent_core.tool_ir import ALLOWED_EXPENSE_CATEGORIES, TOOL_CONTRACTS


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


def tools() -> list[dict[str, Any]]:
    return load_manifest()["tools"]


def tool(name: str) -> dict[str, Any]:
    for entry in tools():
        if entry["name"] == name:
            return entry
    raise AssertionError(f"missing tool {name}")


def walk_keys(node: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            keys.extend(walk_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(walk_keys(item))
    return keys


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


def test_five_finance_tools_with_only_batch_disabled() -> None:
    document = load_manifest()
    finance = [t for t in document["tools"] if t["domain"] == "finance"]
    assert len(finance) == 5
    assert document["disabled_tools"] == ["finance.log_expense_batch"]
    assert set(document["enabled_tools"]) == {
        "finance.log_expense",
        "finance.log_income",
        "finance.update_family_fund",
        "finance.query_expenses",
        "meta.capabilities",
    }


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
    for entry in tools():
        keys = set(walk_keys(entry["model_input_schema"]))
        leaked = keys.intersection(HOST_ONLY_FIELDS)
        assert leaked == set(), f"{entry['name']} exposes host fields {leaked}"


def test_host_context_schema_is_closed_and_pins_the_timezone() -> None:
    schema = load_manifest()["host_context_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["timezone"] == {"const": "Asia/Shanghai"}


def test_every_model_input_schema_is_closed() -> None:
    for entry in tools():
        assert entry["model_input_schema"]["additionalProperties"] is False


def test_write_tools_declare_host_injected_idempotency() -> None:
    for entry in tools():
        if entry["effect"] in {"create", "update"}:
            assert entry["idempotency"]["key_source"] == "host_injected_uuid4"
            assert entry["risk_level"] == "R2"
            assert entry["confirmation"] == "never"


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
