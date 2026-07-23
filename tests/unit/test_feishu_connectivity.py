"""DEV-017 live-fetch, tested offline: base-source guard, pagination, probe.

Everything runs against a mock transport and an in-memory env dict. The point of
these tests is that the synthetic-test guard fails closed and the probe leaks
nothing, so the real run is safe -- the real run itself is a separate, gated
action and is not performed here.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    LedgerSourceError,
    load_base_source,
    require_synthetic_test_base,
)
from personal_data_mcp.feishu.connectivity import probe
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.feishu.endpoints import LIST_FIELDS


PLACEHOLDER = FeishuCredentials(app_id="cli_placeholder", app_secret="secret_xyz")


def clock():
    return 1000.0


def run(coro):
    return asyncio.run(coro)


def full_env() -> dict:
    return {
        "FEISHU_FINANCE_APP_ID": "cli_probe",
        "FEISHU_FINANCE_APP_SECRET": "shh",
        "FEISHU_FINANCE_BASE_TOKEN": "bascnTESTBASE0001",
        "FEISHU_FINANCE_TABLE_EXPENSE": "tblEXPENSE0001",
        "FEISHU_FINANCE_LEDGER_KIND": "synthetic_test",
    }


# --- the synthetic-test guard fails closed ----------------------------------


def test_a_non_synthetic_base_is_refused() -> None:
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(LedgerSourceError):
        require_synthetic_test_base(load_base_source(env))


def test_a_missing_ledger_kind_is_refused() -> None:
    env = full_env()
    del env["FEISHU_FINANCE_LEDGER_KIND"]
    with pytest.raises(LedgerSourceError):
        load_base_source(env)


def test_the_synthetic_base_is_accepted() -> None:
    source = require_synthetic_test_base(load_base_source(full_env()))
    assert source.is_synthetic_test
    assert source.tables == {"expense": "tblEXPENSE0001"}


# --- list_fields follows pagination -----------------------------------------


def paginating_fields_handler():
    pages = {
        None: {
            "code": 0,
            "data": {
                "items": [{"field_id": "fld1", "field_name": "原始金额", "type": 2}],
                "has_more": True,
                "page_token": "p2",
            },
        },
        "p2": {
            "code": 0,
            "data": {
                "items": [{"field_id": "fld2", "field_name": "名称", "type": 1}],
                "has_more": False,
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        token = request.url.params.get("page_token")
        return httpx.Response(200, json=pages[token])

    return handler


def test_list_fields_reads_every_page() -> None:
    async def scenario():
        async with FeishuAdapter(
            PLACEHOLDER,
            transport=httpx.MockTransport(paginating_fields_handler()),
            now=clock,
        ) as adapter:
            return await adapter.list_fields("bascn1", "tbl1")

    fields = run(scenario())
    assert [f["field_id"] for f in fields] == ["fld1", "fld2"]


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

    report = run(conn.probe(full_env()))
    assert report["ledger_kind"] == "synthetic_test"
    assert report["tenant_token_obtained"] is True
    assert report["tables"]["expense"]["field_count"] == 2

    text = json.dumps(report, ensure_ascii=False)
    # No raw id anywhere; hashes only.
    assert "bascnTESTBASE0001" not in text
    assert "tblEXPENSE0001" not in text
    assert "fld1" not in text and "fld2" not in text
    assert report["base_token_hash"].startswith("h:")
    # Field names, which are not secret, are present.
    assert report["tables"]["expense"]["fields"][0]["name"] == "原始金额"


def test_probe_refuses_a_non_synthetic_base(monkeypatch) -> None:
    env = full_env()
    env["FEISHU_FINANCE_LEDGER_KIND"] = "production"
    with pytest.raises(LedgerSourceError):
        run(probe(env))
