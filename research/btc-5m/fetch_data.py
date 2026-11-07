"""Download BTC 5-minute candles for research. Read-only; touches no account.

Sources
  - Binance public archive (data.binance.vision): BTCUSDT spot 5m, monthly zips.
    Long history, which is what makes the statistics in study.py mean anything.
  - Hyperliquid info API: BTC perp 5m, the ~5000 most recent candles (~17 days).
    The venue this repo actually deploys to; used to check the archive agrees.

Usage:  python3 research/btc-5m/fetch_data.py [months=12]
Columns: ts,open,high,low,close,volume,taker_buy_volume (blank on Hyperliquid)
Writes: research/btc-5m/data/binance_btcusdt_5m.csv, hyperliquid_btc_5m.csv
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

DATA = Path(__file__).parent / "data"
UA = {"User-Agent": "trading-terminal-research/0.1"}


def get(url: str, body: bytes | None = None) -> bytes:
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def months_back(n: int) -> list[str]:
    """The n most recent *complete* months, oldest first, as YYYY-MM."""
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return out[::-1]


def binance(months: int) -> Path:
    rows: list[list[str]] = []
    for ym in months_back(months):
        url = f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-{ym}.zip"
        try:
            raw = get(url)
        except Exception as e:  # a month not yet published is not fatal
            print(f"  skip {ym}: {e}")
            continue
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            text = z.read(z.namelist()[0]).decode()
        n = 0
        for line in csv.reader(text.splitlines()):
            if not line or not line[0].isdigit():
                continue
            ts = int(line[0])
            if ts > 10**14:  # Binance switched spot archives to microseconds in 2025
                ts //= 1000
            rows.append([str(ts), line[1], line[2], line[3], line[4], line[5], line[9]])
            n += 1
        print(f"  {ym}: {n} candles")
    rows.sort(key=lambda r: int(r[0]))
    out = DATA / "binance_btcusdt_5m.csv"
    write(out, rows)
    return out


def hyperliquid() -> Path:
    body = json.dumps({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "5m", "startTime": 0}}).encode()
    candles = json.loads(get("https://api.hyperliquid.xyz/info", body))
    rows = [[str(c["t"]), c["o"], c["h"], c["l"], c["c"], c["v"], ""] for c in candles]
    out = DATA / "hyperliquid_btc_5m.csv"
    write(out, rows)
    print(f"  hyperliquid: {len(rows)} candles")
    return out


def write(path: Path, rows: list[list[str]]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "taker_buy_volume"])
        w.writerows(rows)


if __name__ == "__main__":
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    print(f"Binance archive, {months} months:")
    print(" ->", binance(months))
    print("Hyperliquid recent:")
    print(" ->", hyperliquid())
