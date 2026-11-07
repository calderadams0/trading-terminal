"""Live PAPER trader for Polymarket "Bitcoin Up or Down" 5-minute markets.

Forward-testing is the only test this strategy family has not been able to fool (see
README.md: two backtests found fake edge before the trade tape showed ~0). This runs
core.decide() live against the real order book's best ask and records what would have
happened. It holds no keys and contains no order-placement code.

Prices come from Chainlink BTC/USD (chainlink.py) — the feed these markets settle on —
not from an exchange. The first version priced off Hyperliquid, whose BTC sits ~$88
above Chainlink, and "found" an edge in every window's first second that was only that
gap.

Each window:
  1. S_open = Chainlink price stamped at the window's first second. Windows already
     running when the bot starts (or with a feed gap at the open) are skipped.
  2. every POLL seconds after MIN_ELAPSED: both tokens' best ask and size, and two fair
     values: from the latest Chainlink print, and from a "lead" price — Hyperliquid now
     minus the median Hyperliquid-Chainlink gap of the last 5 minutes. Chainlink trails
     the exchanges by seconds, and on the trade tape the lead-adjusted price forecast
     better (Brier 0.159 vs 0.174). Decisions use the lead fair once 5 minutes of gap
     samples exist. At most one paper buy per window.
  3. once Polymarket resolves it, score it and append to out/paper_trades.csv, together
     with Polymarket's own published priceToBeat so S_open can be checked against it.

Usage:  python3 research/btc-5m/polybot/paper.py [minutes=60] [min_edge=0.05] [stake_usd=10] [min_elapsed_s=10]
Needs:  pip install websockets
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
from chainlink import ChainlinkFeed  # noqa: E402

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


def hl_mid() -> float:
    return float(get_json("https://api.hyperliquid.xyz/info", {"type": "allMids"})["BTC"])


def hl_sigma() -> float:
    """Fallback volatility (Hyperliquid 1m) until the feed has an hour of history."""
    start = int((time.time() - 3700) * 1000)
    rows = get_json("https://api.hyperliquid.xyz/info",
                    {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": start}})
    closes = [float(c["c"]) for c in rows][-61:-1]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1) / 60)


def event(t0: int) -> dict | None:
    ev = get_json(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{t0}")
    return ev[0] if ev else None


def best_ask(token: str) -> tuple[float, float]:
    book = get_json(f"https://clob.polymarket.com/book?token_id={token}")
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    return min(asks) if asks else (1.0, 0.0)


def resolve(t0: int) -> tuple[bool | None, float | None]:
    e = event(t0)
    if not e or not e["markets"][0].get("closed"):
        return None, None
    prices = tuple(json.loads(e["markets"][0]["outcomePrices"]))
    ptb = (e.get("eventMetadata") or {}).get("priceToBeat")
    return {("1", "0"): True, ("0", "1"): False}.get(prices), ptb


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
    min_elapsed = float(sys.argv[4]) if len(sys.argv) > 4 else 10.0

    feed = ChainlinkFeed()
    for _ in range(30):
        if feed.healthy():
            break
        time.sleep(1)
    if not feed.healthy():
        sys.exit("No Chainlink prices from ws-live-data.polymarket.com — is the host reachable?")

    stop_at = time.time() + minutes * 60
    first_window = int(time.time()) // 300 * 300 + 300   # skip the window in progress
    print(f"paper trading until {time.strftime('%H:%M:%S', time.localtime(stop_at))}; "
          f"min_edge={min_edge} stake=${stake} min_elapsed={min_elapsed:.0f}s; "
          f"first window {time.strftime('%H:%M:%S', time.localtime(first_window))}", flush=True)

    pending: list[dict] = []
    gaps: list[tuple[float, float]] = []   # (time, Hyperliquid - Chainlink)
    t0 = s_open = sig = None
    tokens: list[str] = []
    traded = skipped = False
    while time.time() < stop_at or pending:
        now = time.time()
        cur = int(now) // 300 * 300
        try:
            if cur >= first_window and cur != t0 and now < stop_at:
                t0, s_open, traded, skipped = cur, None, False, False
                e = event(t0)
                tokens = json.loads(e["markets"][0]["clobTokenIds"]) if e else []
                sig = feed.sigma_per_sqrt_s() or hl_sigma()
            if t0 and s_open is None and not skipped:
                s_open = feed.at(t0, tolerance=1)
                if s_open is not None:
                    print(f"[{time.strftime('%H:%M:%S')}] window {t0}: Chainlink open {s_open:.2f}, "
                          f"vol {sig * math.sqrt(300) * 100:.3f}%/5m "
                          f"({'chainlink' if feed.sigma_per_sqrt_s() else 'hyperliquid fallback'})", flush=True)
                elif now - t0 > 10:
                    skipped = True
                    print(f"[{time.strftime('%H:%M:%S')}] window {t0}: no Chainlink print at the open, skipped",
                          flush=True)
            last = feed.latest()
            hl = hl_mid()
            if last and feed.healthy():
                gaps.append((now, hl - last[1]))
                gaps = [(t, g) for t, g in gaps if t > now - 300]
            if (t0 and s_open and tokens and not traded and last and feed.healthy()
                    and t0 + min_elapsed <= now < t0 + 300 - STOP_BEFORE_END and now < stop_at):
                ts, s_now = last
                (ask_up, sz_up), (ask_dn, sz_dn) = best_ask(tokens[0]), best_ask(tokens[1])
                fair_cl = core.fair_up(s_open, s_now, t0 + 300 - ts, sig)
                fair_lead = None
                if len(gaps) >= 30 and now - gaps[0][0] >= 240:   # ~5 minutes of samples
                    gap = sorted(g for _, g in gaps)[len(gaps) // 2]
                    fair_lead = core.fair_up(s_open, hl - gap, t0 + 300 - now, sig)
                fair = fair_lead if fair_lead is not None else fair_cl
                d = core.decide(fair, ask_up, ask_dn, min_edge)
                if d.side:
                    size = min(stake / d.price, sz_up if d.side == "Up" else sz_dn)
                    if size >= 5:   # Polymarket's minimum order size
                        traded = True
                        pending.append({"t0": t0, "entered_s": round(now - t0, 1), "side": d.side,
                                        "price": d.price, "shares": round(size, 2), "fair": round(d.fair, 4),
                                        "edge": round(d.edge, 4), "fair_source": "lead" if fair_lead is not None else "chainlink",
                                        "fair_up_chainlink": round(fair_cl, 4),
                                        "fair_up_lead": round(fair_lead, 4) if fair_lead is not None else "",
                                        "cl_open": round(s_open, 2), "cl_entry": round(s_now, 2)})
                        print(f"  PAPER BUY {d.side} {size:.1f} @ {d.price:.3f} (fair {d.fair:.3f}, "
                              f"edge {d.edge:+.3f}) at +{now - t0:.0f}s; BTC {s_now - s_open:+.2f} since open",
                              flush=True)
            for p in list(pending):
                if time.time() > p["t0"] + 300 + 20:
                    up_won, ptb = resolve(p["t0"])
                    if up_won is not None:
                        pnl = core.pnl_per_share(p["side"], p["price"], up_won) * p["shares"]
                        p.update(price_to_beat=ptb,
                                 open_vs_ptb=round(p["cl_open"] - ptb, 2) if ptb else "",
                                 up_won=up_won, pnl_usd=round(pnl, 4))
                        log(p)
                        pending.remove(p)
                        print(f"  RESOLVED {p['t0']}: {'Up' if up_won else 'Down'} won -> {pnl:+.2f} USD "
                              f"(our open {p['cl_open']} vs priceToBeat {ptb})", flush=True)
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
