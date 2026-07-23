"""DEV-017 live-fetch, tested offline: base-source guard, pagination, probe.

Everything runs against a mock transport and an in-memory env dict. The point of
these tests is that the synthetic-test guard fails closed and the probe leaks
nothing, so the real run is safe -- the real run itself is a separate, gated
action and is not performed here.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    LedgerSourceError,
    load_base_source,
    require_synthetic_test_base,
)
from personal_data_mcp.feishu.connectivity import probe, probe_succeeded
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.feishu.endpoints import LIST_FIELDS
from personal_data_mcp.finance.ledger_config import load_ledger_config


PLACEHOLDER = FeishuCredentials(app_id="cli_placeholder", app_secret="secret_xyz")
LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(encoding="utf-8")
)


def clock():
    return 1000.0


def run(coro):
    return asyncio.run(coro)


def full_env() -> dict:
    return {
        "FEISHU_FINANCE_APP_ID": "cli_probe",
        "FEISHU_FINANCE_APP_SECRET": "shh",
        "FEISHU_FINANCE_BASE_TOKEN": CONFIG.base_token,
        "FEISHU_FINANCE_TABLE_EXPENSE": CONFIG.tables["expense"].table_id,
        "FEISHU_FINANCE_TABLE_INCOME": CONFIG.tables["income"].table_id,
        "FEISHU_FINANCE_TABLE_FAMILY_FUND": (
            CONFIG.tables["family_fund"].table_id
        ),
        "FEISHU_FINANCE_LEDGER_KIND": "synthetic_test",
    }


# --- the synthetic-test guard fails closed ----------------------------------


def test_a_non_synthetic_base_is_refused() -> None:
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(LedgerSourceError):
        require_synthetic_test_base(
            load_base_source(env),
            approved_base_token=CONFIG.base_token,
            approved_tables={
                kind: table.table_id for kind, table in CONFIG.tables.items()
            },
            approved_ledger_kind=CONFIG.ledger_kind,
        )


def test_a_missing_ledger_kind_is_refused() -> None:
    env = full_env()
    del env["FEISHU_FINANCE_LEDGER_KIND"]
    with pytest.raises(LedgerSourceError):
        load_base_source(env)


def test_the_synthetic_base_is_accepted() -> None:
    source = require_synthetic_test_base(
        load_base_source(full_env()),
        approved_base_token=CONFIG.base_token,
        approved_tables={
            kind: table.table_id for kind, table in CONFIG.tables.items()
        },
        approved_ledger_kind=CONFIG.ledger_kind,
    )
    assert source.is_synthetic_test
    assert source.tables == {
        kind: table.table_id for kind, table in CONFIG.tables.items()
    }


def test_a_real_base_cannot_pass_by_reusing_the_synthetic_marker() -> None:
    env = full_env()
    env["FEISHU_FINANCE_BASE_TOKEN"] = "bascnREALPERSONALBASE"
    with pytest.raises(LedgerSourceError):
        require_synthetic_test_base(
            load_base_source(env),
            approved_base_token=CONFIG.base_token,
            approved_tables={
                kind: table.table_id for kind, table in CONFIG.tables.items()
            },
            approved_ledger_kind=CONFIG.ledger_kind,
        )


def test_all_three_table_ids_are_required() -> None:
    env = full_env()
    del env["FEISHU_FINANCE_TABLE_INCOME"]
    with pytest.raises(LedgerSourceError):
        load_base_source(env)


# --- list_fields follows pagination -----------------------------------------


def paginating_fields_handler(snapshot=None):
    snapshot = SNAPSHOT if snapshot is None else snapshot

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
        fields = snapshot[kind]
        token = request.url.params.get("page_token")
        if kind == "expense" and token is None:
            data = {
                "items": fields[:2],
                "has_more": True,
                "page_token": "p2",
            }
        elif kind == "expense":
            data = {"items": fields[2:], "has_more": False}
        else:
            data = {"items": fields, "has_more": False}
        return httpx.Response(200, json={"code": 0, "data": data})

    return handler


def test_list_fields_reads_every_page() -> None:
    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=httpx.MockTransport(paginating_fields_handler()),
            now=clock,
        ) as adapter:
            return await adapter.list_fields(
                CONFIG.base_token,
                CONFIG.tables["expense"].table_id,
            )

    fields = run(scenario())
    assert [f["field_id"] for f in fields] == [
        field["field_id"] for field in SNAPSHOT["expense"]
    ]


def test_list_fields_fails_if_more_pages_have_no_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"items": [], "has_more": True},
            },
        )

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=httpx.MockTransport(handler),
            now=clock,
        ) as adapter:
            return await adapter.list_fields(
                CONFIG.base_token,
                CONFIG.tables["expense"].table_id,
            )

    from personal_agent_core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_list_fields_fails_if_the_cursor_repeats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [],
                    "has_more": True,
                    "page_token": "same-page",
                },
            },
        )

    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=httpx.MockTransport(handler),
            now=clock,
        ) as adapter:
            return await adapter.list_fields(
                CONFIG.base_token,
                CONFIG.tables["expense"].table_id,
            )

    from personal_agent_core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code is ErrorCode.SOURCE_UNAVAILABLE


# --- the probe: read-only, redacted -----------------------------------------


def test_probe_reports_shape_without_leaking_ids(monkeypatch) -> None:
    # Point the adapter's transport at a mock by patching the client factory is
    # unnecessary: probe builds its own adapter, so inject via the transport
    # through a patched AsyncClient would be indirect. Instead exercise probe's
    # logic against a monkeypatched adapter transport.
    import personal_data_mcp.feishu.connectivity as conn

    handler = paginating_fields_handler()
    original_init = FeishuAdapter.__init__

    def patched_init(self, credentials, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, credentials, **kwargs)

    monkeypatch.setattr(FeishuAdapter, "__init__", patched_init)

    report = run(conn.probe(full_env(), config=CONFIG))
    assert report["ledger_kind"] == "synthetic_test"
    assert report["tenant_token_obtained"] is True
    assert report["schema_status"] == "valid"
    assert report["schema_drifts"] == []
    assert report["tables"]["expense"]["field_count"] == len(
        SNAPSHOT["expense"]
    )

    text = json.dumps(report, ensure_ascii=False)
    # No raw id anywhere; hashes only.
    assert CONFIG.base_token not in text
    for table in CONFIG.tables.values():
        assert table.table_id not in text
    for fields in SNAPSHOT.values():
        for field in fields:
            assert field["field_id"] not in text
    assert report["base_token_hash"].startswith("h:")
    # Field names, which are not secret, are present.
    assert report["tables"]["expense"]["fields"][0]["name"] == "原始金额"


def test_probe_reports_schema_drift_instead_of_token_only_success(
    monkeypatch,
) -> None:
    import personal_data_mcp.feishu.connectivity as conn

    drifted_snapshot = copy.deepcopy(SNAPSHOT)
    drifted_snapshot["income"][0]["field_name"] = "错误金额列"
    handler = paginating_fields_handler(drifted_snapshot)
    original_init = FeishuAdapter.__init__

    def patched_init(self, credentials, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, credentials, **kwargs)

    monkeypatch.setattr(FeishuAdapter, "__init__", patched_init)

    report = run(conn.probe(full_env(), config=CONFIG))
    assert report["tenant_token_obtained"] is True
    assert report["schema_status"] == "drifted"
    assert report["schema_drifts"] == [
        {
            "table": "income",
            "logical_name": "amount",
            "kind": "name_changed",
        }
    ]


def test_g2_success_requires_both_a_token_and_a_valid_schema() -> None:
    assert probe_succeeded(
        {"tenant_token_obtained": True, "schema_status": "valid"}
    )
    assert not probe_succeeded(
        {"tenant_token_obtained": True, "schema_status": "drifted"}
    )
    assert not probe_succeeded(
        {"tenant_token_obtained": True}
    )


def test_probe_refuses_a_non_synthetic_base(monkeypatch) -> None:
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(LedgerSourceError):
        run(probe(env, config=CONFIG))
