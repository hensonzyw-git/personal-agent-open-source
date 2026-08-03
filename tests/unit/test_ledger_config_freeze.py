"""The annual-ledger config freeze, exercised entirely offline.

The freeze is the one step that turns a live Base into the protected config the
write path validates against, so what matters is that it refuses far more often
than it succeeds: an ambiguous name, a missing field, a formula in a writable
slot or an option set that differs from the frozen contract must all fail closed
rather than be adopted as the new expectation. All of that is provable against a
mock transport, and the real run stays a separate, gated action.
"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import stat
from pathlib import Path

import httpx
import pytest

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.finance.config_freeze import (
    ConfigFreezeError,
    build_config,
    freeze,
    require_freezable_kind,
    write_protected,
)
from personal_data_mcp.feishu.base_source import load_base_source
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.onboarding import observed_from_snapshot


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(encoding="utf-8")
)


def run(coro):
    return asyncio.run(coro)


def full_env() -> dict:
    return {
        "FEISHU_FINANCE_APP_ID": "cli_freeze",
        "FEISHU_FINANCE_APP_SECRET": "shh",
        "FEISHU_FINANCE_BASE_TOKEN": CONFIG.base_token,
        "FEISHU_FINANCE_TABLE_EXPENSE": CONFIG.tables["expense"].table_id,
        "FEISHU_FINANCE_TABLE_INCOME": CONFIG.tables["income"].table_id,
        "FEISHU_FINANCE_TABLE_FAMILY_FUND": CONFIG.tables["family_fund"].table_id,
        "FEISHU_FINANCE_LEDGER_KIND": "synthetic_test",
    }


def build_from(snapshot) -> object:
    return build_config(
        ledger_year=2026,
        config_version="2026.1",
        source=load_base_source(full_env()),
        observed=observed_from_snapshot(snapshot),
    )


# --- the happy path reproduces the protected config exactly ------------------


def test_freezing_the_live_schema_reproduces_the_protected_config() -> None:
    derived = build_from(SNAPSHOT)
    # Same checksum means same ids, names, types, writability and option sets:
    # the derivation is not merely "valid", it is the config itself.
    assert derived.checksum() == CONFIG.checksum()
    assert derived.tables["expense"].fields["category"].id == "fldSYNCATEGORY"
    assert derived.tables["family_fund"].fields["balance"].writable is False


def test_personal_spend_formula_is_frozen_as_a_read_only_query_field() -> None:
    # DEV-022 must use this formula for aggregation.  It is in the protected
    # config so a renamed/retyped formula becomes schema drift, but its
    # non-writable contract keeps it outside every create payload.
    derived = build_from(SNAPSHOT)
    personal_spend = derived.tables["expense"].fields["personal_spend"]
    assert personal_spend.id == "fldSYNPERSONAL"
    assert personal_spend.writable is False


# --- resolution refuses rather than guesses ----------------------------------


def test_a_duplicate_field_name_is_refused_as_ambiguous() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["expense"].append(
        {"field_id": "fldSYNAMOUNT2", "field_name": "原始金额", "type": 2}
    )
    with pytest.raises(ConfigFreezeError, match="ambiguous"):
        build_from(snapshot)


def test_a_missing_field_is_refused() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["income"] = [
        f for f in snapshot["income"] if f["field_name"] != "分类"
    ]
    with pytest.raises(ConfigFreezeError, match="income.category"):
        build_from(snapshot)


def test_a_wrong_type_is_refused() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    for field in snapshot["expense"]:
        if field["field_name"] == "原始金额":
            field["type"] = 1  # text where the contract requires a number
    with pytest.raises(ConfigFreezeError, match="expected type number"):
        build_from(snapshot)


def test_a_formula_in_a_writable_slot_is_refused() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    for field in snapshot["expense"]:
        if field["field_name"] == "原始金额":
            field["type"] = 20
    with pytest.raises(ConfigFreezeError, match="must be writable"):
        build_from(snapshot)


def test_a_non_formula_where_a_formula_is_expected_is_refused() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    for field in snapshot["family_fund"]:
        if field["field_name"] == "家庭基金余额":
            field["type"] = 2
    with pytest.raises(ConfigFreezeError, match="family_fund.balance"):
        build_from(snapshot)


def test_an_option_set_difference_is_refused_and_never_created() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    for field in snapshot["expense"]:
        if field["field_name"] == "分类":
            field["property"]["options"] = [
                option
                for option in field["property"]["options"]
                if option["name"] != "房租"
            ]
    with pytest.raises(ConfigFreezeError, match="option set"):
        build_from(snapshot)


def test_an_extra_live_option_is_also_refused() -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    for field in snapshot["expense"]:
        if field["field_name"] == "分类":
            field["property"]["options"].append({"name": "新分类"})
    with pytest.raises(ConfigFreezeError, match="option set"):
        build_from(snapshot)


# --- only the synthetic test Base may be frozen ------------------------------


def test_production_cannot_be_frozen_without_an_explicit_acknowledgement() -> None:
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(ConfigFreezeError, match="acknowledgement"):
        require_freezable_kind(
            load_base_source(env),
            declared_kind="production",
            allow_production=False,
        )


def test_production_can_be_frozen_for_read_only_verification() -> None:
    """Henson's staged-G5 decision, and why it is safe to allow here.

    Freezing reads field definitions and writes a local file; it never writes
    Feishu. What keeps this from becoming a production write path is the *write*
    door, pinned by the test below.
    """
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    require_freezable_kind(
        load_base_source(env), declared_kind="production", allow_production=True
    )


def test_a_production_config_still_cannot_reach_the_write_path() -> None:
    """The load-bearing gate, asserted from the other side.

    Relaxing the freeze guard is only defence-in-depth relaxation. If this ever
    stops holding, freezing production really would become a way to write it.
    """
    from personal_data_mcp.server.composition import load_protected_config
    from personal_data_mcp.feishu.base_source import LedgerSourceError

    production = CONFIG.model_copy(update={"ledger_kind": "production"})
    path = Path(tempfile.mkdtemp()) / "annual.json"
    path.write_text(production.model_dump_json(), encoding="utf-8")

    with pytest.raises(LedgerSourceError, match="G5"):
        load_protected_config(path)


def test_a_declared_kind_that_the_environment_contradicts_is_refused() -> None:
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(ConfigFreezeError, match="does not match"):
        require_freezable_kind(
            load_base_source(env),
            declared_kind="synthetic_test",
            allow_production=False,
        )


def test_the_synthetic_environment_is_freezable() -> None:
    require_freezable_kind(
        load_base_source(full_env()),
        declared_kind="synthetic_test",
        allow_production=False,
    )


# --- the written file is protected and never silently replaced ---------------


def test_the_frozen_file_is_written_at_mode_600(tmp_path) -> None:
    out = tmp_path / "nested" / "ledger.json"
    write_protected(CONFIG, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    reloaded = load_ledger_config(json.loads(out.read_text(encoding="utf-8")))
    assert reloaded.checksum() == CONFIG.checksum()


def test_an_existing_frozen_file_is_never_overwritten(tmp_path) -> None:
    out = tmp_path / "ledger.json"
    write_protected(CONFIG, out)
    with pytest.raises(ConfigFreezeError, match="already exists"):
        write_protected(CONFIG, out)


# --- the end-to-end fetch, against a mock transport --------------------------


def fields_handler(snapshot):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        table_id = request.url.path.split("/tables/", 1)[1].split("/", 1)[0]
        kind = next(
            kind
            for kind, table in CONFIG.tables.items()
            if table.table_id == table_id
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"items": snapshot[kind], "has_more": False},
            },
        )

    return handler


def patch_transport(monkeypatch, snapshot) -> None:
    handler = fields_handler(snapshot)
    original_init = FeishuAdapter.__init__

    def patched_init(self, credentials, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, credentials, **kwargs)

    monkeypatch.setattr(FeishuAdapter, "__init__", patched_init)


def test_the_fetch_produces_the_config_and_a_report_without_raw_ids(
    monkeypatch,
) -> None:
    patch_transport(monkeypatch, SNAPSHOT)
    config, report = run(
        freeze(
            full_env(),
            ledger_year=2026,
            config_version="2026.1",
            declared_kind="synthetic_test",
            allow_production=False,
        )
    )
    assert config.checksum() == CONFIG.checksum()
    assert report["status"] == "valid"
    assert report["config_checksum"] == CONFIG.checksum()

    text = json.dumps(report, ensure_ascii=False)
    assert CONFIG.base_token not in text
    for table in CONFIG.tables.values():
        assert table.table_id not in text
        for field in table.fields.values():
            assert field.id not in text


def test_the_fetch_refuses_a_non_synthetic_environment(monkeypatch) -> None:
    patch_transport(monkeypatch, SNAPSHOT)
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(ConfigFreezeError):
        run(
            freeze(
                env,
                ledger_year=2026,
                config_version="2026.1",
                declared_kind="synthetic_test",
                allow_production=False,
            )
        )
