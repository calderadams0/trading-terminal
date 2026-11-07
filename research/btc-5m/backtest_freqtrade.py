"""Backtest strategies/Btc5mReversion.py in real Freqtrade. No orders, no keys.

Freqtrade runs in Hyperliquid futures mode (BTC/USDC:USDC), but candles come from the
12-month Binance archive (fetch_data.py), because Hyperliquid only serves ~17 days of
5m history. Funding is set to zero, which slightly flatters the result. Fees are the
Hyperliquid taker rate, 0.045% per side.

Usage (needs `pip install freqtrade`, e.g. in a venv):
    python research/btc-5m/backtest_freqtrade.py [freqtrade-binary]
Writes the Freqtrade user dir to research/btc-5m/out/ft/ and prints the report for the
training months and the held-out test months separately.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
UD = HERE / "out" / "ft"
PAIR_FILE = "BTC_USDC_USDC"


def prepare() -> tuple[str, str]:
    df = pd.read_csv(HERE / "data" / "binance_btcusdt_5m.csv")
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    ohlcv = df[["date", "open", "high", "low", "close", "volume"]].astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float})
    d = UD / "data" / "hyperliquid" / "futures"
    d.mkdir(parents=True, exist_ok=True)
    ohlcv.reset_index(drop=True).to_feather(d / f"{PAIR_FILE}-5m-futures.feather")
    # Mark candles = trade candles; funding = 0 every hour.
    hourly = ohlcv.set_index("date").resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
    hourly.to_feather(d / f"{PAIR_FILE}-1h-mark.feather")
    hourly.to_feather(d / f"{PAIR_FILE}-1h-futures.feather")  # Hyperliquid marks off trade candles
    fund = hourly[["date"]].copy()
    fund["open"] = fund["high"] = fund["low"] = fund["close"] = 0.0
    fund["volume"] = 0.0
    fund.to_feather(d / f"{PAIR_FILE}-1h-funding_rate.feather")

    (UD / "strategies").mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "strategies" / "Btc5mReversion.py", UD / "strategies")
    cfg = {
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "max_open_trades": 1,
        "stake_currency": "USDC",
        "stake_amount": 1000,
        "dry_run": True,
        "dry_run_wallet": 10000,
        "timeframe": "5m",
        "fee": 0.00045,
        "exchange": {
            "name": "hyperliquid",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pair_blacklist": [],
            # Only market metadata is fetched; honour HTTPS_PROXY if the host sets one.
            "ccxt_async_config": {"aiohttp_trust_env": True},
        },
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {"price_side": "other", "use_order_book": False},
        "exit_pricing": {"price_side": "other", "use_order_book": False},
        "dataformat_ohlcv": "feather",
        "user_data_dir": str(UD),
        "datadir": str(UD / "data" / "hyperliquid"),
    }
    (UD / "config.json").write_text(json.dumps(cfg, indent=2))
    n = len(ohlcv)
    split = ohlcv["date"].iloc[int(n * 2 / 3)]
    start = ohlcv["date"].iloc[300]
    end = ohlcv["date"].iloc[-1]
    fmt = "%Y%m%d"
    return f"{start:{fmt}}-{split:{fmt}}", f"{split:{fmt}}-{end:{fmt}}"


def run(ft: str, timerange: str, label: str) -> None:
    print(f"\n===== {label}: {timerange} =====", flush=True)
    cmd = [ft, "backtesting", "-c", str(UD / "config.json"), "--userdir", str(UD),
           "-s", "Btc5mReversion", "--timerange", timerange, "--export", "none",
           "--cache", "none", "--breakdown", "month"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    text = out.stdout + out.stderr
    (UD / f"report-{label}.txt").write_text(text)
    keep = False
    for line in text.splitlines():
        if "BACKTESTING REPORT" in line or "SUMMARY METRICS" in line or "EXIT REASON" in line:
            keep = True
        if keep:
            print(line)
    if out.returncode != 0:
        print(text[-3000:])


if __name__ == "__main__":
    ft = sys.argv[1] if len(sys.argv) > 1 else "freqtrade"
    train, test = prepare()
    run(ft, train, "train")
    run(ft, test, "test")
