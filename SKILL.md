---
name: perpetual-arbitrage
description: Perpetual futures arbitrage on Hyperliquid (and other venues): funding-rate harvest, spot-perp basis / cash-and-carry, cross-exchange funding spread, and funding-squeeze setups. v0.3 adds a staged evaluation workflow (Scan -> Verify -> Score -> Debate -> Decide -> Report), a multi-agent evaluation pipeline adapted from Tauric's TradingAgents, a 0-10 signal scorer (scripts/score.py), a live scanner (scripts/scan.py), and a cross-venue funding-spread matcher (scripts/cross_venue.py). Includes runnable backtests (scripts/backtest_funding.py, scripts/sweep.py) on Hyperliquid's public API only — no keys, no live trading. Use when Jesse asks about perp arbitrage, funding-rate trading, basis trades, or wants a backtest or a live scan run.
---

# Perpetual Arbitrage

Version 0.3, 2026-11-16. Created at Jesse's request. Paper/backtest only.

## What changed in v0.3

v0.2 was a validated entry rule plus a backtest. v0.3 adds the
**evaluation discipline around the rule**, borrowed from three
reference repos (see Sources):

1. **Staged workflow** (from the Event-Driven Arbitrage Scanner):
   Scan -> Verify -> Score -> Debate -> Decide -> Report, with a
   structured report (one JSON + one short markdown summary per scan)
   saved in `results/`.
2. **Multi-agent evaluation pipeline** (adapted from Tauric's
   TradingAgents): parallel specialist analysts -> Bull/Bear debate
   -> Risk Manager verdict (GO / NO-GO / REDUCE) -> decision log.
   This is a *procedure the agent follows*, not code.
3. **0-10 signal scorer** (`scripts/score.py`): six weighted
   dimensions, no lookahead, with a liquidity gate that caps
   illiquid-trap signals at 4.0.
4. **Live scanner** (`scripts/scan.py`): scores every Hyperliquid
   perp below the -10% entry threshold each run, ranked table, JSON +
   markdown report in `results/`.
5. **Cross-venue matcher** (`scripts/cross_venue.py`): CloddsBot-style
   symbol matching across Hyperliquid vs OKX (Binance/Bybit
   best-effort), funding spread in percentage points, > 10pp flagged.
   Detection only.
6. **Scored backtest** (`--min-score` on `scripts/backtest_funding.py`):
   the honest result is that the score gate does **not** improve total
   PnL — see §7. The unscored v0.2 setup stays the validated default.
   The scorer's value is as a ranking/evaluation tool, not as a
   validated entry filter.

**What did NOT change:** the v0.2 validated defaults (entry APR <
-10%, exit APR >= 0, stop -5%, 24h timeout, $100 notional 1x,
long-only, BTC/ETH/HYPE). Do not change them without a fresh sweep +
out-of-sample check.

---

## 1. The staged workflow

Every signal evaluation follows these six stages, in order. Skip a
stage only if it is marked optional.

| Stage | What happens | Output |
|---|---|---|
| **1. Scan** | `scripts/scan.py` fetches all 235 Hyperliquid perps, scores every coin below -10% APR, prints a ranked table, saves JSON + markdown in `results/`. | Ranked candidate list with scores |
| **2. Verify** | For each PASS candidate, verify the raw numbers independently: re-read the current funding rate and mark price from `metaAndAssetCtxs`; check the coin actually trades (volume/OI from the same response); check the funding history for the last 24h (is this a 1-hour spike or a sustained print?). A candidate whose numbers do not reproduce is dropped here. | Verified facts per candidate |
| **3. Score** | `scripts/score.py` 0-10 score (see §3). A signal below 6 is WATCH/SKIP and does not go to debate. | Score + dimension breakdown |
| **4. Debate** | The multi-agent pipeline (§2): three analysts in parallel, then Bull vs Bear, then the Risk Manager verdict. | GO / NO-GO / REDUCE + reasons |
| **5. Decide** | The decision is the Risk Manager's verdict, sized by the score and the debate: GO = take the paper position at the validated size; REDUCE = take it at half size (or watch one more hour); NO-GO = skip and log why. No decision without a logged verdict. | Position (or explicit skip) |
| **6. Report** | Append the decision to the scan's markdown summary and to the decision log (§2). One JSON + one short markdown summary per scan, in `results/`. | `results/scan-YYYY-MM-DD-HHMM.{json,md}` + decision log |

