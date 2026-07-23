"""DEV-016: the annual-ledger config and schema validator.

All placeholder ids. No Base is contacted; the validator works on an observed
field list, which is what keeps it credential-free.
"""

from __future__ import annotations

import json

import pytest

from personal_agent_core.tool_ir import ALLOWED_EXPENSE_CATEGORIES
from personal_data_mcp.finance.ledger_config import (
    EXPECTED_EXPENSE_CATEGORIES,
    FieldType,
    load_ledger_config,
)
from personal_data_mcp.finance.redacted_report import redacted_schema_report
from personal_data_mcp.finance.schema_validator import (
    DriftKind,
    ObservedField,
    observed_field_from_feishu,
    validate_schema,
)


def base_config() -> dict:
    return {
        "ledger_year": 2026,
        "config_version": "2026.1",
        "base_token": "bascnPLACEHOLDER0001",
        "effective_from": "2026-01-01",
        "effective_to": "2026-12-31",
        "tables": {
            "expense": {
                "table_id": "tblPLACEHOLDEREXP",
                "fields": {
                    "amount": {
                        "id": "fldAMOUNT",
                        "expected_name": "原始金额",
                        "type": "number",
                    },
                    "name": {
                        "id": "fldNAME",
                        "expected_name": "名称",
                        "type": "text",
                    },
                    "occurred_on": {
                        "id": "fldDATE",
                        "expected_name": "日期",
                        "type": "datetime",
                    },
                    "is_family_expense": {
                        "id": "fldFAMILY",
                        "expected_name": "是否家庭支出",
                        "type": "checkbox",
                    },
                    "category": {
                        "id": "fldCATEGORY",
                        "expected_name": "分类",
                        "type": "single_select",
                        "options": list(EXPECTED_EXPENSE_CATEGORIES),
                    },
                },
            }
        },
    }


def matching_observed() -> dict[str, list[ObservedField]]:
    return {
        "expense": [
            ObservedField("fldAMOUNT", "原始金额", FieldType.NUMBER),
            ObservedField("fldNAME", "名称", FieldType.TEXT),
            ObservedField("fldDATE", "日期", FieldType.DATETIME),
            ObservedField("fldFAMILY", "是否家庭支出", FieldType.CHECKBOX),
            ObservedField(
                "fldCATEGORY",
                "分类",
                FieldType.SINGLE_SELECT,
                options=tuple(ALLOWED_EXPENSE_CATEGORIES),
            ),
        ]
    }


# --- config -----------------------------------------------------------------


def test_the_config_categories_match_the_frozen_contract() -> None:
    # If the contract's category set ever changes, a stale config must fail this.
    assert EXPECTED_EXPENSE_CATEGORIES == ALLOWED_EXPENSE_CATEGORIES


def test_a_single_select_without_options_is_rejected() -> None:
    data = base_config()
    del data["tables"]["expense"]["fields"]["category"]["options"]
    with pytest.raises(ValueError):
        load_ledger_config(data)


def test_the_checksum_changes_when_a_field_id_changes() -> None:
    first = load_ledger_config(base_config()).checksum()
    changed = base_config()
    changed["tables"]["expense"]["fields"]["amount"]["id"] = "fldAMOUNT_NEW"
    assert load_ledger_config(changed).checksum() != first


# --- validation: the happy path and each drift ------------------------------


def test_a_matching_schema_is_valid() -> None:
    config = load_ledger_config(base_config())
    result = validate_schema(config, matching_observed())
    assert result.is_valid
    assert result.status == "valid"


def test_a_missing_field_is_drift() -> None:
    config = load_ledger_config(base_config())
    observed = matching_observed()
    observed["expense"] = [
        f for f in observed["expense"] if f.field_id != "fldNAME"
    ]
    result = validate_schema(config, observed)
    assert not result.is_valid
    assert any(d.kind is DriftKind.MISSING_FIELD for d in result.drifts)


def test_a_renamed_field_is_drift() -> None:
    config = load_ledger_config(base_config())
    observed = matching_observed()
    observed["expense"][0] = ObservedField("fldAMOUNT", "金额", FieldType.NUMBER)
    result = validate_schema(config, observed)
    assert any(d.kind is DriftKind.NAME_CHANGED for d in result.drifts)


