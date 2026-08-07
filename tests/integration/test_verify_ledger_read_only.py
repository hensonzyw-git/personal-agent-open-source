"""The read-only production ledger path: it must be unable to write, structurally.

Henson's 2026-08-03 staged-G5 decision opened read-only access to the real annual
ledger so two §13.2 gates could close. The whole value of that decision rests on
one property -- that this path *cannot* write -- so the tests here are mostly
about absence, and the absence is checked in the source rather than by driving a
handler, because "I did not observe a write" is much weaker than "there is no
code here that could perform one".
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from personal_data_mcp.feishu.base_source import (
    BaseSource,
    LedgerSourceError,
    require_configured_base,
    require_synthetic_test_base,
    require_write_base,
)
from personal_data_mcp.finance import verify_ledger_cli as cli


MODULE = Path(cli.__file__)

APPROVED_TABLES = {
    "expense": "tblEXPENSE",
    "income": "tblINCOME",
    "family_fund": "tblFAMILY",
}


def _source(**overrides) -> BaseSource:
    fields = {
        "base_token": "bascPRODUCTION",
        "ledger_kind": "production",
        "tables": dict(APPROVED_TABLES),
    }
    fields.update(overrides)
    return BaseSource(**fields)


def _bind(source: BaseSource, *, kind: str = "production") -> BaseSource:
    return require_configured_base(
        source,
        approved_base_token="bascPRODUCTION",
        approved_tables=dict(APPROVED_TABLES),
        approved_ledger_kind=kind,
    )


# --- the guard: every identity check survives, only the synthetic clause goes --


def test_a_matching_production_base_binds() -> None:
    assert _bind(_source()).ledger_kind == "production"


def test_a_different_base_token_is_refused() -> None:
    with pytest.raises(LedgerSourceError, match="does not match"):
        _bind(_source(base_token="bascSOMEONEELSE"))


def test_a_different_kind_is_refused() -> None:
    with pytest.raises(LedgerSourceError, match="kind"):
        _bind(_source(ledger_kind="synthetic_test"))


@pytest.mark.parametrize(
    "tables",
    [
        {**APPROVED_TABLES, "expense": "tblSOMETHINGELSE"},
        {"expense": "tblEXPENSE"},
        {**APPROVED_TABLES, "extra": "tblEXTRA"},
    ],
    ids=["swapped", "missing", "extra"],
)
def test_a_table_mapping_that_is_not_exactly_the_approved_one_is_refused(
    tables,
) -> None:
    with pytest.raises(LedgerSourceError, match="tables"):
        _bind(_source(tables=tables))


def test_the_write_guard_still_refuses_production() -> None:
    """The read door must not have opened the write door.

    `require_synthetic_test_base` is what keeps production writes behind G5, and
    adding a read path is exactly the change that could weaken it by accident.
    """
    with pytest.raises(LedgerSourceError, match="synthetic"):
        require_synthetic_test_base(
            _source(),
            approved_base_token="bascPRODUCTION",
            approved_tables=dict(APPROVED_TABLES),
            approved_ledger_kind="production",
        )


# --- the write-path binding: same identity checks, explicit G5-only naming ----
#
# `require_write_base` is the G5-authorized write-path twin of
# `require_configured_base`. It must enforce exactly the same identity checks
# (kind, constant-time token compare, exact table mapping) so that opening the
# write door cannot widen which Base the write path will accept.


def _write_bind(source: BaseSource, *, kind: str = "production") -> BaseSource:
    return require_write_base(
        source,
        approved_base_token="bascPRODUCTION",
        approved_tables=dict(APPROVED_TABLES),
        approved_ledger_kind=kind,
    )


def test_write_binding_accepts_a_matching_production_base() -> None:
    assert _write_bind(_source()).ledger_kind == "production"


def test_write_binding_accepts_a_matching_synthetic_base() -> None:
    synthetic = _source(ledger_kind="synthetic_test")
    assert _write_bind(synthetic, kind="synthetic_test").ledger_kind == "synthetic_test"


def test_write_binding_refuses_a_different_base_token() -> None:
    with pytest.raises(LedgerSourceError, match="does not match"):
        _write_bind(_source(base_token="bascSOMEONEELSE"))


def test_write_binding_refuses_a_different_kind() -> None:
    with pytest.raises(LedgerSourceError, match="kind"):
        _write_bind(_source(ledger_kind="synthetic_test"))


@pytest.mark.parametrize(
    "tables",
    [
        {**APPROVED_TABLES, "expense": "tblSOMETHINGELSE"},
        {"expense": "tblEXPENSE"},
        {**APPROVED_TABLES, "extra": "tblEXTRA"},
    ],
    ids=["swapped", "missing", "extra"],
)
def test_write_binding_refuses_a_table_mapping_that_is_not_exact(tables) -> None:
    with pytest.raises(LedgerSourceError, match="tables"):
        _write_bind(_source(tables=tables))


# --- the structural guarantee --------------------------------------------------


def _imported_names() -> set[str]:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_the_read_only_path_imports_nothing_that_can_write() -> None:
    """Absence, checked in the source.

    Each of these is a component a write needs. Without `sessions` there is no
    execution store to prepare a write in; without the write handlers there is
    nothing that creates a record; and without the reconciler there is nothing
    that replays one. `reconcile_write` is the sharpest of the three, because it
    calls `adapter.create_record` on its own initiative to finish a stranded
    write -- against the real ledger, that is precisely what must not happen.
    """
    imported = _imported_names()
    forbidden = {
        "personal_data_mcp.server.finance_write.FinanceWriteDependencies",
        "personal_data_mcp.server.finance_write.build_expense_handler",
        "personal_data_mcp.server.finance_write.build_income_handler",
        "personal_data_mcp.server.finance_write.build_family_fund_handler",
        "personal_data_mcp.finance.reconciler.reconcile_write",
        "personal_data_mcp.finance.reconciler.reconcile_expense",
        "personal_data_mcp.storage.engine.session_factory",
        "personal_data_mcp.storage.engine.create_database_engine",
        "personal_data_mcp.crypto.keys.load_data_keyring",
    }
    assert not (imported & forbidden), sorted(imported & forbidden)


def test_the_read_only_path_never_names_a_write_call() -> None:
    """Belt and braces: the adapter it holds *does* have `create_record`.

    The dependency type cannot express a write, but the adapter is shared with
    the write path, so the residual guarantee is "no caller here reaches it".
    That is a property of this file's text, so it is checked as one.
    """
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("create_record", "update_record", "delete_record"):
        assert forbidden not in called, forbidden


def test_the_read_only_dependency_type_carries_nothing_a_write_needs() -> None:
    fields = set(cli.ReadOnlyLedger.__dataclass_fields__)
    assert fields == {"adapter", "source", "config", "now"}
    for absent in ("sessions", "keyring", "fx"):
        assert absent not in fields


def test_schema_validation_lists_fields_and_reads_no_rows() -> None:
    """The gate is "touches only approved Base/table/fields".

    `list_fields` returns definitions. If this ever started reading rows to
    validate a schema, it would be reading the real ledger's contents to check
    its shape, which is not what the gate authorises.
    """
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    validator = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "validate_live_schema"
    )
    called = {
        node.func.attr
        for node in ast.walk(validator)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "list_fields" in called
    for row_read in ("list_records", "search_records", "get_record"):
        assert row_read not in called


# --- pagination ----------------------------------------------------------------


class FakeQuery:
    """Returns a fixed chain of pages, and records what it was asked for."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    async def __call__(self, arguments, **kwargs):
        self.calls.append(dict(arguments))
        return self.pages[len(self.calls) - 1]