Analytical mode (borrowed from Trading-R1's terminal modes, as an
idea only — Trading-R1 is **not** installed and its API is **not**
used): when evaluating, numbers first — data and valuation tools,
with narrative/context second. A debate argument without a number
attached to it is a weak argument.

---

## 2. The multi-agent evaluation pipeline (procedure, not code)

Adapted from Tauric's TradingAgents structure — Analyst Team
(parallel specialists) -> Bull/Bear Researcher debate -> Trader
decision -> Risk Management team -> Fund Manager approval — with the
decision log and point-in-time integrity of its backtest. This is a
procedure the agent follows by role-playing each seat in turn. It is
not a program and it does not execute trades.

### The seats

**Analysts — run in parallel, each with its own inputs. No analyst
sees another analyst's conclusion before writing its own.**

| Analyst | Inputs | Must answer |
|---|---|---|
| **Funding Analyst** | Current APR, 24h funding history, persistence fraction | Is the funding extreme *and* persistent, or a 1-hour spike? What does the funding curve look like over 24h? |
| **Market Analyst** | Hourly candles (48h), 24h SMA, 24h range, mark vs oracle price | Is the price recovering above its 24h average, or still falling into the dip? How wide is the 24h range vs the -5% stop? |
| **Liquidity Analyst** | 24h notional volume, open interest (USD), spread proxy (mark vs mid) | Can a $100 position enter and exit without moving the book? Is this one of the known thin books (SKR, MELANIA, CC, BIGTIME, NIL, INIT, SOPH, PONS, USELESS)? |

**Researchers — Bull vs Bear debate, one round each, then a verdict.**

- **Bull** argues the trade: funding pays, price is recovering,
  spread vs other venues confirms venue-specific pessimism.
- **Bear** argues against: funding is a 1-hour spike, price still
  falling, book too thin, funding flips hourly so the edge may be
  gone by the next hour.
- Each side must cite at least one number from the analysts' output.
  An argument without a number does not count.

**Trader (decision).** The trader commits to a side, sized by how
decisively it wins — exactly like TradingAgents' Research Manager
rating scale. Commit; do not manufacture a direction to appear
decisive. Choose NO-GO only when the evidence is still balanced
after weighing, or too thin to support a call.

**Risk Manager (verdict).** The final word, one of three:

| Verdict | Meaning |
|---|---|
| **GO** | Take the paper position at the validated size ($100, 1x). |
| **REDUCE** | Take it at half size ($50) or watch one more hour first. Use when the score passes but one dimension is weak (e.g. liquidity 1–3, or volatility near the stop). |
| **NO-GO** | Skip. Log the reason. A NO-GO is a decision, not a failure to decide. |

**Fund Manager approval.** For paper trading, the "fund manager" is
Jesse's standing rule: paper only, no live trade without his explicit
per-action approval. A GO verdict authorizes a *paper* position only.
It never authorizes a live order.

### Decision log

Every evaluated signal gets one log line, appended to the scan's
markdown report and to `results/decision-log.md`:

```
YYYY-MM-DD HH:MM | COIN | APR -18.3% | score 6.85 (E3.3 P9.2 T7.1 L8.7 V10.0 X6.5)
| Bull: <one line> | Bear: <one line> | verdict GO | size $100 | outcome: <filled later>
```

The outcome (exit reason, PnL) is filled in when the position closes.
A decision with no logged verdict is not a decision — it did not
happen.

### Point-in-time integrity

Like TradingAgents' backtest (its grid stops at the present because a
future date has no outcome to settle against), every number used in
an evaluation must be computable from data available at that moment.
No future data, no lookahead, no "this would have scored well
yesterday". The backtest enforces this by construction (§7); live
evaluations enforce it by only reading live/history endpoints, never
by peeking at a later print.

---

## 3. The 0-10 scoring rubric

`scripts/score.py` — pure functions, stdlib only. Six dimensions,
weighted. Each dimension scores 0-10 with explicit thresholds.

