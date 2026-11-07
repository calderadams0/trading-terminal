"""Live PAPER trader for Polymarket "Bitcoin Up or Down" 5-minute markets.

Forward-testing is the only test this strategy family has not been able to fool (see
README.md: two backtests found fake edge before the trade tape showed ~0). This runs the
same core.decide() rule live, against the real order book's best ask, and records what
would have happened. It holds no keys and contains no order-placement code.

Each window:
  1. at the first poll after the window opens, record BTC (Hyperliquid mid) as S_open
     (windows already in progress when the bot starts are skipped)
  2. every POLL seconds: BTC mid, both tokens' best ask and size, fair value -> decide()
  3. at most one paper buy per window, sized to min(STAKE / ask, size at the ask)
  4. once Polymarket resolves the window, score it and append to out/paper_trades.csv

Usage:  python3 research/btc-5m/polybot/paper.py [minutes=60] [min_edge=0.05] [stake_usd=10]
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out"
LOG = OUT / "paper_trades.csv"
UA = {"User-Agent": "trading-terminal-research/0.1"}
POLL = 2.0
STOP_BEFORE_END = 10   # no new entries in the last seconds of a window


def get_json(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA, **({"Content-Type": "application/json"} if body is not None else {}))
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=10) as r:
        return json.loads(r.read())


def btc_mid() -> float:
    return float(get_json("https://api.hyperliquid.xyz/info", {"type": "allMids"})["BTC"])


def sigma_now() -> float:
    start = int((time.time() - 3700) * 1000)
    rows = get_json("https://api.hyperliquid.xyz/info",
                    {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": start}})
    closes = [float(c["c"]) for c in rows][-61:-1]   # completed minutes only
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1) / 60)


def market(t0: int) -> dict | None:
    ev = get_json(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{t0}")
    return ev[0]["markets"][0] if ev else None


def best_ask(token: str) -> tuple[float, float]:
    book = get_json(f"https://clob.polymarket.com/book?token_id={token}")
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    return min(asks) if asks else (1.0, 0.0)


def resolve(t0: int) -> bool | None:
    m = market(t0)
    if not m or not m.get("closed"):
        return None
    prices = json.loads(m["outcomePrices"])
    return {("1", "0"): True, ("0", "1"): False}.get(tuple(prices))


def log(row: dict) -> None:
    OUT.mkdir(exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    min_edge = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
    stake = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    stop_at = time.time() + minutes * 60
    pending: list[dict] = []
    first_window = int(time.time()) // 300 * 300 + 300   # skip the window in progress
    print(f"paper trading until {time.strftime('%H:%M:%S', time.localtime(stop_at))}, "
          f"min_edge={min_edge}, stake=${stake}. First window opens at "
          f"{time.strftime('%H:%M:%S', time.localtime(first_window))}.", flush=True)

    t0 = None
    while time.time() < stop_at or pending:
        now = time.time()
        cur = int(now) // 300 * 300
        try:
            if cur >= first_window and cur != t0 and now < stop_at:
                t0 = cur
                m = market(t0)
                up_tok, down_tok = json.loads(m["clobTokenIds"]) if m else (None, None)
                s_open, sig, traded = btc_mid(), sigma_now(), False
                print(f"[{time.strftime('%H:%M:%S')}] window {t0}: S_open={s_open:.1f} "
                      f"sigma={sig * math.sqrt(300) * 100:.3f}%/5m", flush=True)
            if t0 and up_tok and not traded and now < t0 + 300 - STOP_BEFORE_END and now < stop_at:
                s_now = btc_mid()
                (ask_up, sz_up), (ask_dn, sz_dn) = best_ask(up_tok), best_ask(down_tok)
                fair = core.fair_up(s_open, s_now, t0 + 300 - now, sig)
                d = core.decide(fair, ask_up, ask_dn, min_edge)
                if d.side:
                    size = min(stake / d.price, sz_up if d.side == "Up" else sz_dn)
                    if size >= 5:   # Polymarket's minimum order size
                        traded = True
                        pending.append({"t0": t0, "entered_s": round(now - t0, 1), "side": d.side,
                                        "price": d.price, "shares": round(size, 2), "fair": round(d.fair, 4),
                                        "edge": round(d.edge, 4), "btc_open": s_open, "btc_entry": s_now})
                        print(f"  PAPER BUY {d.side} {size:.1f} @ {d.price:.3f} (fair {d.fair:.3f}, "
                              f"edge {d.edge:+.3f}) at +{now - t0:.0f}s", flush=True)
            for p in list(pending):
                if time.time() > p["t0"] + 300 + 20:
                    up_won = resolve(p["t0"])
                    if up_won is not None:
                        pnl = core.pnl_per_share(p["side"], p["price"], up_won) * p["shares"]
                        p.update(up_won=up_won, pnl_usd=round(pnl, 4))
                        log(p)
                        pending.remove(p)
                        print(f"  RESOLVED {p['t0']}: {'Up' if up_won else 'Down'} won -> {pnl:+.2f} USD", flush=True)
                    elif time.time() > p["t0"] + 300 + 900:
                        pending.remove(p)   # unresolved after 15 min; drop rather than hang
        except Exception as e:   # network blips must not kill a long forward test
            print(f"  ({type(e).__name__}: {e})", flush=True)
        time.sleep(POLL)

    if LOG.exists():
        rows = list(csv.DictReader(LOG.open()))
        tot = sum(float(r["pnl_usd"]) for r in rows)
        print(f"\nAll-time paper log: {len(rows)} trades, P&L {tot:+.2f} USD ({LOG})")


if __name__ == "__main__":
    main()