@pytest.fixture()
def ledger(monkeypatch):
    async def no_validation(_ledger):
        return object()

    monkeypatch.setattr(cli, "validate_live_schema", no_validation)
    return cli.ReadOnlyLedger(adapter=object(), source=object(), config=object())


def test_page_all_follows_the_cursor_to_the_end(monkeypatch, ledger) -> None:
    query = FakeQuery(
        [
            {"items": [1], "next_cursor": "c1"},
            {"items": [2], "next_cursor": "c2"},
            {"items": [3], "next_cursor": None},
        ]
    )
    monkeypatch.setattr(cli, "query_expenses", query)

    pages = cli.run_query.__wrapped__ if hasattr(cli.run_query, "__wrapped__") else cli.run_query
    import asyncio

    result = asyncio.run(
        pages(ledger, {"view": "records"}, cursor_secret=b"x" * 32, page_all=True)
    )

    assert len(result) == 3
    # A continuation must not restate its filters, per the query contract.
    assert query.calls[1] == {"view": "records", "cursor": "c1"}
    assert query.calls[2] == {"view": "records", "cursor": "c2"}


def test_without_page_all_only_the_first_page_is_read(monkeypatch, ledger) -> None:
    query = FakeQuery([{"items": [1], "next_cursor": "c1"}])
    monkeypatch.setattr(cli, "query_expenses", query)
    import asyncio

    result = asyncio.run(
        cli.run_query(
            ledger, {"view": "records"}, cursor_secret=b"x" * 32, page_all=False
        )
    )
    assert len(result) == 1
    assert len(query.calls) == 1