| Dimension | Weight | What it measures | Key thresholds |
|---|---|---|---|
| **Funding extremity** | 30% | How deeply negative the APR is now | -10% -> 0; -20% -> 4; -50% -> 8; -100% -> 10 (saturates — beyond -100% is usually an illiquid artefact) |
| **Persistence** | 20% | Fraction of the last 24h with APR < -10% | 0% of hours -> 0; 100% -> 10, linear. A 1-hour spike scores ~0.4. |
| **Trend alignment** | 15% | Price vs its 24h SMA | -5% below SMA -> 2; at SMA -> 5; +2% above -> 8; +5% -> 10 |
| **Liquidity** | 15% | 24h notional volume AND open interest (USD) | <$1M -> 0; $10M -> 3; $50M -> 6; $200M -> 8; $1B+ -> 10. Score = MIN(volume, OI) — a coin needs both. |
| **Volatility** | 10% | 24h high-low range as % of price. LOW vol scores HIGH. | <3% -> 10; 5% -> 7; 8% -> 4; 12% -> 2; >15% -> 0 |
| **Cross-venue spread** | 10% | HL APR vs another venue (OKX), in pp | 0pp -> 0; 5pp -> 3; 10pp -> 6; 20pp -> 8; 30pp -> 10 |

**Rules:**

- **Missing dimensions are dropped and the remaining weights
  renormalize.** A score is always a weighted average over the
  dimensions actually available. The result lists which dimensions
  were used. In the live scan all 6 are available when OKX lists the
  coin; in the backtest only 4 are (see §7).
- **Liquidity gate:** if the liquidity dimension is present and
  scores 0 (volume or OI effectively zero), the total is capped at
  4.0 — below the default minimum of 6. No signal on an untradeable
  book can pass on extremity alone.
- **Verdicts:** score >= 6 = PASS (worth full evaluation); 4–<6 =
  WATCH (monitor only); < 4 = SKIP.
- **Why these weights:** extremity is the entry condition itself
  (30%); persistence is the v0.2 lesson that extreme funding is
  usually a 1-hour spike (20%); trend + volatility encode the second
  v0.2 lesson that PnL comes from price moves (15% + 10%);
  liquidity is the trap filter (15%); cross-venue is a supporting
  signal, not the trade itself (10%).
- **Threshold 6 is a starting guess, not a validated value.** See
  §7 for what the backtest actually says about it.

---

## 4. Cross-venue matching rules (CloddsBot-style)

`scripts/cross_venue.py` — detection only, no execution.

Borrowed from CloddsBot's `src/arbitrage/index.ts`: match the same
asset across venues, compare the numbers, flag the spread. CloddsBot
matches prediction markets by question similarity (Jaccard >= 0.8)
and computes spread / spreadPct / profitPer100 with a 2% minimum
spread. Here the "question" is a coin symbol, so matching is exact
after normalization, and the number compared is the funding APR:

| Rule | Value |
|---|---|
| **Match** | Normalize symbols first: `BTC-USDT-SWAP` (OKX), `BTCUSDT` (Binance/Bybit), `BTC` (HL) all -> `BTC`. Exact normalized match = confidence 1.0. A coin listed on only one venue has no match and is skipped — never guessed. |
| **Spread** | `other_venue_apr - hl_apr`, in percentage points. |
| **Candidate** | \|spread\| > **10pp**. Direction for a spread pair: long the venue where funding is more negative (detection only — no position is opened by this script). |
| **Scope** | Only HL coins with APR < -10% or > +30%, plus BTC/ETH/HYPE/SOL. The +10.95% APR many HL perps sit at is the funding floor (0.00125%/h), not a signal. |
| **Venues** | Hyperliquid (always works), OKX (public API, works). Binance (`/fapi/v1/premiumIndex`) returns HTTP 451 and Bybit (`/v5/market/tickers`) returns 403 from this VM — both are attempted best-effort and skipped gracefully on geo-block. A blocked venue never crashes the run and never produces a fake "no spread" row. |