def test_a_type_change_is_drift() -> None:
    config = load_ledger_config(base_config())
    observed = matching_observed()
    observed["expense"][0] = ObservedField("fldAMOUNT", "原始金额", FieldType.TEXT)
    result = validate_schema(config, observed)
    assert any(d.kind is DriftKind.TYPE_CHANGED for d in result.drifts)


def test_a_formula_field_where_a_writable_one_is_expected_is_drift() -> None:
    config = load_ledger_config(base_config())
    observed = matching_observed()
    observed["expense"][0] = ObservedField(
        "fldAMOUNT", "原始金额", None, is_formula=True
    )
    result = validate_schema(config, observed)
    assert any(d.kind is DriftKind.NOT_WRITABLE for d in result.drifts)


def test_a_changed_option_set_is_drift() -> None:
    config = load_ledger_config(base_config())
    observed = matching_observed()
    # A category was added in Feishu without a config change: fail closed.
    observed["expense"][4] = ObservedField(
        "fldCATEGORY",
        "分类",
        FieldType.SINGLE_SELECT,
        options=tuple(ALLOWED_EXPENSE_CATEGORIES) + ("新分类",),
    )
    result = validate_schema(config, observed)
    assert any(d.kind is DriftKind.OPTIONS_CHANGED for d in result.drifts)


def test_an_empty_snapshot_fails_closed() -> None:
    config = load_ledger_config(base_config())
    result = validate_schema(config, {})
    assert not result.is_valid
    # Every configured field is reported missing, not vacuously valid.
    assert all(d.kind is DriftKind.MISSING_FIELD for d in result.drifts)
    assert len(result.drifts) == 5


# --- Feishu normalisation ---------------------------------------------------


def test_feishu_single_select_options_are_read_from_property() -> None:
    field = {
        "field_id": "fldCATEGORY",
        "field_name": "分类",
        "type": 3,
        "property": {
            "options": [{"name": "餐饮", "id": "opt1"}, {"name": "旅行", "id": "opt2"}]
        },
    }
    observed = observed_field_from_feishu(field)
    assert observed.type is FieldType.SINGLE_SELECT
    assert observed.options == ("餐饮", "旅行")


def test_feishu_formula_and_auto_number_are_flagged() -> None:
    formula = observed_field_from_feishu(
        {"field_id": "f1", "field_name": "个人支出", "type": 20}
    )
    auto = observed_field_from_feishu(
        {"field_id": "f2", "field_name": "编号", "type": 1005}
    )
    assert formula.is_formula and formula.type is None
    assert auto.is_auto_number and auto.type is None


def test_an_unsupported_type_code_maps_to_none() -> None:
    observed = observed_field_from_feishu(
        {"field_id": "f3", "field_name": "附件", "type": 17}
    )
    assert observed.type is None


# --- redacted report --------------------------------------------------------


def test_the_redacted_report_hides_ids_but_keeps_shape() -> None:
    config = load_ledger_config(base_config())
    report = redacted_schema_report(config, matching_observed())

    serialised = json.dumps(report, ensure_ascii=False)
    # No raw resource id appears anywhere.
    assert "bascnPLACEHOLDER0001" not in serialised
    assert "tblPLACEHOLDEREXP" not in serialised
    assert "fldAMOUNT" not in serialised
    # The shape a reviewer needs is present.
    assert report["status"] == "valid"
    assert report["base_token_hash"].startswith("h:")
    amount = report["tables"]["expense"]["fields"]["amount"]
    assert amount["type"] == "number"
    assert amount["field_id_hash"].startswith("h:")
    # Category names are not secret and stay legible.
    assert report["tables"]["expense"]["fields"]["category"]["options"] == list(
        ALLOWED_EXPENSE_CATEGORIES
    )


def test_the_report_is_stable_for_the_same_input() -> None:
    config = load_ledger_config(base_config())
    a = redacted_schema_report(config, matching_observed())
    b = redacted_schema_report(config, matching_observed())
    assert a == b
