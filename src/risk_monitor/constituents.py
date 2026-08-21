"""Constituent snapshot access (ADR-0001: a *frozen* snapshot, not a live list).

The ticker list is checked in as ``assets/spx_constituents.json`` with a
``snapshot_date``; staleness is surfaced via ``snapshot_meta`` rather than
silently corrected by re-pulling the live index membership. It lives under
``assets/`` rather than ``data/`` because the root ``.gitignore`` ignores every
``data/`` directory (runtime DBs and exports) — the constituents list is public
reference data and must ship with the package.
"""

from __future__ import annotations

import json
from pathlib import Path

_SNAPSHOT = Path(__file__).resolve().parent / "assets" / "spx_constituents.json"


def load_tickers(path: str | None = None) -> list[str]:
    p = Path(path) if path else _SNAPSHOT
    return list(json.loads(p.read_text())["tickers"])


def snapshot_meta(path: str | None = None) -> dict:
    p = Path(path) if path else _SNAPSHOT
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if k != "tickers"}
