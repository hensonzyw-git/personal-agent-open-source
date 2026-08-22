"""One-time: resolve each S&P 500 constituent's Tencent canonical code and bake
it into the frozen snapshot as ``tencent_codes``.

Tencent's batch realtime quote endpoint (`qt.gtimg.cn/q=usMMM,usAOS,...`) returns
one `v_us<CODE>="<f0>~<name>~<canonical>~..."` line per ticker, where field 2 is
the exchange-qualified canonical code (e.g. ``AAPL.OQ``, ``JPM.N``, ``BRK.B.N``).
The kline endpoint then takes ``us<canonical>``. The snapshot ticker list uses
hyphens for share classes (``BRK-B``) while Tencent uses dots (``BRK.B``), so the
query key is ``ticker.replace('-', '.')``.

Run once (from the Mac) and commit the snapshot; the client reads the frozen map,
so production never resolves exchanges at runtime.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

SNAPSHOT = Path("src/risk_monitor/assets/spx_constituents.json")
QUOTE_URL = "https://qt.gtimg.cn/q="
CHUNK = 50


def query_key(ticker: str) -> str:
    return ticker.replace("-", ".")


def main() -> int:
    data = json.loads(SNAPSHOT.read_text())
    tickers: list[str] = data["tickers"]

    codes: dict[str, str] = {}  # query_key -> canonical code (field 2)
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        q = ",".join("us" + query_key(t) for t in chunk)
        raw = urllib.request.urlopen(QUOTE_URL + q, timeout=25).read().decode("latin-1")
        for line in raw.splitlines():
            m = re.match(r'v_us([^=]+)="([^"]*)"', line.strip())
            if not m:
                continue
            fields = m.group(2).split("~")
            if len(fields) > 2 and fields[2]:
                codes[m.group(1)] = fields[2]
        time.sleep(0.4)

    tencent_codes: dict[str, str] = {}
    missing: list[str] = []
    for t in tickers:
        code = codes.get(query_key(t))
        if code:
            tencent_codes[t] = code
        else:
            missing.append(t)

    data["tencent_codes"] = tencent_codes
    SNAPSHOT.write_text(json.dumps(data, indent=1) + "\n")
    print(f"mapped {len(tencent_codes)}/{len(tickers)}")
    if missing:
        print("MISSING:", missing)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
