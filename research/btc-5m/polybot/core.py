"""Shared logic for the Polymarket BTC 5-minute paper bot: fair value, fees, decisions.

The market: "Bitcoin Up or Down — <5-minute window>". Up pays $1 if the Chainlink BTC/USD
TWAP over the window's last 60 seconds is >= the price at the window's start.

The edge hypothesis tested here is the one most public bots in this space chase: once a
window is under way, BTC's move since the open and the time left pin down P(Up) fairly
well, and the order book may lag that. We price it as a driftless random walk:

    final  = average price over [T-60s, T]
    P(Up)  = Phi( ln(S_now / S_open) / sd ),
    sd^2   = sigma^2 * ( max(tau - 60, 0) + min(tau, 60)^3 / (3 * 60^2) )

where tau is the seconds left and sigma the per-sqrt-second volatility of log price. The
second term is the variance of a 60-second average of a random walk (60/3 when tau >= 60).

No order placement lives anywhere in this package. Paper only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TWAP_SECONDS = 60
FEE_RATE = 0.07          # feeSchedule.rate on these markets; taker only, exponent 1


def taker_fee_per_share(price: float) -> float:
    """Polymarket crypto fee, in $ per share, for a taker buy at `price`."""
    return FEE_RATE * price * (1 - price)


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fair_up(s_open: float, s_now: float, seconds_left: float, sigma_per_sqrt_s: float) -> float:
    if s_open <= 0 or s_now <= 0:
        return 0.5
    tau = max(seconds_left, 0.0)
    a = min(tau, TWAP_SECONDS)
    var = sigma_per_sqrt_s ** 2 * (max(tau - TWAP_SECONDS, 0.0) + a ** 3 / (3 * TWAP_SECONDS ** 2))
    if var <= 1e-18:
        return 1.0 if s_now >= s_open else 0.0
    return phi(math.log(s_now / s_open) / math.sqrt(var))


@dataclass
class Decision:
    side: str | None         # "Up", "Down" or None
    price: float = 0.0       # what we pay per share
    fair: float = 0.0        # our probability for that side
    edge: float = 0.0        # fair - price - fee, $ per share


def decide(fair_up_p: float, ask_up: float, ask_down: float, min_edge: float) -> Decision:
    """Buy whichever side has expected value above `min_edge` $/share after fees, if any."""
    best = Decision(None)
    for side, fair, ask in (("Up", fair_up_p, ask_up), ("Down", 1 - fair_up_p, ask_down)):
        if not (0.01 <= ask <= 0.99):
            continue
        edge = fair - ask - taker_fee_per_share(ask)
        if edge > min_edge and edge > best.edge:
            best = Decision(side, ask, fair, edge)
    return best


def pnl_per_share(side: str, price: float, up_won: bool) -> float:
    won = (side == "Up") == up_won
    return (1.0 if won else 0.0) - price - taker_fee_per_share(price)
