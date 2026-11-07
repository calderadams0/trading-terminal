# BTC 5-minute research

Can anything here make money on BTC's 5-minute horizon — on a Hyperliquid perp, or on
Polymarket's "Bitcoin Up or Down" 5-minute markets?

**Short answer, as of 2026-11-07: not with anything tested here, and not with any public
bot whose claims we could check.** Every apparent edge either lost to fees or turned out
to be a measurement artifact. The tools are kept so the question can be re-asked
honestly as markets change.

Nothing in this folder places orders, holds keys, or can be pointed at an account.

```
fetch_data.py               12 months of Binance 5m + recent Hyperliquid 5m candles
study.py                    Part 1 — what predicts the next 5m candle?
strategies/Btc5mReversion.py Part 2 — the best candidate, as a Freqtrade strategy
strategy.test.ts            …which must pass lib/freqtrade-guard.ts (it does)
backtest_freqtrade.py       …backtested in real Freqtrade, train and held-out months
polybot/core.py             Part 3 — fair value, Polymarket fee, decision rule (shared)
polybot/backtest.py         …on CLOB price history  ← flattering; kept as a cautionary tale
polybot/tape_backtest.py    …on the real trade tape ← the honest one
polybot/chainlink.py        …live Chainlink BTC/USD, the settlement price
polybot/paper.py            …live paper trading against the real order book
polybot/summary.py          …is the paper log's P&L clear of zero yet?
```

Data and reports land in `data/` and `out/` (git-ignored).

## Part 1 — what predicts the next 5-minute candle?

`python3 research/btc-5m/fetch_data.py && python3 research/btc-5m/study.py`

105,072 Binance BTCUSDT 5m candles, 2025-11-01 → 2026-10-31. Every feature is lagged.

- Base rate: Up 50.05% (CI 49.75–50.35%). A coin flip.
- There **is** a small, consistent **mean-reversion** effect: after a strong up candle, a
  close near the high, a run of greens, or heavy taker buying, the next candle is slightly
  more likely red (and vice versa). Extreme buckets reach 47–53%.
- A logistic model trained on the first 8 months scores **51.4%** on the 4 months it never
  saw (CI 50.9–51.9%); on its top-10% most confident calls, **54.0%** (CI 52.3–55.6%).
- But the average 5m move is ~6 bp and a Hyperliquid taker round trip is 9 bp. Every
  confidence bucket nets **≈ −9 bp per trade** on the perp. The direction is predictable
  by a hair; the *size* of the move it predicts is too small to pay the fee.
- 54% would clear Polymarket's 51.75% break-even *if* you could buy at 50c at the open.
  You can't, reliably: the book opens near 50/50 with a spread, and seven thresholds were
  tested, so the best one is flattered by selection.

## Part 2 — a Freqtrade strategy for the app's pipeline

Parameters were chosen on the training months only (fade a 15-minute move beyond 2.5σ,
hold 15 minutes). Across all 36 parameter combinations tried, gross edge was ≤ 1 bp per
trade against 9 bp of fees; most that were positive in training were negative in test.

`strategies/Btc5mReversion.py` passes the app's validator (`strategy.test.ts`). Real
Freqtrade 2026.10, Hyperliquid futures mode, 0.045% taker fee, 1x, zero funding
(`backtest_freqtrade.py`):

| period | trades | win rate | result |
|---|---:|---:|---:|
| train 2025-11 → 2026-07 | 1,250 | 42.6% | **−9.0%** |
| test 2026-07 → 2026-10 | 605 | 35.2% | **−5.2%** |

Do not deploy it. It exists to show the pipeline end to end and to be beaten.

## Part 3 — Polymarket "Bitcoin Up or Down" 5-minute markets

How they work (read from the Gamma API, Nov 2026): Up pays $1 if the **Chainlink BTC/USD
60-second TWAP** at the end of the window is ≥ the price at the start. Fees are taker-only,
`0.07 × p × (1−p)` per share — 1.75c at 50c, i.e. 3.5% of notional. Minimum order 5 shares.

The idea most public bots chase: once a window is under way, BTC's move so far and the
time left imply P(Up); if the order book lags, buy the cheap side. `core.fair_up()` prices
that as a random walk, accounting for the 60-second TWAP.

Three backtests on resolved windows from 2026-11-04 → 11-07, each stricter (v2 and v3 use
the same 850 windows; v1, an earlier draft priced off Hyperliquid 1m candles, used 1,033):

| test | what the market price was | mean P&L per $1 share | verdict |
|---|---|---:|---|
| v1 (early draft), minute boundaries | CLOB `prices-history`, ~1/min | +0.12 | artifact |
| v2 `backtest.py`, at each history point, BTC 1 s before | same | +0.07 to +0.11 | artifact |
| v3 `tape_backtest.py`, at each real trade, fill on the next print ≥ 1 s later | 1.4 M executed trades | **+0.003 (CI −0.029 to +0.036)** | **no edge** |
| v3 + Chainlink start price and lead-adjusted "now" (current `tape_backtest.py`) | same | **+0.013 (CI −0.019 to +0.046)** | **no edge** |

Why v1 and v2 lie: a `prices-history` point is not the market *at* its timestamp. Sampling
the live book every 5 s and comparing, the history lagged it by **5–115 seconds** (one
point read 0.615 while the book was at 0.305). Any model fed a fresh BTC price "beats" a
price that old. On the real tape, the market's last trade is a *better* forecast than the
model (Brier 0.151 vs 0.174), and the model's apparent 9c edge shrinks to zero once you
must fill at the next real print.

Most v3 entries came in the first seconds of a window. Forbidding entries before 30 / 60 /
120 s (`tape_backtest.py 0.05 1 <seconds>`) gave −0.003 / +0.009 / −0.015 $/share, all
with CIs spanning zero.

