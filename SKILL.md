---
name: perpetual-arbitrage
description: Perpetual futures arbitrage on Hyperliquid (and other venues): funding-rate harvest, spot-perp basis / cash-and-carry, cross-exchange funding spread, and funding-squeeze setups. Includes a runnable backtest (scripts/backtest_funding.py) and a parameter sweep (scripts/sweep.py) that use only Hyperliquid's public API — no keys, no live trading. Use when Jesse asks about perp arbitrage, funding-rate trading, basis trades, or wants a backtest run.
---

# Perpetual Arbitrage

Version 0.2 ("gacor" — validated setup), 2026-11-16. Created at Jesse's
request. Built from Superior Trade's `funding-rate-arbitrage`, `basis-arb`,
and `funding-squeeze` skills (github.com/Superior-Trade/superior-skills),
then tuned and validated with independent backtests on live Hyperliquid
public data (see `results/`).

Venue focus: **Hyperliquid** (Jesse's venue — he trades HYPE perps there).
Everything below is paper/backtest unless Jesse explicitly approves live
trading per action. Live trading needs a funded Hyperliquid API wallet —
Jesse has none — and his standing rule: no live trade without explicit
per-action approval.

## The validated setup (Strategy A — funding harvest, long-only)

Long a perp when funding is deeply negative (shorts pay longs). Funding on
Hyperliquid is paid **hourly**; APR = hourly rate x 24 x 365.

| Rule | Value |
|---|---|
| Long entry | funding APR < **-10%** |
| Long exit | funding APR >= 0 (carry gone) |
| Stop-loss | **-5%** on price |
| Max hold | **24h** timeout (never binds in practice — funding flips exit first) |
| Profit target | none — let the trade work |
| Size | $100 notional, 1x per coin (scale only after validation) |
| Fees assumed | 0.035% taker per side |
| Coins | BTC, ETH, HYPE (HYPE is the workhorse — most setups, most profit) |

### Validation — 208 days (2026-04-22 -> 2026-11-16), $100 notional, 1x

`results/backtest-2026-11-16-208d.json` (script:
`scripts/backtest_funding.py --days 208 --coins BTC ETH HYPE`).

| Coin | Trades | Win | PnL | Return | Max DD | Buy & hold |
|---|---|---|---|---|---|---|
| BTC | 31 | 51.6% | +$2.69 | +2.69% | 2.61% | -17.6% |
| ETH | 36 | 55.6% | +$11.97 | +11.97% | 7.61% | -24.8% |
| HYPE | 99 | 57.6% | +$28.86 | +28.86% | 4.19% | +63.6% |
| **Combined** | **166** | — | **+$43.52** | **+14.51% on $300** | — | — |

The strategy beat buy-and-hold on BTC and ETH (positive while buy-and-hold
lost 18–25%) but **lagged HYPE's +63.6% rally** — it is out of the market
most of the time, so it wins in chop/downtrends and lags strong uptrends.

### What the sweep proved (and what NOT to do)

`scripts/sweep.py` tested 48 parameter combos on window A (120d,
2026-07-19 -> 2026-11-16) and validated the top 3 on window B (88d,
2026-04-22 -> 2026-07-19). Results in `results/sweep-2026-11-16.json`.

- **Stricter entry (-20%) looks better on A (+$8.81 vs +$8.29) but loses
  out-of-sample (B: +$24.61 vs +$35.23) and combined ($33.42 vs $43.52).**
  It is overfitting to window A — do not use it. The -10% default is the
  robust choice.
- **The short side hurts.** Every "both" combo underperformed its
  long-only twin (e.g. -10% both +$5.91 vs long +$8.29 on A). Shorting
  into +30% funding gets squeezed. Long-only is the default.
- **-30% entry is worse** (best +$4.92 on A) and **late exits (-5%) hurt**
  (worst combo -$7.95 on A).
- **Timeout 24h vs 48h is identical** — exits are always funding flips.

### Honest finding — the money is NOT from funding

Across all 166 trades, funding income was only **$0.10–$0.25 per coin**.
The PnL came from price moves. Extreme funding is a 1-hour spike that
normalizes right after entry, so at 1x the strategy is really a
**positioning / mean-reversion signal** (buy the dip when shorts are
paying hard, exit when funding normalizes), not a carry harvest. Real
funding harvest needs leverage or longer holds — both add risk. Do not
sell this as "passive funding income".

## Other strategies (not the default)

### B. Basis / cash-and-carry (two-leg, NOT single-leg)

True basis arb is hedged: long perp + short spot when perp trades at a
discount to spot (basis < 0), or short perp + long spot when perp trades
at a premium (basis > 0). You earn the basis as the legs converge, plus
funding on the perp leg. Basis = (perp_mark - spot_mid) / spot_mid.

Honest limits: Freqtrade is a single-leg engine — it cannot run paired
hedged trades. Superior Trade's `basis-arb` template is a **directional
read**, not a hedge, and its own backtest produced **0 trades** (no spot
leg in the engine). Hyperliquid spot books exist only for a few coins
(BTC, ETH, SOL…). The hedged version needs a multi-leg runtime and spot
borrow/short access. Do not run it without one.

### C. Cross-exchange funding spread

Same asset, different funding on two venues (e.g. HL vs Binance): long
where funding is negative, short where positive — collect both sides in
theory. In practice: funded accounts on both venues, margin on both
sides, transfer latency, spread closes fast. Not backtested here — screen
live funding on both venues before building anything.

### D. Funding squeeze (variant of A — higher risk)

Long when funding is deeply negative **and price is already rising**
(shorts getting squeezed). Exit on funding normalisation or time stop.
Superior Trade's own backtest (BTC, Jan–May 2026): 8 trades, 38% win,
**-0.75%**, Sharpe -0.75 — negative in a downtrend. Suggested
refinements: higher-timeframe regime filter (e.g. only enter when 1d
close > 1d EMA-50) and a multi-pair scan. Experimental, never the default.

## Data sources (public, no key)

Hyperliquid Info API `POST https://api.hyperliquid.xyz/info`:

| Data | Request | Notes |
|---|---|---|
| Live funding (all perps) | `{"type": "metaAndAssetCtxs"}` | `funding` per asset in the `universe` |
| Funding history | `{"type": "fundingHistory", "coin": "BTC", "startTime": ms, "endTime": ms}` | 500 rows/page — paginate with `startTime` |
| Candles 1h | `{"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": ms, "endTime": ms}}` | max ~5,000 candles (~208 days at 1h); NOT the old `candle` type (422s) |

Rate limit: 1,200 weight/minute per IP. Funding row `time` is the payment
time at the END of its hour; a row with time in (candle_open,
candle_close] is the payment for holding that hour.

## How to run the backtest

```bash
python3 scripts/backtest_funding.py --days 208 --coins BTC ETH HYPE --out results/backtest-YYYY-MM-DD.json
python3 scripts/sweep.py --out results/sweep-YYYY-MM-DD.json   # 48-combo grid + out-of-sample validation
```

- Stdlib only, no keys, no trading. Entry/exit evaluated at hourly closes
  (no lookahead: the decision uses the funding rate of the hour just
  closed, fill at that close).
- `backtest_funding.py` accepts `--entry-apr`, `--exit-apr`, `--stop`,
  `--timeout`, `--side`, `--end YYYY-MM-DD` to test other setups — but
  changing the defaults needs a fresh sweep + out-of-sample check first.

## Rules before any live talk

1. Backtest first, on a fresh window, before quoting any return.
2. An hourly funding APR is an annualisation of a 1-hour spike — a
   -4,000% APR does NOT mean that return for a year. Extreme funding
   usually means an illiquid/new listing whose price swings dwarf the
   funding income; the -5% stop can trigger before funding is collected.
3. Funding flips hourly. A setup that is valid now can be gone next hour.
4. Never present a backtest as a promise. Always report: window, trades,
   win rate, return, max DD, and the buy-and-hold comparison.
5. Live deployment needs Jesse's explicit per-action approval, a funded
   API wallet (he has none), and a dry-run/paper phase first.

## Sources

- Superior Trade skills: `funding-rate-arbitrage`, `basis-arb`,
  `funding-squeeze` (github.com/Superior-Trade/superior-skills)
- Hyperliquid docs: funding mechanics, Info API
  (hyperliquid.gitbook.io/hyperliquid-docs)
