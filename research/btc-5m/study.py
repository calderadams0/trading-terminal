"""Part 1 — what, if anything, predicts the direction of the next BTC 5-minute candle?

Every feature for candle t is computed from candles <= t-1 only. The target is
close_t >= open_t ("Up"; Polymarket resolves ties to Up). The model is fitted on the
first 2/3 of the history and scored on the last 1/3, which it never saw.

Each result is compared against the break-even it has to clear:
  - Polymarket 5m Up/Down bought at 50c: taker fee 0.07*p*(1-p) per share
    -> need a hit rate above 51.75%.
  - Hyperliquid perp, taker in and out: 2 x 0.045% = 0.09% of notional per trade.

Usage:  python3 research/btc-5m/study.py      (run fetch_data.py first)
Writes: research/btc-5m/out/study.md
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
OUT = HERE / "out"
PM_FEE_RATE = 0.07
HL_TAKER = 0.00045


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(HERE / "data" / name)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("dt").sort_index()


def features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    ret = np.log(df["close"] / df["open"])
    f["ret"] = ret                                   # this candle — the TARGET's source
    f["up"] = (df["close"] >= df["open"]).astype(int)
    p = ret.shift(1)                                 # everything below is lagged
    f["r1"] = p
    f["r2"] = ret.shift(2)
    f["r3"] = ret.shift(3)
    f["r_3"] = ret.rolling(3).sum().shift(1)
    f["r_12"] = ret.rolling(12).sum().shift(1)
    f["r_48"] = ret.rolling(48).sum().shift(1)
    vol = ret.rolling(48).std().shift(1)
    f["vol_48"] = vol
    f["z_r1"] = p / vol
    f["z_12"] = f["r_12"] / (vol * math.sqrt(12))
    # where did the last candle close inside its own range (0 = low, 1 = high)
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    f["clv1"] = ((df["close"] - df["low"]) / rng).shift(1)
    if df["taker_buy_volume"].notna().any():
        imb = 2 * df["taker_buy_volume"] / df["volume"].replace(0, np.nan) - 1
        f["imb1"] = imb.shift(1)
        f["imb_3"] = imb.rolling(3).mean().shift(1)
    f["vol_ratio"] = (df["volume"] / df["volume"].rolling(48).mean()).shift(1)
    # signed run length of same-colour candles ending at t-1
    s = np.sign(ret).replace(0, 1).to_numpy()
    run = np.zeros(len(s))
    for i in range(1, len(s)):
        run[i] = run[i - 1] + s[i] if s[i] == s[i - 1] or run[i - 1] == 0 else s[i]
    f["streak"] = pd.Series(run, index=df.index).shift(1)
    f["hour"] = df.index.hour
    return f.dropna()


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    z, ph = 1.96, k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 25) -> np.ndarray:
    """Newton/IRLS logistic regression with an L2 penalty. X includes a bias column."""
    w = np.zeros(X.shape[1])
    reg = l2 * np.eye(X.shape[1])
    reg[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + reg @ w
        H = (X * (p * (1 - p))[:, None]).T @ X + reg
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def auc(y: np.ndarray, s: np.ndarray) -> float:
    order = np.argsort(s)
    ranks = np.empty(len(s))
    ranks[order] = np.arange(1, len(s) + 1)
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    return (ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def conditional_table(f: pd.DataFrame, col: str, bins: int | list | None = 5) -> str:
    if bins is None:
        g = f.groupby(col)["up"]
    else:
        g = f.groupby(pd.qcut(f[col], bins, duplicates="drop"), observed=True)["up"]
    lines = [f"| {col} bucket | n | P(up) | 95% CI |", "|---|---:|---:|---:|"]
    for k, s in g:
        lo, hi = wilson(int(s.sum()), len(s))
        lines.append(f"| {k} | {len(s)} | {pct(s.mean())} | {pct(lo)} – {pct(hi)} |")
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    raw = load("binance_btcusdt_5m.csv")
    f = features(raw)
    md: list[str] = []
    w = md.append

    n = len(f)
    k = int(f["up"].sum())
    lo, hi = wilson(k, n)
    w("# BTC 5-minute direction study\n")
    w(f"Data: Binance BTCUSDT 5m, {f.index[0]:%Y-%m-%d} → {f.index[-1]:%Y-%m-%d}, {n:,} candles.\n")
    w("## Base rate\n")
    w(f"P(close ≥ open) = **{pct(k / n)}** (95% CI {pct(lo)} – {pct(hi)}).\n")
    w(f"Median |5m move| = {pct(f['ret'].abs().median())}; Hyperliquid taker round trip = {pct(2 * HL_TAKER)}.\n")

    w("## Does the last candle predict the next?\n")
    f["prev_up"] = (f["r1"] >= 0).astype(int)
    w(conditional_table(f, "prev_up", None))
    w("\nLag-1 autocorrelation of 5m returns: "
      f"**{f['ret'].autocorr(1):+.4f}**, lag-2: {f['ret'].autocorr(2):+.4f}, lag-12 {f['ret'].autocorr(12):+.4f}.\n")
    w("### By size of the previous move (z-scored by 4h volatility)\n")
    w(conditional_table(f, "z_r1", 10))
    w("\n### By same-colour streak length\n")
    f["streak_c"] = f["streak"].clip(-5, 5)
    w(conditional_table(f, "streak_c", None))
    w("\n### By where the last candle closed in its range\n")
    w(conditional_table(f, "clv1", 5))
    if "imb1" in f:
        w("\n### By taker-buy imbalance of the last candle (order flow)\n")
        w(conditional_table(f, "imb1", 5))
    w("\n### By hour (UTC)\n")
    w(conditional_table(f, "hour", None))

    # ---- walk-forward model -------------------------------------------------
    cols = [c for c in ["r1", "r2", "r3", "r_3", "r_12", "r_48", "z_r1", "z_12", "clv1",
                        "imb1", "imb_3", "vol_ratio", "streak"] if c in f]
    split = int(n * 2 / 3)
    tr, te = f.iloc[:split], f.iloc[split:]
    mu, sd = tr[cols].mean(), tr[cols].std().replace(0, 1)

    def design(d: pd.DataFrame) -> np.ndarray:
        z = ((d[cols] - mu) / sd).clip(-6, 6).to_numpy()
        return np.hstack([np.ones((len(z), 1)), z])

    wts = fit_logit(design(tr), tr["up"].to_numpy())
    p_te = 1 / (1 + np.exp(-design(te) @ wts))
    y_te = te["up"].to_numpy()
    pred = (p_te >= 0.5).astype(int)
    acc = (pred == y_te).mean()
    lo, hi = wilson(int((pred == y_te).sum()), len(y_te))
    w("\n## Walk-forward logistic model\n")
    w(f"Train {tr.index[0]:%Y-%m-%d} → {tr.index[-1]:%Y-%m-%d} ({len(tr):,}); "
      f"test {te.index[0]:%Y-%m-%d} → {te.index[-1]:%Y-%m-%d} ({len(te):,}, never seen in training).\n")
    w("| feature | weight (std units) |\n|---|---:|")
    for c, v in sorted(zip(cols, wts[1:]), key=lambda t: -abs(t[1])):
        w(f"| {c} | {v:+.4f} |")
    w(f"\nOut-of-sample hit rate: **{pct(acc)}** (95% CI {pct(lo)} – {pct(hi)}); AUC {auc(y_te, p_te):.4f}.\n")

    conf = np.abs(p_te - 0.5)
    w("### Only trading when the model is confident\n")
    w("| keep top | n | hit rate | 95% CI | perp edge/trade after 0.09% fees | beats Polymarket 51.75%? |")
    w("|---|---:|---:|---:|---:|---|")
    ret_te = te["ret"].to_numpy()
    rows = []
    for q in [1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01]:
        thr = np.quantile(conf, 1 - q)
        m = conf >= thr
        hits = int((pred[m] == y_te[m]).sum())
        a = hits / m.sum()
        l, h = wilson(hits, int(m.sum()))
        side = np.where(pred[m] == 1, 1, -1)
        edge = (side * ret_te[m]).mean() - 2 * HL_TAKER
        rows.append((q, a, l, edge))
        w(f"| {int(q * 100)}% | {m.sum():,} | {pct(a)} | {pct(l)} – {pct(h)} | {edge * 1e4:+.2f} bp | "
          f"{'yes' if l > 0.5175 else 'no (CI includes break-even)' if a > 0.5175 else 'no'} |")

    # ---- does the archive agree with the venue? -----------------------------
    try:
        hl = load("hyperliquid_btc_5m.csv")
        both = raw[["open", "close"]].join(hl[["open", "close"]], rsuffix="_hl", how="inner")
        if len(both):
            agree = ((both["close"] >= both["open"]) == (both["close_hl"] >= both["open_hl"])).mean()
            w(f"\n## Binance vs Hyperliquid\n\nOn {len(both):,} overlapping candles the two venues agree on "
              f"direction {pct(agree)} of the time. Findings above transfer to the perp to about that degree.\n")
    except FileNotFoundError:
        pass

    w("\n## Reading this\n")
    best = max(rows, key=lambda r: r[1])
    w(f"- The best out-of-sample bucket hit {pct(best[1])} (lower CI {pct(best[2])}) on the top "
      f"{int(best[0] * 100)}% most-confident candles.")
    w("- A hit rate has to clear **51.75%** at 50c on Polymarket, and the *average* directional "
      "move has to clear **9 bp** on a Hyperliquid perp with taker fills.")
    w("- Anything that does not clear its break-even with its lower confidence bound is noise "
      "until more data says otherwise.")
    report = "\n".join(md) + "\n"
    (OUT / "study.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
