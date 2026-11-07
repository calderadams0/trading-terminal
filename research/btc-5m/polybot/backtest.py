"""Backtest the fair-value rule on real, already-resolved Polymarket BTC 5m windows.

Inputs
  - Polymarket resolution per window (Gamma API) and the Up token's price history
    (CLOB prices-history, fidelity=1 — roughly one point a minute).
  - BTC per-second prices from the Binance archive (data.binance.vision, 1s klines),
    published a day late, so only complete UTC days are covered.

Timing — the part that matters. A decision is made AT THE TIMESTAMP OF EACH POLYMARKET
PRICE POINT, using the BTC price of the second before it. An earlier version decided on
minute boundaries with whatever Polymarket point preceded it; those points were 30–50 s
stale, the model was effectively reading the future, and it "earned" +12c/share. Do not
reintroduce that.

Fill assumption: the recorded price is treated as a mid; we pay mid + SLIPPAGE (1c) plus
the taker fee. Optimistic for size and speed — a real bot queues behind faster ones.

Usage:  python3 research/btc-5m/polybot/backtest.py [min_edge=0.02]
Writes: research/btc-5m/out/polybot_backtest.md
Caches: research/btc-5m/data/polymarket_windows.json, data/binance_1s_<day>.csv
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA, OUT = ROOT / "data", ROOT / "out"
UA = {"User-Agent": "trading-terminal-research/0.1"}
SLIPPAGE = 0.01
DAYS = 3


def get(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def binance_1s(days: int) -> dict[int, tuple[float, float]]:
    """{unix_second: (open, close)} for the last `days` complete UTC days available."""
    out: dict[int, tuple[float, float]] = {}
    day = datetime.now(timezone.utc).date()
    got = 0
    for back in range(1, days + 4):
        if got == days:
            break
        d = day - timedelta(days=back)
        cache = DATA / f"binance_1s_{d}.csv"
        if not cache.exists():
            try:
                raw = get(f"https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-{d}.zip")
            except Exception:
                continue
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                DATA.mkdir(exist_ok=True)
                cache.write_bytes(z.read(z.namelist()[0]))
        for row in csv.reader(cache.read_text().splitlines()):
            if row and row[0].isdigit():
                ts = int(row[0])
                ts = ts // 1_000_000 if ts > 10**14 else ts // 1000
                out[ts] = (float(row[1]), float(row[4]))
        got += 1
        print(f"  BTC 1s {d}: ok", flush=True)
    return out


def fetch_window(t0: int) -> dict | None:
    ev = json.loads(get(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{t0}"))
    if not ev:
        return None
    m = ev[0]["markets"][0]
    prices = json.loads(m["outcomePrices"]) if m.get("closed") else None
    if prices not in (["1", "0"], ["0", "1"]):
        return None
    up_token = json.loads(m["clobTokenIds"])[0]
    hist = json.loads(get(f"https://clob.polymarket.com/prices-history?market={up_token}"
                          f"&startTs={t0 - 60}&endTs={t0 + 300}&fidelity=1"))["history"]
    return {"t0": t0, "up_won": prices[0] == "1", "hist": [(h["t"], h["p"]) for h in hist]}


def load_windows(lo: int, hi: int) -> list[dict]:
    cache = DATA / "polymarket_windows.json"
    have = {w["t0"]: w for w in json.loads(cache.read_text())} if cache.exists() else {}
    todo = [t for t in range((lo // 300 + 1) * 300, hi, 300) if t not in have]
    print(f"{len(have)} windows cached, fetching {len(todo)}…", flush=True)
    with ThreadPoolExecutor(8) as ex:
        for w in ex.map(fetch_window, todo):
            if w:
                have[w["t0"]] = w
    DATA.mkdir(exist_ok=True)
    cache.write_text(json.dumps(sorted(have.values(), key=lambda w: w["t0"])))
    return [w for t, w in sorted(have.items()) if lo <= t <= hi]


def sigma_before(sec: dict, t: int) -> float | None:
    """Per-sqrt-second volatility from 1-minute returns over the hour before t."""
    closes = [sec[s][1] for s in range(t - 3600, t, 60) if s in sec]
    if len(closes) < 50:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1) / 60)


def price_at(sec: dict, t: int) -> float | None:
    """Last BTC close at or before second t (looks back up to 10 s for gaps)."""
    for s in range(t, t - 10, -1):
        if s in sec:
            return sec[s][1]
    return None


def main() -> None:
    min_edge = float(sys.argv[1]) if len(sys.argv) > 1 else 0.02
    sec = binance_1s(DAYS)
    lo, hi = min(sec) + 3600, max(sec) - 300
    windows = load_windows(lo, hi)

    trades, agree = [], []
    brier: dict[int, list[tuple[float, float]]] = {k: [] for k in range(5)}
    for w in windows:
        t0 = w["t0"]
        s_open = sec.get(t0, (None,))[0]
        sig = sigma_before(sec, t0)
        if s_open is None or sig is None:
            continue
        tail = [sec[s][1] for s in range(t0 + 240, t0 + 300) if s in sec]
        if tail:  # does the Binance TWAP proxy reproduce Chainlink's resolution?
            agree.append((sum(tail) / len(tail) >= s_open) == w["up_won"])
        y = 1.0 if w["up_won"] else 0.0
        traded = False
        for ts, mid in w["hist"]:
            if not (t0 < ts <= t0 + 290):
                continue
            s_now = price_at(sec, ts - 1)          # strictly before the market's print
            if s_now is None:
                continue
            fair = core.fair_up(s_open, s_now, t0 + 300 - ts, sig)
            brier[(ts - t0) // 60].append(((mid - y) ** 2, (fair - y) ** 2))
            if traded:
                continue
            d = core.decide(fair, min(mid + SLIPPAGE, 0.99), min(1 - mid + SLIPPAGE, 0.99), min_edge)
            if d.side:
                traded = True
                trades.append({"t0": t0, "elapsed": ts - t0, "side": d.side, "price": d.price,
                               "edge": d.edge, "pnl": core.pnl_per_share(d.side, d.price, w["up_won"])})

    md = ["# Polymarket BTC 5m — fair-value backtest\n"]
    if windows:
        fmt = "%Y-%m-%d %H:%M"
        md.append(f"Windows: {len(windows)} resolved markets, {time.strftime(fmt, time.gmtime(windows[0]['t0']))} → "
                  f"{time.strftime(fmt, time.gmtime(windows[-1]['t0']))} UTC.\n")
    if agree:
        md.append(f"A Binance 60-second TWAP reproduced Chainlink's resolution in "
                  f"{100 * sum(agree) / len(agree):.1f}% of {len(agree)} windows.\n")
    md.append("## Who is better calibrated? (Brier score, lower is better)\n")
    md.append("Each row compares the Polymarket price with our fair value computed from BTC one second "
              "*before* that price was recorded.\n")
    md.append("| minute into window | n | Polymarket price | our fair value |\n|---|---:|---:|---:|")
    for k in range(5):
        if brier[k]:
            n = len(brier[k])
            md.append(f"| {k}–{k + 1} | {n} | {sum(a for a, _ in brier[k]) / n:.4f} | "
                      f"{sum(b for _, b in brier[k]) / n:.4f} |")
    md.append(f"\n## Paper trades (min edge {min_edge:.2f} $/share after fee; fill = mid + {SLIPPAGE:.2f})\n")
    if trades:
        n = len(trades)
        pnl = [t["pnl"] for t in trades]
        mean = sum(pnl) / n
        sd = math.sqrt(sum((p - mean) ** 2 for p in pnl) / max(n - 1, 1))
        wins = sum(1 for p in pnl if p > 0)
        md.append(f"- Trades: **{n}** of {len(windows)} windows, win rate **{100 * wins / n:.1f}%**")
        md.append(f"- Mean P&L: **{mean:+.4f} $/share** (95% CI {mean - 1.96 * sd / math.sqrt(n):+.4f} to "
                  f"{mean + 1.96 * sd / math.sqrt(n):+.4f}); total {sum(pnl):+.2f} $ at 1 share/trade")
        md.append(f"- Mean edge the model *thought* it had: {sum(t['edge'] for t in trades) / n:+.4f} $/share")
        md.append("\n| minute entered | trades | mean P&L $/share |\n|---|---:|---:|")
        for k in range(5):
            sub = [t["pnl"] for t in trades if t["elapsed"] // 60 == k]
            if sub:
                md.append(f"| {k}–{k + 1} | {len(sub)} | {sum(sub) / len(sub):+.4f} |")
    else:
        md.append("No trades cleared the edge threshold.")
    report = "\n".join(md) + "\n"
    OUT.mkdir(exist_ok=True)
    (OUT / "polybot_backtest.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