Latest run (2026-11-16 ~10:06 UTC, `results/cross-venue-2026-11-16-1006.json`):
34 coins checked, 26 OKX matches, **18 candidates**. Top spreads:
CASHCAT +146.6pp (HL -149.5% vs OKX -2.9%), RESOLV -91.8pp,
TRUMP +75.2pp, W +71.0pp, TURBO +57.2pp. Note: the biggest spreads
are all on small/illiquid coins — a spread is a *detection*, not a
trade. Every candidate still has to pass the liquidity gate (§3) and
the full pipeline (§2) before any position, paper or otherwise. (An
earlier run the same morning,
`results/cross-venue-2026-11-16-1004.json`, used an unfiltered
|APR| > 10% interesting-set and is superseded by the 1006 run — 87
of its 111 rows were funding-floor noise. It is kept in `results/`
for the record, not as a result.)

---

## 5. Structured signal report format

Every scan produces **one JSON + one short markdown summary**, same
timestamped stem, in `results/`:

```
results/scan-YYYY-MM-DD-HHMM.json
results/scan-YYYY-MM-DD-HHMM.md
```

**JSON** (`scan-*.json`): `summary` (perps scanned, below-entry
count, candidates scored, passes, min_apr, min_score) + `rows` (one
per scored coin: coin, APR, mark, 24h volume, OI, OKX APR, score,
dimension breakdown, missing dimensions, liquidity-gate flag,
verdict).

**Markdown** (`scan-*.md`): one-line header, the counts, the ranked
table (rank, coin, APR, score, verdict, volume, OI), the scoring
weights, and — appended by hand or by the agent — the decision log
lines for every evaluated signal (§2).

**Cross-venue reports** follow the same pattern:
`results/cross-venue-YYYY-MM-DD-HHMM.json` (matches, per-venue APRs,
spread, candidate flag, long_venue) with the table printed to stdout.

---

## 6. The validated setup (Strategy A — funding harvest, long-only)

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

---

## 7. Scored backtest — the honest result (v0.3)

`--min-score` on `scripts/backtest_funding.py` gates long entries on
the point-in-time 0-10 score. At each entry decision the score is
computed from data up to that hour ONLY (current APR, last 24h of
funding, last 25 hourly closes + highs/lows) — no lookahead. Only
the 4 dimensions computable from Hyperliquid history are used
(extremity, persistence, trend, volatility); liquidity and
cross-venue need data that does not exist historically, so they are
dropped and the remaining weights renormalize (see `scripts/score.py`).
The score gate applies to long entries only.

Same window as the v0.2 validation (208 days, 2026-04-22 ->
2026-11-16, BTC/ETH/HYPE, $100 1x):

| Variant | Trades | PnL | Win | Signals seen | Filtered |
|---|---|---|---|---|---|
| **Baseline (unscored, v0.2)** | **166** | **+$43.52** | **56%** | — | — |
| min-score 2 | 158 | +$44.46 | 57% | 179 | 21 |
| min-score 3 | 100 | +$20.20 | 55% | 223 | 123 |
| min-score 6 | 2 | +$8.00 | 100% | 385 | 383 |

**The scorer does NOT improve total PnL.** Min-score 2 is +$0.94
above baseline — noise, and the threshold was picked after looking
at the results (in-sample). Min-score 3 is worse (+$20.20).
Min-score 6 barely trades at all (2 trades: BTC 0, ETH 0, HYPE 2).

**Why threshold 6 fails — a structural reason, not a bug.** Entries
fire on the *first* hour funding crosses below -10%. At that hour,
persistence is mechanically low (only ~1 of the last 24 hours is
below -10%) and extremity is usually just past the threshold. Across
the 166 baseline trades the mean entry score is **3.03** (median
2.93, 90th percentile 4.33). A threshold of 6 was miscalibrated — it
asks for a score the entry rule almost never produces.

**What the score DOES predict.** Per-trade quality rises with score:
win rate 56% overall -> 72% for scores >= 4, and average PnL/trade
-$0.04 (score < 2) -> +$0.23 (2–3) -> +$0.29 (3–4) -> +$0.48 (4–5).
But not enough to beat the baseline on total return, because higher
thresholds remove too many profitable trades along with the bad ones.

**Conclusion: the unscored v0.2 setup stays the validated default.**
The scorer's value is as a **ranking/evaluation tool** for the live
scan — where all 6 dimensions exist and the liquidity gate filters
traps — not as a validated entry filter. Do not use `--min-score` as
a live filter without a fresh out-of-sample validation on a new
window first. The diagnostic file with every baseline trade's entry
score attached is
`results/backtest-2026-11-16-208d-scored0.json` (min-score 0: all
signals pass, scores recorded).

