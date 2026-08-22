"""Measure the raw-vs-adjusted close effect on breadth and ai_basket.

Runs from the Mac (home network reaches Yahoo). Fetches Yahoo chart (2y/1d)
for every S&P 500 constituent once, and from the single response extracts BOTH
the adjusted close (`indicators.adjclose`) and the raw close
(`indicators.quote.close`). The two series share the identical date axis, so
the only difference is the dividend adjustment — a fully controlled comparison.

It reproduces, verbatim, the two production formulas (risk_monitor.breadth and
risk_monitor.derive):

  * breadth flag = latest_close > mean(last 200 closes, including latest)
  * pct_vs_200dma = (latest / mean(last 200 closes) - 1) * 100

Output answers one question with hard numbers: if the daily job swaps Yahoo
adjusted close for a raw-close source (Tencent), how many percentage points
does breadth move, and does any ai_basket name's band change?
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
TICKERS = json.loads((REPO / "src" / "risk_monitor" / "assets" / "spx_constituents.json").read_text())["tickers"]
AI_NAMES = ["NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL"]
WINDOW = 200
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(ticker: str) -> tuple[str, list[tuple[str, float]], list[tuple[str, float]]]:
    """Return (ticker, adj_series, raw_series) aligned on identical dates."""
    for attempt in range(3):
        try:
            with httpx.Client(headers=HEADERS, timeout=30.0) as client:
                resp = client.get(URL.format(t=ticker), params={"range": "2y", "interval": "1d"})
            if resp.status_code == 404:
                return ticker, [], []
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return ticker, [], []
            break
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    else:
        return ticker, [], []

    chart = (resp.json().get("chart") or {}).get("result") or [{}]
    result = chart[0] or {}
    ts = result.get("timestamp") or []
    ind = result.get("indicators") or {}
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or []
    raw = (ind.get("quote") or [{}])[0].get("close") or []

    adj_out: list[tuple[str, float]] = []
    raw_out: list[tuple[str, float]] = []
    for i, t in enumerate(ts):
        a = adj[i] if i < len(adj) else None
        r = raw[i] if i < len(raw) else None
        if a is None or r is None:
            continue  # keep both series on identical dates
        d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        adj_out.append((d, float(a)))
        raw_out.append((d, float(r)))
    return ticker, adj_out, raw_out


def pct_vs_200dma(series: list[tuple[str, float]]) -> float | None:
    if len(series) < WINDOW:
        return None
    latest = series[-1][1]
    dma = sum(v for _, v in series[-WINDOW:]) / WINDOW
    if dma == 0:
        return None
    return round((latest / dma - 1.0) * 100.0, 4)


def above_200dma(series: list[tuple[str, float]]) -> bool | None:
    if len(series) < WINDOW:
        return None
    latest = series[-1][1]
    dma = sum(v for _, v in series[-WINDOW:]) / WINDOW
    return latest > dma


def main() -> int:
    t0 = time.time()
    data: dict[str, tuple[list, list]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, t): t for t in TICKERS}
        for fut in futs:
            t, adj, raw = fut.result()
            if adj:
                data[t] = (adj, raw)
            else:
                errors[t] = "no data"

    print(f"fetched {len(data)}/{len(TICKERS)} tickers in {time.time()-t0:.1f}s "
          f"({len(errors)} empty)")

    # breadth: % above 200dma under each convention
    def breadth(which: int) -> tuple[float, int, int]:
        flags, n_signal = [], 0
        for t, (adj, raw) in data.items():
            s = adj if which == 0 else raw
            f = above_200dma(s)
            if f is None:
                continue
            n_signal += 1
            flags.append(f)
        if not flags:
            return 0.0, 0, 0
        return round(sum(flags) / len(flags) * 100.0, 4), len(flags), sum(flags)

    b_adj, n_adj, up_adj = breadth(0)
    b_raw, n_raw, up_raw = breadth(1)
    print(f"\nBREADTH (S&P 500, % above 200dma):")
    print(f"  adjusted close: {b_adj}%  ({up_adj}/{n_adj})")
    print(f"  raw close:      {b_raw}%  ({up_raw}/{n_raw})")
    print(f"  delta:          {round(b_raw - b_adj, 4)} pp")

    # per-ticker flips
    flips = []
    for t, (adj, raw) in data.items():
        fa = above_200dma(adj)
        fr = above_200dma(raw)
        if fa is None or fr is None or fa == fr:
            continue
        pa = pct_vs_200dma(adj)
        pr = pct_vs_200dma(raw)
        flips.append((t, fa, fr, pa, pr))
    print(f"  tickers flipping above/below: {len(flips)}")
    for t, fa, fr, pa, pr in sorted(flips, key=lambda x: abs((x[4] or 0) - (x[3] or 0)))[:10]:
        print(f"    {t}: adj={fa} raw={fr}  pct adj={pa} raw={pr}")

    # ai_basket: per-name pct_vs_200dma
    print(f"\nAI_BASKET (per-name pct_vs_200dma):")
    for t in AI_NAMES:
        if t not in data:
            print(f"  {t}: MISSING")
            continue
        adj, raw = data[t]
        pa = pct_vs_200dma(adj)
        pr = pct_vs_200dma(raw)
        print(f"  {t}: adj={pa}%  raw={pr}%  delta={round((pr or 0)-(pa or 0), 4)}")

    # aggregate raw-vs-adj close deviation on the last common close
    import statistics
    devs = []
    for t, (adj, raw) in data.items():
        if adj and raw:
            a, r = adj[-1][1], raw[-1][1]
            if a:
                devs.append((r - a) / a * 100.0)
    if devs:
        print(f"\nlatest-close raw-vs-adj deviation over {len(devs)} names:")
        print(f"  median={statistics.median(devs):.4f}%  max_abs={max(abs(x) for x in devs):.4f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
