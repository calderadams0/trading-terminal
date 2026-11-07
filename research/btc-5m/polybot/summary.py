"""Summarise the paper log written by paper.py.

Usage:  python3 research/btc-5m/polybot/summary.py [path=research/btc-5m/out/paper_trades.csv]

The question it answers: after N windows, is mean P&L per share above zero with its
95% confidence interval clear of zero? Until it is, the answer is "no edge yet".
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "out" / "paper_trades.csv"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        sys.exit(f"no paper log at {path} yet")
    rows = list(csv.DictReader(path.open()))
    if not rows:
        sys.exit("paper log is empty")
    per_share = [float(r["pnl_usd"]) / float(r["shares"]) for r in rows]
    usd = [float(r["pnl_usd"]) for r in rows]
    n = len(rows)
    mean = sum(per_share) / n
    se = math.sqrt(sum((p - mean) ** 2 for p in per_share) / max(n - 1, 1) / n) if n > 1 else float("nan")
    wins = sum(p > 0 for p in per_share)
    print(f"trades: {n}   win rate: {100 * wins / n:.1f}%   total P&L: {sum(usd):+.2f} USD")
    print(f"mean P&L per $1 share: {mean:+.4f}   95% CI: {mean - 1.96 * se:+.4f} to {mean + 1.96 * se:+.4f}")
    lo = mean - 1.96 * se
    print("verdict:", "edge (CI above zero) — keep collecting before believing it" if lo > 0
          else "no edge yet (CI includes or is below zero)")
    by_side: dict[str, list[float]] = {}
    for r, p in zip(rows, per_share):
        by_side.setdefault(r["side"], []).append(p)
    for side, ps in sorted(by_side.items()):
        print(f"  {side:<4} {len(ps):>4} trades, mean {sum(ps) / len(ps):+.4f}")
    gaps = [abs(float(r["open_vs_ptb"])) for r in rows if r.get("open_vs_ptb") not in (None, "")]
    if gaps:
        print(f"start-price check: |our Chainlink open - Polymarket priceToBeat| median "
              f"{sorted(gaps)[len(gaps) // 2]:.2f}, max {max(gaps):.2f} USD")


if __name__ == "__main__":
    main()