---

## 8. Other strategies (not the default)

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

Same asset, different funding on two venues (e.g. HL vs OKX): long
where funding is negative, short where positive — collect both sides in
theory. In practice: funded accounts on both venues, margin on both
sides, transfer latency, spread closes fast. Detection is now
implemented (`scripts/cross_venue.py`, §4); execution is not
backtested here — screen live funding on both venues before building
anything.

### D. Funding squeeze (variant of A — higher risk)

Long when funding is deeply negative **and price is already rising**
(shorts getting squeezed). Exit on funding normalisation or time stop.
Superior Trade's own backtest (BTC, Jan–May 2026): 8 trades, 38% win,
**-0.75%**, Sharpe -0.75 — negative in a downtrend. Suggested
refinements: higher-timeframe regime filter (e.g. only enter when 1d
close > 1d EMA-50) and a multi-pair scan. Experimental, never the default.

---

## 9. Live scan — first run (2026-11-16 ~10:00 UTC)

`results/scan-2026-11-16-1000.{json,md}` — 235 perps scanned, 26 below
-10% APR, 29 scored (incl. BTC/ETH/HYPE as reference rows), 2 PASS.

| Rank | Coin | APR | Score | Verdict | Vol 24h | OI |
|---:|---|---:|---:|---|---:|---:|
| 1 | TRUMP | -76.2% | 6.33 | PASS | $3.06M | $6.84M |
| 2 | WLD | -32.6% | 6.12 | PASS | $8.20M | $27.08M |
| 3 | XMR | -178.2% | 5.93 | WATCH | $25.06M | $91.09M |
| 4 | CC | -38.3% | 5.86 | WATCH | $1.60M | $4.15M |
| 5 | SKR | -2775.5% | 5.84 | WATCH | $6.99M | $1.04M |

Notes: ETH (-8.3%), BTC (+10.8%) and HYPE (+11.0%) appear as
reference rows — none signals right now (the paper ETH long from the
16:21 WIB live watch is still open; its exit is APR >= 0, not this
scan). The known traps did NOT top the ranking: RUNE, NIL, MELANIA,
BIGTIME and 9 others were capped at 4.00 by the liquidity gate. SKR
still ranks 5th on extremity alone (liquidity dimension 0.05) — a
reminder that the score ranks signals, it does not bless them; the
debate (§2) is where a thin book gets NO-GO'd. TRUMP/WLD passing
means "worth the full evaluation", not "buy now" — both have thin
liquidity dimensions (1.46 and 2.74) and would face a hard liquidity
question in debate.

---

## 10. Data sources (public, no key)

Hyperliquid Info API `POST https://api.hyperliquid.xyz/info`:

| Data | Request | Notes |
|---|---|---|
| Live funding (all perps) | `{"type": "metaAndAssetCtxs"}` | `funding` per asset in the `universe`; also `dayNtlVlm` (24h notional volume, USD), `openInterest` (coin units — x markPx for USD), `markPx`. The +10.95% APR many perps sit at is the funding floor (0.00125%/h). |
| Funding history | `{"type": "fundingHistory", "coin": "BTC", "startTime": ms, "endTime": ms}` | 500 rows/page — paginate with `startTime` |
| Candles 1h | `{"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": ms, "endTime": ms}}` | max ~5,000 candles (~208 days at 1h); NOT the old `candle` type (422s) |

OKX public API (no key): `GET
https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP`
— per-instrument, funds every 8h (APR = 8h rate x 3 x 365). Binance
(`/fapi/v1/premiumIndex`) returns HTTP 451 and Bybit
(`/v5/market/tickers`) returns 403 from this VM (geo-blocked) — both
are attempted best-effort in `cross_venue.py` and skipped gracefully.

Rate limit (Hyperliquid): 1,200 weight/minute per IP. Funding row
`time` is the payment time at the END of its hour; a row with time in
(candle_open, candle_close] is the payment for holding that hour.

## 11. How to run