**Pricing off Chainlink.** These markets settle on Chainlink BTC/USD, which on
2026-11-07 sat ~$103 below Binance and ~$88 below Hyperliquid, drifting ~$21 between
windows. A *constant* gap cancels out of the model (it uses the ratio now/open). What
does not cancel is that Chainlink **trails** the exchanges by seconds, so the
exchange price now is a forecast of where Chainlink is about to print. `tape_backtest.py`
now uses Polymarket's published `priceToBeat` as the start price and the exchange price
minus the median gap of the previous 30 minutes as "now". That makes the model
noticeably better calibrated (Brier 0.174 → **0.159**) — but still worse than the
market's own last trade (0.151), and the P&L is still indistinguishable from zero:
**+0.013 $/share (CI −0.019 to +0.046)** with no entry delay, −0.009 (CI −0.040 to
+0.022) with 30 s. The market already knows what the model knows, and a bit more.

The same trap is documented independently in `emoubarak/polymarket-updown-lab`
(`research/FINDINGS.md`: "prices-history is median 56 s stale"; "directional edge =
temporal lookahead artifact").

`polybot/paper.py` runs the rule live against the **real best ask**, which cannot be
stale, and logs to `out/paper_trades.csv`. It prices off **live Chainlink BTC/USD**
(`polybot/chainlink.py`, Polymarket's public relay of the settlement feed), takes the
window's start price from it, and decides on the lead-adjusted price once it has five
minutes of Hyperliquid-minus-Chainlink gap samples. Each resolved row also records
Polymarket's published `priceToBeat`, so the start price is checked, not assumed.

That forward log — hundreds of windows, not a handful — is the only number worth
trusting before any real money. From a cloud box the bot is also seconds slower than
co-located competitors, which costs more edge, not less.

### Running it for weeks

A cloud session's container is temporary, so run it on a machine that stays on (your
own computer or a small VPS). It needs outbound HTTPS/WSS to `gamma-api.polymarket.com`,
`clob.polymarket.com`, `ws-live-data.polymarket.com` and `api.hyperliquid.xyz`.

```bash
pip install -r research/btc-5m/requirements.txt
# 14 days, $10 paper stake, 5c min edge, no entries in a window's first 10 s
nohup python3 research/btc-5m/polybot/paper.py 20160 0.05 10 10 >> paper.log 2>&1 &
python3 research/btc-5m/polybot/summary.py      # any time: P&L, 95% CI, verdict
```

The log appends across restarts. ~288 windows a day; a few hundred trades is the least
that can tell a 2c edge from zero.

## Public "polybots" on GitHub (checked 2026-11-07)

Searched GitHub for Polymarket BTC 5m bots; cloned 13 and read them (never ran them). A
repo's own profit claim is not evidence; we looked for a public wallet, a real trade log,
or out-of-sample numbers.

| repo | ★ | what it is | evidence of profit | flags |
|---|---:|---|---|---|
| emoubarak/polymarket-updown-lab | 0 | 6-week honest research log, paper + small real money | **5m: −$520** over 39 paper runners; 15m: +$55; only "edge" left is maker liquidity rewards | Best thing to read |
| npcnpc09/polymarket-updown-bot | 0 | Retired real-money bot, late-window favourites | Real wallet log: $57 → $443 peak → $323, Jun–Aug 2026 | Made money **when fees were 0%**; per-trade P&L went +$0.41 → −$0.55 as the edge was competed away |
| 0xbrutusdev/polymarket-5min-crypto-trading-bot | 70 | Maker bot reverse-engineered from public wallet `spork30` | Points at a real profitable wallet, not its own | Market-making (rebates + spread), not prediction; committed `.pyc` files |
| KaustubhPatange/polymarket-trade-engine | 323 | TypeScript engine + strategy harness + dashboard | None claimed | Clean; a framework, not an edge |
| jinwovo/polymarket-btc-updown-jury-bot | 2 | "Binance leads Polymarket" latency arb | Claims 65–75% win rate, PF 2–3; no wallet | The exact edge the tape test above finds absent at retail latency |
| sebbourgeois/polybtcbot | 7 | Same latency-arb idea, paper by default | Cites another trader's wallet ("stargate5"), not its own | Honest disclaimer |
| ElijahPrince73/polymarket-btc-5m-assistant | 9 | Rust paper-trading assistant | None | Paper only |
| AllAboutAI-YT/polymarket_bot_beginner | 13 | Beginner late-favourite bot | README: "not consistently profitable" | Honest |
| dearolaf/Polymarket-Crypto-5Minutes-Trading-Bot | 29 | Freemium; "Premium" sold off-site | None | **Martingale** staking; committed `.env`; Telegram funnel. Avoid |
| radioman/polymarket-arbitrage-trading-bot | 637 | C++ repo created **2016**, 737 MB, renamed | None | Repurposed old repo with recycled stars. Avoid |

No obfuscated payloads were found in the code that was read, but key-handling code in
this niche is a known malware vector: never put a funded wallet's key into code you have
not read line by line, and use a fresh wallet with a small balance if you ever do.

**Conclusion:** no public repo shows verifiable, *current* profit from predicting BTC's
5-minute direction. The money that is verifiably made in these markets comes from market
making (spread + Polymarket's liquidity rewards, with co-located, low-latency
infrastructure) or came from a 0%-fee period that has ended.

## Before risking money

- Polymarket blocks some jurisdictions (these markets are flagged `restricted`); check
  yours.
- Run `polybot/paper.py` for weeks. If its log is not clearly positive after a few hundred
  windows, the answer is no.
- Re-run `tape_backtest.py` monthly; fees, competition and resolution rules change.
