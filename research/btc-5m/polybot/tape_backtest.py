"""The honest version of backtest.py: decide and fill against Polymarket's real trade tape.

backtest.py uses CLOB prices-history, a ~1/minute midpoint sample that is itself ~55 s
stale relative to its own timestamp. Any model fed fresh BTC prices "beats" it — a
lookahead artifact (we measured +7..+10 c/share of fake edge; see README.md).

Here, for each window:
  - market price  = the last real trade (Data API v2 /trades), converted to an Up price
  - BTC price     = Binance 1s close of the second before that trade
  - decision      = core.decide() on that pair, at most once per second
  - fill          = the price of the next real trade on our side >= LATENCY s later,
                    plus the taker fee. A print someone else actually got is the best
                    case for us, so this still flatters — but not by a minute.

Usage:  python3 research/btc-5m/polybot/tape_backtest.py [min_edge=0.05] [latency_s=1]
Writes: research/btc-5m/out/polybot_tape_backtest.md
"""

from __future__ import annotations

import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402
from backtest import DATA, OUT, binance_1s, get, load_windows, price_at, sigma_before  # noqa: E402

TAPES = DATA / "tapes"


def condition_id(t0: int) -> str | None:
    ev = json.loads(get(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{t0}"))
    return ev[0]["markets"][0]["conditionId"] if ev else None


def tape(t0: int) -> list[tuple[int, float]] | None:
    """[(ts, up_price)] ascending, every trade in the window. Cached per window."""
    path = TAPES / f"{t0}.json"
    if path.exists():
        return [tuple(x) for x in json.loads(path.read_text())]
    cid = condition_id(t0)
    if not cid:
        return None
    rows, cursor = [], None
    for _ in range(30):
        url = f"https://data-api.polymarket.com/v2/trades?conditionId={cid}&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        page = json.loads(get(url))
        for t in page["data"]:
            p = float(t["price"])
            rows.append((int(t["timestamp"]), p if t["outcome"] == "Up" else 1 - p))
        pg = page.get("pagination") or {}
        if not pg.get("has_more"):
            break
        cursor = pg.get("next_cursor")
    rows.sort()
    TAPES.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


def main() -> None:
    min_edge = float(sys.argv[1]) if len(sys.argv) > 1 else 0.05
    latency = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sec = binance_1s(3)
    windows = load_windows(min(sec) + 3600, max(sec) - 300)
    print(f"loading tapes for {len(windows)} windows…", flush=True)
    with ThreadPoolExecutor(8) as ex:
        tapes = dict(zip([w["t0"] for w in windows], ex.map(tape, [w["t0"] for w in windows])))

    trades, placebo, brier = [], [], []
    for w in windows:
        t0, tp = w["t0"], tapes.get(w["t0"])
        s_open, sig = sec.get(t0, (None,))[0], sigma_before(sec, t0)
        if not tp or s_open is None or sig is None:
            continue
        y = 1.0 if w["up_won"] else 0.0
        inside = [(ts, u) for ts, u in tp if t0 < ts <= t0 + 290]
        last_sec, done = None, False
        for i, (ts, u) in enumerate(inside):
            if ts == last_sec:
                continue
            last_sec = ts
            s_now = price_at(sec, ts - 1)
            if s_now is None:
                continue
            fair = core.fair_up(s_open, s_now, t0 + 300 - ts, sig)
            brier.append(((u - y) ** 2, (fair - y) ** 2))
            if done:
                continue
            d = core.decide(fair, u, 1 - u, min_edge)
            if not d.side:
                continue
            fill = next((uu for tts, uu in inside[i + 1:] if tts >= ts + latency), None)
            if fill is None:
                continue
            price = fill if d.side == "Up" else 1 - fill
            if not 0.01 <= price <= 0.99:
                continue
            trades.append({"elapsed": ts - t0, "side": d.side, "price": price, "seen": d.price,
                           "edge": d.edge, "pnl": core.pnl_per_share(d.side, price, w["up_won"])})
            # placebo: same moment, same fill, opposite side — what does random direction earn?
            opp = "Down" if d.side == "Up" else "Up"
            placebo.append(core.pnl_per_share(opp, 1 - price, w["up_won"]))
            done = True

    md = ["# Polymarket BTC 5m — fair-value backtest on the real trade tape\n",
          f"Windows: {len(windows)}; with a tape: {sum(1 for v in tapes.values() if v)}; "
          f"trades in tapes: {sum(len(v) for v in tapes.values() if v):,}.\n",
          f"Rule: min edge {min_edge:.2f} $/share after fee; fill = next real print ≥ {latency}s later.\n"]
    if brier:
        md.append(f"Brier score at every tape print — last trade price: **{sum(a for a, _ in brier) / len(brier):.4f}**, "
                  f"our fair value: **{sum(b for _, b in brier) / len(brier):.4f}** (lower is better; n={len(brier):,}).\n")
    if trades:
        n = len(trades)
        pnl = [t["pnl"] for t in trades]
        mean = sum(pnl) / n
        se = math.sqrt(sum((p - mean) ** 2 for p in pnl) / max(n - 1, 1) / n)
        slip = sum(t["price"] - t["seen"] for t in trades) / n
        md.append(f"- Trades: **{n}**, win rate {100 * sum(p > 0 for p in pnl) / n:.1f}%")
        md.append(f"- Mean P&L: **{mean:+.4f} $/share** (95% CI {mean - 1.96 * se:+.4f} to {mean + 1.96 * se:+.4f})")
        md.append(f"- Edge the model saw: {sum(t['edge'] for t in trades) / n:+.4f}; "
                  f"price moved against us by {slip:+.4f} between signal and fill")
        md.append(f"- Placebo (opposite side, same fills): {sum(placebo) / len(placebo):+.4f} $/share")
        md.append("\n| entered at second | trades | mean P&L $/share |\n|---|---:|---:|")
        for lo, hi in [(0, 60), (60, 120), (120, 180), (180, 240), (240, 300)]:
            sub = [t["pnl"] for t in trades if lo <= t["elapsed"] < hi]
            if sub:
                md.append(f"| {lo}–{hi} | {len(sub)} | {sum(sub) / len(sub):+.4f} |")
    else:
        md.append("No trades.")
    report = "\n".join(md) + "\n"
    OUT.mkdir(exist_ok=True)
    (OUT / "polybot_tape_backtest.md").write_text(report)
    print(report)


if __name__ == "__main__":
    t = time.time()
    main()
    print(f"({time.time() - t:.0f}s)")