```bash
python3 scripts/score.py --apr=-0.183 --apr-history=-0.05,-0.08,... \
    --closes=1790,1795,... --volume=1200000000 --oi=350000000 \
    --other-apr=-0.059                      # score one signal (JSON out)

python3 scripts/scan.py                     # live scan, ranked table
                                            # -> results/scan-YYYY-MM-DD-HHMM.{json,md}

python3 scripts/cross_venue.py              # cross-venue spread matcher
                                            # -> results/cross-venue-YYYY-MM-DD-HHMM.json

python3 scripts/backtest_funding.py --days 208 --end 2026-11-16 \
    --coins BTC ETH HYPE                    # unscored v0.2 baseline
python3 scripts/backtest_funding.py --days 208 --end 2026-11-16 \
    --coins BTC ETH HYPE --min-score 6 --out results/scored.json
                                            # scored variant (v0.3, see §7)
python3 scripts/sweep.py --out results/sweep-YYYY-MM-DD.json
                                            # 48-combo grid + out-of-sample validation
```

- Stdlib only, no keys, no trading. Entry/exit evaluated at hourly closes
  (no lookahead: the decision uses the funding rate of the hour just
  closed, fill at that close).
- `backtest_funding.py` accepts `--entry-apr`, `--exit-apr`, `--stop`,
  `--timeout`, `--side`, `--end YYYY-MM-DD` to test other setups — but
  changing the defaults needs a fresh sweep + out-of-sample check first.

## 12. Rules before any live talk

1. Backtest first, on a fresh window, before quoting any return.
2. An hourly funding APR is an annualisation of a 1-hour spike — a
   -4,000% APR does NOT mean that return for a year. Extreme funding
   usually means an illiquid/new listing whose price swings dwarf the
   funding income; the -5% stop can trigger before funding is collected.
3. Funding flips hourly. A setup that is valid now can be gone next hour.
4. Never present a backtest as a promise. Always report: window, trades,
   win rate, return, max DD, and the buy-and-hold comparison.
5. A PASS score or a GO verdict authorizes a *paper* position only.
   Live deployment needs Jesse's explicit per-action approval, a funded
   API wallet (he has none), and a dry-run/paper phase first.
6. A spread flagged by the cross-venue matcher is a detection, not a
   trade. It still has to pass the liquidity gate and the full
   pipeline before any position.

## Sources

- Superior Trade skills: `funding-rate-arbitrage`, `basis-arb`,
  `funding-squeeze` (github.com/Superior-Trade/superior-skills) — the
  v0.1/v0.2 basis.
- Hyperliquid docs: funding mechanics, Info API
  (hyperliquid.gitbook.io/hyperliquid-docs)
- **TauricResearch / TradingAgents** (github.com/TauricResearch —
  priority reference for v0.3): the multi-agent pipeline structure
  (Analyst Team -> Bull/Bear debate -> Research Manager -> Trader ->
  Risk Management team -> Fund Manager), the decision log (memory
  log) with ratings settled later against realized/alpha return, the
  report tree (per-section markdown + consolidated report), and
  point-in-time backtest integrity (grid stops at the present; one
  failed cell never ends the sweep). Structure borrowed, code not
  copied — the pipeline here is a procedure the agent follows, not a
  program.
- **TauricResearch / Trading-R1** (research terminal, modes
  `auto` / `research` / `analytical`): inspiration only for the
  "analytical mode" idea (numbers first, narrative second). NOT
  installed, its API is NOT used — it needs a Tauric API key.
- **Event-Driven Arbitrage Scanner** (github.com/d-wwei/
  Event-Driven-Arbitrage-Scanner): the staged workflow (search ->
  verify -> compute -> risk -> score -> report), the two-layer
  ranking (0-10 quality score, then a secondary sort), and the
  structured report format (overview table + per-opportunity detail
  + action summary), adapted from US-equities event arb to perp
  funding signals.
- **CloddsBot** (github.com/alsk1992/CloddsBot,
  `src/arbitrage/index.ts`): cross-platform matching by question
  similarity (Jaccard >= 0.8), 3 arb types (same_event / correlated /
  cross_platform), spread / spreadPct / profitPer100, 2% minimum
  spread, detection-only. Borrowed the matching idea: match the same
  coin across venues by normalized symbol and compute the funding
  spread. Detection only, no execution — same as here.
