# Part 2 — BTC 5m mean-reversion, the best candidate study.py's training months produced.
#
# Fade a 15-minute move larger than 2.5 sigma (sigma = 24h rolling 5m volatility) and
# hold for 3 candles. Long after a flush down, short after a spike up. 1x leverage.
#
# Research artifact, NOT a recommendation: on the held-out months its gross edge is
# ~0 bp per trade against ~9 bp of Hyperliquid taker fees. See research/btc-5m/README.md
# for the backtest. It passes lib/freqtrade-guard.ts so it can be studied in the same
# pipeline the app uses — that says it is safe to run, not that it makes money.

from datetime import datetime

import numpy as np
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class Btc5mReversion(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True
    startup_candle_count = 300

    # 1x: a 1% price stop is a 1% position stop.
    stoploss = -0.01
    minimal_roi = {"0": 0.01}
    trailing_stop = False

    lookback = 3       # candles in the move being faded (15 minutes)
    z_entry = 2.5      # how many sigmas that move must be
    hold_candles = 3   # exit after this many candles (15 minutes)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        logret = np.log(dataframe["close"]).diff()
        sigma = logret.rolling(288).std()
        move = np.log(dataframe["close"]) - np.log(dataframe["close"].shift(self.lookback))
        dataframe["move_z"] = move / (sigma * np.sqrt(self.lookback))
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["move_z"] < -self.z_entry) & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["move_z"] > self.z_entry) & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exits are time-based (custom_exit) plus the ROI/stop above.
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        if (current_time - trade.open_date_utc).total_seconds() >= self.hold_candles * 300:
            return "time_exit"
        return None
