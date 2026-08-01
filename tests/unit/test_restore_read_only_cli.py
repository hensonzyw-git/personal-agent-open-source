"""Restore smoke uses service binaries without composing production writes."""

from __future__ import annotations

import sys
from pathlib import Path

from personal_data_mcp import cli
from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import create_database_engine


def test_mcp_restore_mode_never_composes_finance_tools(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "finance.sqlite"
    engine = create_database_engine(database)
    db.upgrade(engine, "head")
    engine.dispose()
    config = Path(__file__).parents[1] / "fixtures" / "ledger" / "config.synthetic.json"

    served: list[object] = []
    app = object()
    monkeypatch.setattr(cli, "build_app", lambda *_args, **_kwargs: app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda value, **_kwargs: served.append(value))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("restore mode composed Finance network dependencies")

    monkeypatch.setattr(cli, "_serve_with_finance_tools", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "personal-data-mcp",
            "--database",
            str(database),
            "--ledger-config",
            str(config),
            "--restore-read-only",
        ],
    )

    cli.main()

    assert served == [app]