def test_an_endless_cursor_is_refused_rather_than_followed(monkeypatch, ledger) -> None:
    """A cursor bug must become a refusal, not an unbounded read of the ledger."""
    monkeypatch.setattr(cli, "MAX_PAGES", 3)
    query = FakeQuery([{"items": [1], "next_cursor": "same"}] * 10)
    monkeypatch.setattr(cli, "query_expenses", query)
    import asyncio

    with pytest.raises(LedgerSourceError, match="refusing to keep reading"):
        asyncio.run(
            cli.run_query(
                ledger, {"view": "records"}, cursor_secret=b"x" * 32, page_all=True
            )
        )


# --- the cursor secret ---------------------------------------------------------


def test_the_cursor_secret_must_be_configured_and_long_enough() -> None:
    with pytest.raises(SystemExit):
        cli._cursor_secret(env={})
    with pytest.raises(SystemExit):
        cli._cursor_secret(env={cli.CURSOR_SECRET_ENV: "   "})
    with pytest.raises(SystemExit):
        cli._cursor_secret(env={cli.CURSOR_SECRET_ENV: "aGVsbG8="})  # 5 bytes


def test_a_long_enough_base64url_secret_decodes() -> None:
    import base64

    raw = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    assert cli._cursor_secret(env={cli.CURSOR_SECRET_ENV: raw}) == b"k" * 32


def test_only_base64url_is_accepted_because_hex_overlaps_with_it() -> None:
    """The bug a first draft had, pinned so it cannot come back.

    `"ab" * 32` is 64 hex characters AND valid base64url. A decoder that tries
    base64 then hex accepts it as base64 and yields a different 48-byte secret
    than the operator meant -- silently, and self-consistently, until something
    else reads the same variable.
    """
    import base64

    ambiguous = "ab" * 32
    assert cli._cursor_secret(env={cli.CURSOR_SECRET_ENV: ambiguous}) == (
        base64.urlsafe_b64decode(ambiguous)
    )
    assert cli._cursor_secret(
        env={cli.CURSOR_SECRET_ENV: ambiguous}
    ) != bytes.fromhex(ambiguous)

    with pytest.raises(SystemExit, match="base64url"):
        cli._cursor_secret(env={cli.CURSOR_SECRET_ENV: "not valid base64!!"})


# --- the composition itself, with real objects ---------------------------------


def test_open_ledger_builds_a_real_adapter(tmp_path, monkeypatch) -> None:
    """Every test above used `object()` for the adapter, so none of them ran the
    real constructor -- and the real one takes a required keyword-only `now`.

    The result was a `TypeError` on the first live run against the annual
    ledger, after the config had already been frozen. Exactly §5.1's warning:
    a fake built from the same assumptions as the code confirms only those
    assumptions. This test constructs the production object.
    """
    from personal_data_mcp.feishu.adapter import FeishuAdapter
    from personal_data_mcp.finance.ledger_config import LedgerConfig

    config_path = tmp_path / "annual.json"
    config = _minimal_production_config()
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    for name, value in {
        "FEISHU_FINANCE_APP_ID": "cli_probe",
        "FEISHU_FINANCE_APP_SECRET": "secret",
        "FEISHU_FINANCE_BASE_TOKEN": config.base_token,
        "FEISHU_FINANCE_LEDGER_KIND": config.ledger_kind,
        "FEISHU_FINANCE_TABLE_EXPENSE": config.tables["expense"].table_id,
        "FEISHU_FINANCE_TABLE_INCOME": config.tables["income"].table_id,
        "FEISHU_FINANCE_TABLE_FAMILY_FUND": config.tables["family_fund"].table_id,
    }.items():
        monkeypatch.setenv(name, value)

    import asyncio

    ledger = asyncio.run(cli.open_ledger(config_path))
    try:
        assert isinstance(ledger.adapter, FeishuAdapter)
        assert isinstance(ledger.config, LedgerConfig)
        assert ledger.source.ledger_kind == "production"
    finally:
        asyncio.run(ledger.adapter.aclose())


def _minimal_production_config():
    """The smallest config the loader accepts, borrowed from the freeze tests."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    from test_ledger_config_freeze import CONFIG

    return CONFIG.model_copy(update={"ledger_kind": "production"})
