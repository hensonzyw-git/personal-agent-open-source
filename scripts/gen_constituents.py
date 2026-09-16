"""One-off: turn the S&P 500 constituents CSV (public-domain GitHub mirror) into
the checked-in snapshot JSON used by the breadth computation.

Run once, commit the resulting JSON, then forget this script. The snapshot is
deliberately *frozen* (ADR-0001: "fixed snapshot ... saving the snapshot
version per run") — breadth is computed against a stable list, and staleness is
surfaced via the snapshot metadata, not silently corrected.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

SRC = Path("/tmp/spx_constituents.csv")
DST = Path(__file__).resolve().parents[1] / "src" / "risk_monitor" / "assets" / "spx_constituents.json"
SNAPSHOT_DATE = "2026-08-21"


def main() -> None:
    tickers: list[str] = []
    with SRC.open() as f:
        for row in csv.DictReader(f):
            s = row["Symbol"].strip()
            if s:
                # Yahoo Finance expects BRK-B, not BRK.B.
                tickers.append(s.replace(".", "-"))

    seen: set[str] = set()
    ordered: list[str] = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(json.dumps({
        "source": "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
        "license": "Public Domain Dedication (ODC-PDDL) via the datasets/s-and-p-500-companies mirror",
        "snapshot_date": SNAPSHOT_DATE,
        "count": len(ordered),
        "tickers": ordered,
    }, indent=1) + "\n")

    print(f"wrote {DST} with {len(ordered)} tickers")


if __name__ == "__main__":
    main()
