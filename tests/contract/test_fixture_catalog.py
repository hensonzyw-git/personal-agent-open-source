"""DEV-004: fixture catalogs are derived from the contract, not rewritten."""

from __future__ import annotations

from fixtures.finance_fixture import (
    fixture_catalog,
    fixture_receipt,
    fixture_record_id,
)
from personal_agent_core.evalset import OBSOLETE_TOOL_NAMES
from personal_agent_core.manifest import load_manifest


def test_catalog_matches_the_enabled_manifest_tools() -> None:
    manifest = load_manifest()
    assert [tool["name"] for tool in fixture_catalog()] == manifest["enabled_tools"]


def test_disabled_batch_is_absent_rather_than_present_and_refusing() -> None:
    names = {tool["name"] for tool in fixture_catalog()}
    assert "finance.log_expense_batch" not in names


def test_catalog_carries_no_obsolete_names() -> None:
    names = {tool["name"] for tool in fixture_catalog()}
    assert names.isdisjoint(OBSOLETE_TOOL_NAMES)


def test_catalog_schemas_are_the_generated_ones() -> None:
    manifest = {tool["name"]: tool for tool in load_manifest()["tools"]}
    for tool in fixture_catalog():
        assert tool["input_schema"] == manifest[tool["name"]]["model_input_schema"]


def test_receipts_are_deterministic_so_replay_is_distinguishable() -> None:
    arguments = {
        "name": "午饭",
        "input_amount": "45.00",
        "input_currency": "CNY",
        "occurred_on": "2026-07-23",
        "is_family_expense": False,
        "entry_kind": "expense",
        "category": "餐饮",
    }
    first = fixture_record_id("finance.log_expense", arguments)
    assert first == fixture_record_id("finance.log_expense", arguments)

    changed = fixture_record_id(
        "finance.log_expense", {**arguments, "input_amount": "46.00"}
    )
    assert changed != first

    receipt = fixture_receipt("finance.log_expense", arguments)
    assert receipt["record_id"] == first
    assert receipt["evidence"] == {
        "kind": "feishu_record",
        "external_id": first,
    }
