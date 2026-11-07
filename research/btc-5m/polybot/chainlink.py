"""Live Chainlink BTC/USD — the price Polymarket's 5-minute markets settle on.

Polymarket relays the Chainlink data stream on its public real-time socket
(wss://ws-live-data.polymarket.com, topic crypto_prices_chainlink), one update per
second per symbol. This keeps a rolling
{unix_second: price} history on a background thread and reconnects if it drops.

Why it matters: Chainlink's BTC/USD sits ~$85-110 below Hyperliquid's BTC perp (measured
2026-11-07 over 22 windows). A bot that takes its start price from Hyperliquid thinks
every window opened ~$88 "up" and trades on that phantom move — which is what the first
paper run did.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
import time

URL = "wss://ws-live-data.polymarket.com"
# Unfiltered on purpose: as of 2026-11-07 a {"symbol": "btc/usd"} filter returns a
# ~1 minute history snapshot and then no live updates (even alongside an unfiltered
# subscription). Unfiltered streams every symbol once a second; we keep btc/usd.
SUB = {"action": "subscribe", "subscriptions": [
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}
KEEP_SECONDS = 2 * 3600


class ChainlinkFeed:
    def __init__(self) -> None:
        self.prices: dict[int, float] = {}
        self.last_update = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    # ---- reading -----------------------------------------------------------
    def latest(self) -> tuple[int, float] | None:
        with self._lock:
            if not self.prices:
                return None
            t = max(self.prices)
            return t, self.prices[t]

    def at(self, t: int, tolerance: int = 3) -> float | None:
        """Price stamped exactly t, else the nearest within `tolerance` seconds."""
        with self._lock:
            for d in range(tolerance + 1):
                for s in (t - d, t + d):
                    if s in self.prices:
                        return self.prices[s]
        return None

    def span_seconds(self) -> int:
        with self._lock:
            return max(self.prices) - min(self.prices) if self.prices else 0

    def sigma_per_sqrt_s(self, lookback: int = 3600) -> float | None:
        """Volatility from 1-minute returns of the stream; None until ~50 minutes exist."""
        import math
        with self._lock:
            if not self.prices:
                return None
            end = max(self.prices)
            closes = [self.prices[s] for s in range(end - lookback, end + 1, 60) if s in self.prices]
        if len(closes) < 50:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        mu = sum(rets) / len(rets)
        return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1) / 60)

    def healthy(self, max_age: float = 10) -> bool:
        return time.time() - self.last_update < max_age

    # ---- socket --------------------------------------------------------------
    def _store(self, ts_ms: int, value: float) -> None:
        with self._lock:
            self.prices[int(ts_ms) // 1000] = float(value)
            self.last_update = time.time()
            cutoff = max(self.prices) - KEEP_SECONDS
            if len(self.prices) > KEEP_SECONDS + 600:
                for s in [s for s in self.prices if s < cutoff]:
                    del self.prices[s]

    async def _run(self) -> None:
        import websockets  # pip install websockets (>=15, for proxy support)
        cafile = os.environ.get("SSL_CERT_FILE")
        ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        while True:
            try:
                async with websockets.connect(URL, ssl=ctx, proxy=True, ping_interval=10) as ws:
                    await ws.send(json.dumps(SUB))
                    last_ping = time.time()
                    while True:
                        if time.time() - last_ping > 5:   # the server expects text PINGs
                            await ws.send("PING")
                            last_ping = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 1)
                        except asyncio.TimeoutError:
                            if time.time() - self.last_update > 30:
                                raise ConnectionError("no btc/usd update for 30 s")
                            continue
                        if not raw or raw[0] != "{":
                            continue
                        msg = json.loads(raw)
                        p = msg.get("payload") or {}
                        if "data" in p:            # history snapshot on subscribe
                            for pt in p["data"]:
                                self._store(pt["timestamp"], pt["value"])
                        elif p.get("symbol") == "btc/usd" and "value" in p:
                            self._store(p["timestamp"], p["value"])
            except Exception:
                await asyncio.sleep(2)             # reconnect; history is kept


if __name__ == "__main__":   # quick check: python3 chainlink.py
    feed = ChainlinkFeed()
    for _ in range(10):
        time.sleep(1)
        print(feed.latest(), "history:", feed.span_seconds(), "s")
