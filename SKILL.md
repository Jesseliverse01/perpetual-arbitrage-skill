---
name: perpetual-arbitrage
description: "Perpetual futures arbitrage on Hyperliquid (and other venues): funding-rate harvest, spot-perp basis / cash-and-carry, cross-exchange funding spread, funding-squeeze setups, and delta-neutral cross-venue funding harvest (Strategy E). Includes a staged evaluation workflow (Scan -> Verify -> Score -> Debate -> Decide -> Report), a multi-agent evaluation pipeline, a 0-10 signal scorer (scripts/score.py), a live scanner (scripts/scan.py), a cross-venue funding-spread matcher (scripts/cross_venue.py), a live multi-venue scanner (scripts/multivenue_scan.py), and runnable backtests (scripts/backtest_funding.py, scripts/sweep.py, scripts/backtest_strategy_e.py, scripts/backtest_basis.py) on public APIs only. Live execution is an explicit opt-in through scripts/live/ (testnet by default; see LIVE.md). Use when the user asks about perp arbitrage, funding-rate trading, basis trades, or wants a backtest or a live scan run."
---

# Perpetual Arbitrage

Version 0.10.0, 2026-11-21. Paper/backtest first; live is an explicit opt-in (see LIVE.md).

## Please read this first

This is **not financial advice**. It is a research tool — a way to study
how funding rates behave and to test ideas on old data before you risk
anything real.

- **Live trading is off by default.** Every core script reads public
  data and writes a report. Live mode (`scripts/live/`,
  `execute.py --mode live`) places orders only with `--live-confirm`
  and a yes per ticket, on testnet unless `--mainnet` is passed.
  Keys come from the environment only.
- **A backtest is not a promise.** Past results do not predict what
  happens next. A strategy that made money on old data can lose money
  tomorrow.
- **Funding rates are wild.** They can flip every hour. A -4,000% APR
  does not mean you earn that for a year — it is one hour, annualized.
  Most of the time it is gone by the next hour.
- **You can lose money fast.** The risks include liquidation, a venue
  going down, one leg of a two-leg trade filling while the other does
  not, slippage, and fees eating the whole edge. There is also venue
  and smart-contract risk you cannot fully control.
- **No returns are guaranteed.** None. If anyone tells you this is
  passive income, they are selling you something.
- **Do your own research.** Check the numbers yourself, paper-trade
  first, and never trade money you cannot afford to lose.

If you skip everything else, do not skip that.

## What this is

Perpetual futures ("perps") have a trick built in: every hour, one side
pays the other. That payment is the **funding rate**. When too many
people are short, shorts pay longs — and the rate goes deeply negative.
This skill watches those payments and asks one question: is there a
trade here?

It covers five ideas, all on public data:

| Strategy | What it does | Where to read |
|---|---|---|
| **A. Funding harvest (long-only)** | Buy a perp when funding is deeply negative, exit when it flips back to zero. | §6 — the validated default |
| B. Basis / cash-and-carry | Buy the cheap leg, sell the expensive leg, earn the gap as they converge. | §8 B |
| C. Cross-exchange funding spread | Same coin, different funding on two venues. | §8 C — detected by §4 |
| D. Funding squeeze | Buy when funding is deeply negative *and* price is already rising. | §8 D |
| **E. Delta-neutral cross-venue harvest** | Long the venue that pays funding, short the venue that charges it — same coin, same size, so price moves cancel out. | §13 |

Around those strategies sit the tools that keep you honest: a staged
workflow (§1), a debate-style evaluation pipeline (§2), a 0–10 signal
scorer (§3), a live scanner (§9), a cross-venue matcher (§4), and a
multi-venue scanner for Strategy E (§13), and a unified execution
cycle (§16) that any multi-venue trade can use. Everything runs on public
APIs. Nothing needs a key. CCXT (§15) is an optional extra — the same
venue data through one library, for readers who prefer it. The core
scripts do not need it.

---

## 1. The staged workflow

Every signal goes through six stages, in order. Skip a stage only if
it is marked optional.

| Stage | What happens | Output |
|---|---|---|
| **1. Scan** | `scripts/scan.py` fetches all 235 Hyperliquid perps, scores every coin below -10% APR, prints a ranked table, saves JSON + markdown in `results/`. | Ranked candidate list with scores |
| **2. Verify** | For each PASS candidate, check the numbers yourself: re-read the current funding rate and mark price from `metaAndAssetCtxs`; check the coin actually trades (volume/OI from the same response); check the funding history for the last 24h (is this a 1-hour spike or a sustained print?). A candidate whose numbers do not reproduce is dropped here. | Verified facts per candidate |
| **3. Score** | `scripts/score.py` 0-10 score (see §3). A signal below 6 is WATCH/SKIP and does not go to debate. | Score + dimension breakdown |
| **4. Debate** | The multi-agent pipeline (§2): three analyst roles in turn, then Bull vs Bear, then the Risk Manager verdict. | GO / NO-GO / REDUCE + reasons |
| **5. Decide** | The decision is the Risk Manager's verdict, sized by the score and the debate: GO = take the paper position at the validated size; REDUCE = take it at half size (or watch one more hour); NO-GO = skip and log why. No decision without a logged verdict. | Position (or explicit skip) |
| **6. Report** | Append the decision to the scan's markdown summary and to the decision log (§2). One JSON + one short markdown summary per scan, in `results/`. | `results/scan-YYYY-MM-DD-HHMM.{json,md}` + decision log |

Analytical mode: when you evaluate, numbers first — data and valuation
tools, with narrative/context second. An argument without a number
does not count.

---

## 2. The multi-agent evaluation pipeline (procedure, not code)

Think of it as a small team arguing about one trade. Five seats, each
with its own job. The agent plays every seat in turn — this is a
procedure to follow, not a program, and it never places a trade.

One honest limit, before you use it: this pipeline runs on ONE model
in ONE context, in order. The analysts, Bull/Bear, and Risk Manager
are roles the same model plays — not independent agents. There is no
real context isolation and nothing actually runs in parallel. The
debate is a forced two-sided self-check: the same model argues both
sides, and it can end up agreeing with itself. Treat the verdict as
a single-model judgment, not a consensus.

The seats, in order: Analyst Team (three specialist roles) -> Bull/Bear
debate -> Trader decision -> Risk Management team -> Fund Manager
approval. Every evaluation keeps a decision log, and every number must
be computable from data available at that moment (no peeking at the
future).

### The seats

**Analysts — three specialist roles, played one after another, each
with its own inputs.** Answer each role's questions from that role's
inputs only; do not carry one role's conclusion into the next.

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
decisively it wins — on a five-tier rating scale. Commit; do not manufacture a direction to appear
decisive. Choose NO-GO only when the evidence is still balanced
after weighing, or too thin to support a call.

**Risk Manager (verdict).** The final word, one of three:

| Verdict | Meaning |
|---|---|
| **GO** | Take the paper position at the validated size ($100, 1x). |
| **REDUCE** | Take it at half size ($50) or watch one more hour first. Use when the score passes but one dimension is weak (e.g. liquidity 1–3, or volatility near the stop). |
| **NO-GO** | Skip. Log the reason. A NO-GO is a decision, not a failure to decide. |

**Fund Manager approval.** For paper trading, the "fund manager" is
the user's standing rule: paper only, no live trade without the user's explicit
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

A backtest's grid stops at the present, because a future date has no
outcome to settle against. Same rule here: every number you use must
be computable from data available at that moment. No future data, no
lookahead, no "this would have scored well yesterday". The backtest
enforces this by construction (§7); live evaluations enforce it by
only reading live/history endpoints, never by peeking at a later print.

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

## 4. Cross-venue matching rules

`scripts/cross_venue.py` — detection only, no execution.

Match the same asset across venues, compare the numbers, flag the
spread. Matching is exact after symbol normalization (the matched
key is a coin symbol), and the number compared is the funding APR:

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

The idea is simple. When funding is deeply negative, shorts are
paying longs to hold their position. You buy the perp, collect the
payments, and leave when funding flips back to zero or above.
Funding on Hyperliquid is paid **hourly**; APR = hourly rate x 24 x 365.

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
| BTC | 31 | 51.6% | +$2.55 | +2.55% | 2.61% | -17.6% |
| ETH | 36 | 55.6% | +$11.96 | +11.96% | 7.61% | -24.8% |
| HYPE | 97 | 57.7% | +$27.98 | +27.98% | 4.22% | +63.6% |
| **Combined** | **164** | — | **+$42.49** | **+14.16% on $300** | — | — |

The baseline was re-fetched 2026-11-17 with the fixed code; the
previous run was +$43.52 on 166 trades. The $1.03 move is almost
entirely data drift at the window edges, not the fix: two HYPE trades
at the window start no longer fire in the re-fetched data (-$0.88),
one BTC entry shifted three hours (-$0.14), and the stop-loss funding
fix itself accounts for only about $0.006 (the two ETH stop trades).

It beat buy-and-hold on BTC and ETH (positive while buy-and-hold
lost 18–25%). But it **lagged HYPE's +63.6% rally** — it is out of the
market most of the time, so it wins in chop and downtrends and lags
strong uptrends.

### What the sweep proved (and what NOT to do)

`scripts/sweep.py` tested 48 parameter combos on window A (120d,
2026-07-19 -> 2026-11-16) and validated the top 3 on window B (88d,
2026-04-22 -> 2026-07-19). Results in `results/sweep-2026-11-16.json`.

- **Stricter entry (-20%) looks better on A (+$8.81 vs +$8.28) but loses
  out-of-sample (B: +$24.61 vs +$35.37) and combined ($33.42 vs $43.65).**
  It is overfitting to window A — do not use it. The -10% default is the
  safer choice.
- **The short side hurts.** Every "both" combo underperformed its
  long-only twin (e.g. -10% both +$5.88 vs long +$8.28 on A). Shorting
  into +30% funding gets squeezed. Long-only is the default.
- **-30% entry is worse** (best +$4.85 on A) and **late exits (-5%) hurt**
  (worst combo -$7.99 on A).
- **Timeout 24h vs 48h is identical** — exits are always funding flips.

### Honest finding — the money is NOT from funding

Across all 164 trades, funding income was only **$0.10–$0.25 per coin**.
The PnL came from price moves. Here is why: extreme funding is a
1-hour spike that normalizes right after entry. So at 1x, this is
really a **positioning / mean-reversion signal** — buy the dip when
shorts are paying hard, exit when funding normalizes — not a carry
harvest. Real funding harvest needs leverage or longer holds, and both
add risk. Do not sell this as "passive funding income". By the lock classification in §13, this is synthetic edge — a directional position with a funding tailwind, not a locked trade. Never call it arbitrage.

---

## 7. Scored backtest — the honest result

`--min-score` on `scripts/backtest_funding.py` gates long entries on
the point-in-time 0-10 score, computed from data up to that hour only
— no lookahead. Only the 4 dimensions computable from Hyperliquid
history are used (extremity, persistence, trend, volatility);
liquidity and cross-venue are dropped and the weights renormalize.
Same window as §6 (208 days, 2026-04-22 -> 2026-11-16, BTC/ETH/HYPE,
$100 1x):

| Variant | Trades | PnL | Win | Signals seen | Filtered |
|---|---|---|---|---|---|
| **Baseline (unscored, v0.2)** | **164** | **+$42.49** | **56%** | — | — |
| min-score 2 | 155 | +$42.49 | 57% | 177 | 22 |
| min-score 3 | 98 | +$18.75 | 55% | 218 | 120 |
| min-score 6 | 3 | +$7.94 | 67% | 381 | 378 |

**The scorer does NOT improve total PnL.** Min-score 2 now ties the
baseline exactly (+$42.49 vs +$42.49) — no improvement at all, and
the threshold was picked after looking at the results. Min-score 3
is worse; min-score 6 barely trades (3 trades: BTC 1, ETH 0,
HYPE 2). Why: entries fire on the first hour below -10%, so the mean
entry score across the 164 baseline trades is only **3.04** — a
threshold of 6 was miscalibrated.

**Conclusion: the unscored setup stays the validated default.** The
scorer is a **ranking/evaluation tool** for the live scan, not a
validated entry filter. Do not use `--min-score` as a live filter
without a fresh out-of-sample validation first. The diagnostic file
with every baseline trade's entry score is `results/backtest-2026-11-16-208d-scored0.json`.

Two limits of the scored backtest itself: the liquidity gate (cap at
4.0 for zero-liquidity signals) never applies in backtest mode,
because the liquidity dimension is dropped there — so the scored
backtest cannot validate the trap filter for illiquid coins. Each
scored trade now records `entry_score_dims` and
`entry_score_missing`, so a score can be traced to the dimensions
behind it.

---

## 8. Other strategies (not the default)

### B. Basis / cash-and-carry (two-leg, hedged)

True basis arb is hedged: long perp + short spot when perp trades at a
discount to spot (basis < 0), or short perp + long spot when perp trades
at a premium (basis > 0). You earn the basis as the legs converge, plus
funding on the perp leg. Basis = (perp_mark - spot_mid) / spot_mid.

The hedged backtest now exists: scripts/backtest_basis.py. It fetches
perp and spot hourly candles plus funding history from the public
Hyperliquid API, resolves each coin's spot pair at runtime from
spotMeta (UBTC/USDC, UETH/USDC, USOL/USDC), and simulates both legs
at $100 notional, 1x, with every cost line in: taker fees on all four
fills, funding accrued hourly on the perp leg (signed), and a borrow
cost parameter for the short-spot direction. Entry at |basis| above
20 bps, exit on convergence to 5 bps, a 72h timeout, or an adverse
stop at 40 bps, plus a hard stop at -$1.00 net. Entries need the
basis beyond the threshold on 2 consecutive closes, filled at the
second close.

Why the confirmation filter matters: the first version entered on a
single close and showed +$5.66 on 53 trades with a 100% win rate.
That was not an edge. Spot closes are last-trade prices, and one hour
can print a transient spike that reverts the next hour. The worst case
was a -100.6 bps BTC print on 2026-07-08 that vanished in one hour
and alone contributed +$0.90. That run is kept as
results/backtest-basis-2026-11-16-naive.json to show why the filter
exists. Do not quote its total as a result.

Validation, 203 days (2026-04-26 -> 2026-11-16; spot history starts
2026-04-26, so the window is 203 days even though 208 were
requested), $100 notional per leg, 1x. Results in
results/backtest-basis-2026-11-16.json. Re-run verified identical.

| Coin | Trades | Win | PnL | Price | Funding | Borrow | Fees |
|---|---|---|---|---|---|---|---|
| BTC | 0 | — | $0 | $0 | $0 | $0 | $0 |
| ETH | 0 | — | $0 | $0 | $0 | $0 | $0 |
| SOL | 2 | 100% | +$0.13 | +$0.41 | +$0.00 | -$0.00 | -$0.28 |
| Combined | 2 | 100% | +$0.13 | +$0.41 | +$0.00 | -$0.00 | -$0.28 |

The honest result: on the three majors the strategy barely trades.
BTC and ETH saw zero confirmed dislocations in 203 days; SOL saw
two, both converged within 3-7 hours, for +$0.13 combined. The 100%
win rate means nothing on two trades. The basis on majors is thin
(median |basis| about 4.5 bps) and the 14 bps round-trip fee eats
almost every move. Funding contributed about $0.00. Like Strategy E
on majors, this is a working machine, not a return stream. The
opportunity set, if there is one, is the wider universe of spot
pairs, which this backtest does not cover yet.

Live feasibility: the short-perp + long-spot direction (perp at a
premium, roughly 70% of hours in the window) is feasible on
Hyperliquid alone. Spot long is a cash buy and perp short is a normal
short, but it needs cash for the spot leg plus margin for the perp
leg, about $200 capital per $100-notional trade. The long-perp +
short-spot direction is not feasible on Hyperliquid alone: spot
cannot be shorted without borrowing the asset on a lending venue. The
backtest prices borrow at 5% APR by default (--borrow-apr) but no
borrow source is wired up. That direction stays a live blocker, not a
solved problem. Both directions also need both legs filled at the same
time; a fill on one leg without the other leaves a naked directional
position. Slippage is not modeled, so treat every PnL number as an
upper bound.

The net edge, when you do size one:

```
net_basis = futures_price − spot_price
net_edge  = net_basis − entry_fees − exit_fees − funding − borrow − carry_cost
annualized_edge = net_edge / spot_price × (365 / days_to_expiry)
```

Every cost line has to be in there before the edge counts. If the hedge needs rolling, or the borrow is uncertain, the edge is synthetic, not locked (§13). Annualized edge is the number you compare with the funding APRs in §6 and §13.

### C. Cross-exchange funding spread

Same asset, different funding on two venues (e.g. HL vs OKX): long
where funding is negative, short where positive — collect both sides in
theory. In practice: funded accounts on both venues, margin on both
sides, transfer latency, spread closes fast. Detection is implemented
(`scripts/cross_venue.py`, §4); the full delta-neutral version of
this idea — with sizing, close rules, cooldown, and a backtest — is
now **Strategy E (§13)**. What §13 adds over this section: a real
two-leg trade (not a detection), the 70% entry band, and a
208-day HL-vs-Lighter backtest.

### D. Funding squeeze (variant of A — higher risk)

Long when funding is deeply negative **and price is already rising**
(shorts getting squeezed). Exit on funding normalisation or time stop.
An earlier public backtest of that shape (BTC, Jan–May 2026): 8 trades, 38% win,
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

### Which coins actually signal

How often does each coin even go below -10% APR? Thirty days of
hourly Hyperliquid funding (2026-10-17 -> 2026-11-16, 720 hours per
coin), from `results/funding-frequency-2026-11-16.json`:

| Coin | % of hours below -10% APR | Mean APR | Lowest APR |
|---|---:|---:|---:|
| SOPH | 40.8% | -150.6% | -4,380.0% |
| NIL | 38.2% | -27.9% | -2,198.8% |
| INIT | 33.3% | -306.7% | -4,380.0% |
| MELANIA | 30.3% | -15.5% | -287.3% |
| RUNE | 18.1% | -12.9% | -422.3% |
| CC | 15.8% | 0.2% | -168.1% |
| BIGTIME | 11.4% | 3.3% | -338.8% |
| DOGE | 11.4% | 3.6% | -34.5% |
| XMR | 7.8% | 53.9% | -309.0% |
| IMX | 7.4% | 4.6% | -189.1% |
| ETH | 5.3% | 6.2% | -67.2% |
| SOL | 5.1% | 5.4% | -26.5% |
| HYPE | 3.3% | 11.7% | -40.6% |
| BTC | 0.3% | 9.5% | -15.7% |
| SAGA / RESOLV / PONS | 0% | — | — |
| SKR | 1.1% | -21.0% | -3,515.8% |

Read this carefully, because it is easy to read it wrong.

**Often does not mean profitable.** The top four — SOPH, NIL, INIT,
MELANIA — are small, illiquid coins. Thin books, wide spreads,
violent price moves. They were never backtested; only BTC/ETH/HYPE
were validated. A coin that is below -10% forty percent of the time
is often a coin in a structural downtrend. The shorts are right, and
buying the dip into that just loses money more often.

**SKR is the other trap.** Its spikes are enormous (-3,515.8% at the
worst hour) but it is below -10% only 1.1% of the time. One violent
hour, then nothing. A scanner that chases the biggest number will
find SKR first and lose on it.

**The usable middle:** RUNE, DOGE, ETH, SOL, HYPE. Liquid enough to
actually trade, and they signal often enough to matter. RUNE is the
most frequent of the liquid set (18.1% of hours). BTC almost never
signals (0.3%) — and SAGA, RESOLV, and PONS never do at all, because
their funding sits at the +11% floor.

This section is a map, not a recommendation. Frequency tells you
where to look. It does not tell you a trade will work.

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

Lighter public API `https://mainnet.zklighter.elliot.ai/api/v1` (no
key; docs at https://docs.lighter.xyz/api):

| Data | Request | Notes |
|---|---|---|
| Cross-venue funding rates | `GET /funding-rates` | ONE call returns funding for hyperliquid, lighter, binance, bybit. `rate` is an **8h-normalized rate for every venue**: APR = rate x 3 x 365 (verified live 2026-11-16: HL BTC rate 7.786e-05 -> 8.53% APR vs HL live 8.51%). This is how Binance/Bybit rates are readable from this VM. |
| Funding history (Lighter leg) | `GET /fundings?market_id=N&resolution=1h&start_timestamp=&end_timestamp=&count_back=0` | Max 750 rows/call — chunk 30d. `rate` is a **PERCENT string** (0.0012 means 0.0012% = 1.2e-05/hour), signed by `direction` (+rate when `long`, −rate when `short`). Calibrated live: Lighter BTC 0.0012% long == funding-rates Lighter BTC 8h rate 9.6e-05 -> hourly 1.2e-05. `timestamp` is seconds, hour-aligned, payment at the hour mark. |
| Mark-price candles | `GET /markPriceCandles?market_id=N&resolution=1h&start_timestamp=&end_timestamp=&count_back=0` | OHLC marks. **Caps at 500 rows/call** — chunk 20d (480h), not 30d (a 30d chunk silently returns only the last 500h). |
| Market ids | `GET /orderBooks` | `market_id` per perp symbol (BTC 1, ETH 0, HYPE 24). Also returns `taker_fee` per market (0.0000 on these markets). |
| Last-24h prices | `GET /marketPriceCharts?market_ids=1,0` | 24 hourly marks per market — not enough for a backtest; use `markPriceCandles`. |

Funding on Lighter (docs/trading/funding): payments at each hour
mark; rate = clamp(premium, ±BigClamp 4%)/8 — the premium is
distributed over 8h like CEXs. Normal hourly rate 0.00125%
(InterestRate 0.01% / 8), same floor as Hyperliquid.

Aster public API (no key, works from this VM):
`GET https://fapi.asterdex.com/fapi/v1/premiumIndex` (bulk, all
symbols) or `?symbol=BTCUSDT`. `lastFundingRate` is an 8h rate:
APR = rate x 3 x 365. Funds every 8h.

Rate limit (Hyperliquid): 1,200 weight/minute per IP. Funding row
`time` is the payment time at the END of its hour; a row with time in
(candle_open, candle_close] is the payment for holding that hour.

CCXT (§15) reads the same data through one library instead of one
client per venue — optional, and on this VM it only works for
Hyperliquid.

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
                                            # scored variant (see §7)
python3 scripts/sweep.py --out results/sweep-YYYY-MM-DD.json
                                            # 48-combo grid + out-of-sample validation

python3 scripts/multivenue_scan.py           # Strategy E live scanner
                                            # -> results/multivenue-scan-YYYY-MM-DD-HHMM.json
python3 scripts/backtest_strategy_e.py --days 208 --end 2026-11-16 \
    --coins BTC ETH HYPE --out results/backtest-strategy-e-YYYY-MM-DD.json
                                            # Strategy E backtest (HL vs Lighter, close rules)

python3 scripts/backtest_basis.py --days 208 --end 2026-11-16 \
    --coins BTC ETH SOL --out results/backtest-basis-2026-11-16.json
                                            # Strategy B hedged two-leg backtest (§8 B)

python3 scripts/ccxt_funding.py              # current funding APR via CCXT (Hyperliquid only)
                                            # -> prints a table; needs `pip install ccxt` (§15)
```

- Stdlib only, no keys, no trading. (`scripts/ccxt_funding.py` is the
  one exception — it needs `pip install ccxt`, and it only covers
  Hyperliquid on this VM.) Entry/exit evaluated at hourly closes
  (no lookahead: the decision uses the funding rate of the hour just
  closed, fill at that close).
- `backtest_basis.py` fetches through curl (subprocess) so it works
  behind an authenticated egress proxy as well as on a plain
  connection. It takes about 40 seconds on first run and accepts
  `--cache-dir DIR` to reuse raw API responses on re-runs. A
  successful run prints one line per coin plus the output path:

```text
[BTC] spot pair @142, fetching perp + spot + funding ...
[BTC] 4878 perp candles, 4877 spot candles, 4992 funding rows
[BTC] trades=0 pnl=$0 return=0.0% win=None signals=6
[ETH] spot pair @151, fetching perp + spot + funding ...
[ETH] 4878 perp candles, 4876 spot candles, 4992 funding rows
[ETH] trades=0 pnl=$0 return=0.0% win=None signals=20
[SOL] spot pair @156, fetching perp + spot + funding ...
[SOL] 4878 perp candles, 4876 spot candles, 4992 funding rows
[SOL] trades=2 pnl=$0.13 return=0.13% win=1.0 signals=30
wrote results/backtest-basis-2026-11-16.json
```

  The window end is fixed by `--end`, so a fresh clone reproduces
  these counts. `trades=0` on BTC and ETH is the expected result,
  not an error: no confirmed dislocation cleared the 20 bps entry
  in 203 days (see §8 B).
- `backtest_funding.py` accepts `--entry-apr`, `--exit-apr`, `--stop`,
  `--timeout`, `--side`, `--end YYYY-MM-DD` to test other setups — but
  changing the defaults needs a fresh sweep + out-of-sample check first.

## 12. Rules before any live talk

One reminder before the rules: the disclaimer at the top of this file
applies to everything below. Nothing here is a promise, and no rule
below authorizes real money.

1. Backtest first, on a fresh window, before quoting any return.
2. An hourly funding APR is an annualisation of a 1-hour spike — a
   -4,000% APR does NOT mean that return for a year. Extreme funding
   usually means an illiquid/new listing whose price swings dwarf the
   funding income; the -5% stop can trigger before funding is collected.
3. Funding flips hourly. A setup that is valid now can be gone next hour.
4. Never present a backtest as a promise. Always report: window, trades,
   win rate, return, max DD, and the buy-and-hold comparison.
5. A PASS score or a GO verdict authorizes a *paper* position only.
   Live deployment needs the user's explicit per-action approval, a funded
   API wallet (they have none), and a dry-run/paper phase first.
6. A spread flagged by the cross-venue matcher is a detection, not a
   trade. It still has to pass the liquidity gate and the full
   pipeline before any position.
7. Strategy E in this skill is paper/backtest/scan only. A PASS from
   the multi-venue scanner authorizes a *paper* cycle only. Live
   execution needs accounts with balance on >= 2 venues + API access
   (the user has none set up) + the user's explicit per-action approval
   for that exact plan (§14). Never present live execution as
   available now.
8. Do not lower Strategy E's 70% MIN_NET_APR to manufacture trades
   on majors. The threshold exists because friction dominates below
   it. If the opportunity set looks thin, widen the *universe*
   (more coins), not the threshold.

## 13. Strategy E — delta-neutral cross-venue funding harvest

The idea: **long the venue paying funding, short the venue charging
it — same asset, same size, opposite sides.** The legs cancel price
exposure. What is left is the funding-rate spread, collected hourly
while the spread holds. "Delta-neutral" just means the two legs move
against each other when the price moves, so a price swing alone does
not hurt you. Everything else is risk management around that trade.
The algorithm and defaults below are this skill's Strategy E
parameters; the implementation here is public-data only
(paper/backtest/scan).

### The trade, in one table

| Rule | Value |
|---|---|
| Legs | Long venue with the lower (more negative) funding rate; short venue with the higher rate. Same asset, same notional. |
| Leg collateral | **$10 per leg** ($20 margin per arb) |
| Leg notional | **$50** (effective leverage 5 = target 10, capped at 5) |
| Max notional | $100 per position; max margin $120 per exchange; max 12 open arbs; max 3 new opens per cycle |
| Entry | Net APR = (short-venue rate − long-venue rate) × 24 × 365 in **[70%, 120%]** (hard cap 200% — above that is a spike or a data error, never a trade) |
| Funding accrual | Per hour: (short rate − long rate) × $50 |
| Max hold | **96h**; min hold **12h** (soft exits blocked before 12h) |
| Stop-loss | **−5% of (leg collat × 2) = −$1.00 net**, fires any time, even before min hold |
| Safe floor | `max(open_net_apr × 0.70, 70%)` — soft exits only fire below it, unless funding breakeven is already achieved |
| Cooldown | **2 consecutive losing closes → 48h block** on that asset; a winning close resets it |
| Fees modeled | HL taker 0.035% per side on the HL leg; Lighter taker 0% on these markets (from `orderBooks` `taker_fee` 0.0000). Slippage NOT modeled (no historical book data). |
| Friction estimate (breakeven flag only) | (leg notional × 2) × 15bps × 2 = **$0.30** per arb; breakeven achieved when peak funding > $0.30 × 1.5 = **$0.45** (sticky once true) |

### Open filters (in order, reject on first failure)

Adapted to what public data can check
(§13 scanner notes which are computable live):

1. Asset not in the **blacklist**: YZY, STBL, 2Z, CC, CHIP, VVV,
   MEGA, AZTEC, JTO, STABLE, alias:mu, alias:bmnr, EIGEN, WLFI,
   XMR, PYTH, ZRO, CRV. (Note the overlap with this skill's own
   illiquid-trap list: SKR, MELANIA, CC, NIL, INIT, SOPH.)
2. Asset not on cooldown (2 losses within 48h).
3. Asset not already open in another arb (one arb per asset).
4. 70% ≤ net APR ≤ min(120%, 200%).
5. `apr_24h ≥ 0` AND `apr_7d ≥ 0` (trailing means of the SIGNED spread
   for the candidate direction — no headline-only spikes; these are
   real filters now, not trivially true).
6. `apr_1h / apr_24h ≤ 5` and `apr_24h / apr_7d ≤ 3`.
7. Both legs' vol24h ≥ $100k, per-leg ≥ $250k — **only where the
   venue exposes volume** (Hyperliquid `dayNtlVlm` does;
   Lighter/Aster/OKX funding endpoints do not). Not applied where
   uncomputable; the row is flagged `volume_unknown` — and the verdict
   is capped at WATCH. A row with unknown volume on any leg can never
   PASS.
8. Both legs in the target exchanges (Hyperliquid, Lighter; Aster
   included in the scanner as a third venue).

For survivors, in net-APR-descending order: slippage check
(≤ 200bps per leg — needs a live execution API, not computable from
public data), entry-price basis check (≤ 30bps adverse basis — same),
effective leverage = min(10, 5, leg maxes), per-exchange margin cap
check, balance check (≥ leg collat × 1.1). Stop at 3 new opens per
cycle.

### Lock quality — classify before you size

Not every spread is a locked trade. Classify each proposed open before you size it. The class decides the size and what you write down with it.

| Class | What it means | What follows |
|---|---|---|
| **True lock** | Both legs fillable now at known prices — same asset, same size, margin pre-funded on both venues. Price moves cancel. Only the funding spread and the fees remain. | Full size. |
| **Soft lock** | The hedge exists, but one leg may move or fail before both fill. Thin book, one venue's data missing, spread history too short to verify, slippage or basis check not computable. | Half size (REDUCE). Write the kill condition down first: what closes the first leg if the second never fills. Fill the constrained leg first — thinner book, tighter margin, slower venue. |
| **Pre-funded lock** | The trade only opens as far as both venue accounts are funded. Capacity is the smaller funded balance, not the scanner's notional. | Size to the smaller funded balance. In paper mode this does not bind. It binds the moment the cycle goes live. |
| **Synthetic edge** | Positive expectation, no locked payout. One leg's funding without the hedge, or a spread that only pays if the price moves your way. | Not Strategy E. Never open it as an arb, and never call it arbitrage. |

Two more rules, same idea:

- **Stale data kills the candidate.** A funding reading older than the last hour mark is stale. Drop the candidate. Never carry a stale reading into the next cycle.
- **The plan records the class.** Step 8 lists, per proposed open: lock class, weakest leg, kill condition. A soft lock with no written kill condition does not open.

### Close rules — priority order, first match wins

For each open arb, evaluated every hour:

1. **`hard_stop_loss`** — `net_usd < −$1.00`. Fires regardless of
   min hold. (`net_usd = uPnL + funding_cum`, where uPnL is real
   basis drift — see the backtest notes.)
2. **`max_hold`** — held > 96h. Fires regardless of min hold.
3. **Min-hold gate** — held < 12h: skip the soft exits below, mark
   HOLD.
4. **`smart_neg_3`** — last 3 spread readings ALL < −10% APR (the
   spread reversed), and (current APR < safe floor OR breakeven
   already achieved).
5. **`smart_decl_6`** — last 7 readings strictly declining, same
   floor condition.
6. **`low_apr_6`** — last 6 readings ALL < 10% APR, AND funding
   breakeven NOT achieved. (Once funding has paid back friction
   ×1.5, low APR alone does not justify closing.)

### The decision cycle (procedure, not code)

One cycle = load state → fetch live data → reconcile → update
metrics → decide closes → decide opens → bracket SL/TP → plan →
wait for "yes" → execute closes first, then opens → persist state.
In this skill the cycle is a **paper procedure** run by the agent;
the execution step (§14) is replaced by "record the paper plan and
wait for the user".

1. **Load state** — `results/strategy-e-positions.json` and
   `results/strategy-e-cooldown.json` (fields: `arb_id`, `asset`,
   `long_exchange`, `short_exchange`, `opened_at`, `open_net_apr`,
   `leg_collat_usd`, `leg_notional_usd`, `effective_leverage`,
   `apr_history` (last 24), `peak_funding_cum`,
   `funding_breakeven_achieved`; cooldown: `consecutive_losses`,
   `last_loss_ts`). Append-only log: `results/strategy-e-history.ndjson`
   (events `CYCLE_STARTED`, `OPEN_PROPOSED`, `CLOSE_PROPOSED`,
   `CYCLE_DONE`, ...). Missing files start empty.
2. **Fetch live data** — run `scripts/multivenue_scan.py` (all venues
   at once) for opportunities; read the paper positions file for the
   open book.
3. **Reconcile** — paper positions have no venue drift (nothing is
   live), so this step is a consistency check: every open paper arb
   still has a live spread reading; a coin whose spread data went
   missing is flagged, not silently kept.
4. **Update per-arb metrics** — append the current SIGNED spread for
   that pair's direction to `apr_history` (last 24) — it can go
   negative when the spread reverses; accrue funding from the two
   venues' hourly rates on the position's fixed legs; update
   `peak_funding_cum` and the breakeven flag;
   recompute uPnL from the two venues' marks and `net_usd`.
5. **Decide closes** — the priority order above.
6. **Decide opens** — the filters above, from the scanner's ranked
   list, max 3 per cycle. Classify each proposed open (lock quality, below) before sizing.
7. **Bracket SL/TP (paper)** — record the intended stop per leg
   (SL trigger = liquidation price + 5% buffer toward entry; cross-leg
   TP = the other leg's SL price; omit TPs if they don't clear the
   entry on both sides). For Hyperliquid, prices clamp to 5 sig figs.
   In paper mode the bracket is a *record*, not an order — the
   cycle's hard stop is what actually protects the paper position.
8. **Build the plan** — Markdown: state (open arbs, margin used,
   aggregate uPnL/funding/NET, cooldowns), proposed closes with
   reason + expected realized, proposed opens with legs, net APR,
   collat/notional, basis, per-leg slippage (if known), lock class,
   weakest leg, kill condition, capital after the plan.
9. **Wait for the user** — a paper plan is executed (recorded) only on
   their explicit "yes" for that exact plan. "yes close only" / "yes
   open only" for partial; anything else = monitor and exit.
10. **Execute (paper)** — closes first: remove from positions,
    determine win/loss (realized = uPnL + funding − fees), update
    cooldown (loss → increment + timestamp; win → delete entry).
    Then opens: insert with `apr_history=[netApr]`,
    `peak_funding_cum=0`, `funding_breakeven_achieved=false`. Record the constrained leg first. If the other leg cannot be recorded in the same cycle, remove the first leg's record and log why — never leave a one-leg arb in the book.
11. **Write state back** — atomically (write `.tmp`, rename);
    append `CYCLE_DONE` with summary stats.

### Honest numbers

- Realistic ballpark: **low single-digit % monthly**, highly variable.
- Worst case: lose **10–20% of position notional** (venue outages,
  half-leg fills, sudden funding flips).
- Friction is real: at 5–7¢ per arb on $50 notional, a position needs
  **~40–70 hours** of funding accrual to break even.
- Dominant loss mode: **decay before breakeven** — the spread
  collapses before funding covers friction. Then adverse basis on
  entry, half-leg events, and venue/API outages.

### Backtest — HL vs Lighter, 208 days (2026-04-22 → 2026-11-16)

`scripts/backtest_strategy_e.py`, results in
`results/backtest-strategy-e-2026-11-16-208d.json`. Same window as
the §6 validation. Full data existed for all three coins
(HL funding 4,992h, Lighter fundings 4,985h of 4,992, both candle
series 4,991–4,993h). One fix found while running it: Lighter's
`/markPriceCandles` caps at 500 rows/call, so the script chunks
20d (480h), not 30d — a 30d chunk silently returns only the last
500h.

**Corrected in v0.5.3 — read this before the numbers.** The original
run reported 5 trades and +$0.19. It had two bugs: funding accrued
from the best direction each hour instead of the position's fixed
legs (so it could only grow), and the spread history was absolute
instead of signed (so the `apr_24h`/`apr_7d >= 0` filters were
trivially true and `smart_neg_3` could never fire). The numbers
below are the corrected re-run on the same window — 1 trade,
+$0.02. The old numbers are kept here once, for transparency.

| Coin | Trades | Win | PnL | Funding | Price (basis) | Fees |
|---|---|---|---|---|---|---|
| BTC | 0 | — | $0 | $0 | $0 | $0 |
| ETH | 0 | — | $0 | $0 | $0 | $0 |
| HYPE | 1 | 100% | +$0.02 | +$0.03 | +$0.03 | −$0.04 |
| **Combined** | **1** | **100%** | **+$0.02** | **+$0.03** | **+$0.03** | **−$0.04** |

Exit: 1× `smart_decl_6`; no stop-losses, no max-holds. The one
trade held 19h (above the 12h min hold), opened at 74.61% APR
(inside the band), long Lighter / short Hyperliquid. Funding
+$0.027, price +$0.027, fees −$0.035. Signals seen: BTC 4,814 /
ETH 4,814 / HYPE 4,797 — nearly all filtered.

**The honest result: on the three majors, the 70% threshold almost
never triggers.** Diagnostic on the full spread series (4,985h per
coin): mean net APR is only ~5% (BTC 4.64%, ETH 4.51%, HYPE 6.50%);
p99 is 21.5% / 23.5% / 54.3%. BTC's max spread in 208 days was
111.7% — one hour, which failed the trailing-mean/ratio filters
(a 1-hour spike). ETH never reached 70% at all (max 67.2%). HYPE
had 20 hours ≥ 70% and 16 in band — and with the signed filters
only 1 of them passed everything.

**Conclusion: Strategy E on BTC/ETH/HYPE is a "wait for a rare
event" strategy — +$0.02 in 208 days, 1 trade, is not a return
stream, it proves the machinery works.** The strategy's
real opportunity set is the full 200+ coin universe: the live
scanner (§13, below) found 9 in-band candidates on 2026-11-16
across all coins, vs 0 on the three majors. Do NOT lower the 70%
threshold to manufacture trades on majors — the threshold exists
because friction dominates below it. That backtest is now done — see the full-universe run below. It did
not change the threshold: 70% stays.

### Full-universe backtest — 81 coins, same 208 days

`results/backtest-strategy-e-2026-11-16-full-universe.json`. Same
window (2026-04-22 → 2026-11-16), same parameters, same corrected
script logic — only the universe is wider. Every coin listed as a
perp on BOTH Hyperliquid and Lighter, exact symbol match: 97 in the
intersection, 16 blacklisted and not run, 81 run. Of those 81, 6 had
zero hours of data (AI, AI16Z, BNB, CASHCAT, LAUNCHCOIN, SOL), so 75
coins actually had data; 62 traded, and the remaining 13 with data
saw no signal pass the filters. Signals seen across all coins:
305,734. Data coverage averages 4,146h of 4,992 per coin (some coins
listed mid-window).

This run is in-sample: the parameters and the window are the same
ones the strategy was developed on, and there is no out-of-sample
validation of the Strategy E parameter set yet (Strategy A has one,
§6). A half-window stability check was planned but not run — the E
re-runs were skipped. Quote the headline total as in-sample until a
fresh window exists.

The §13 portfolio caps (max 12 open arbs, max 3 new opens per hour)
are enforced by a greedy chronological filter on the per-coin
simulations (`apply_portfolio_caps` in
`scripts/backtest_strategy_e.py`): trades are considered in
entry-time order and accepted while a slot is free. The capped total
is the headline number; the uncapped total is kept in the results
file (`results['portfolio']`) for comparison. The filter is an
approximation, not a joint re-simulation — a rejected trade does not
change its coin's later signals or cooldown state. 12 open arbs at
$10 per leg also enforces the $120-per-exchange margin cap.

Blacklist note (2026-11-17): the 16 names were compiled 2026-11-16
with v0.4 (commit bec238d) and no per-name reason was recorded at the
time. The only documented part is the overlap with the illiquid-trap
list (filter 1, below). Two entries in the code were dead until this
fix ("alias:mu"/"alias:bmnr" could never match, because aliases are
resolved before the check); they are now the post-alias symbols MUU
and BMNR, which are not in the intersection, so the universe is
unchanged. Going forward, any addition to the blacklist needs a
one-line reason and a date, or it does not go in.

**Note (2026-11-17): the numbers in this table are from the pre-fix
run — the backtest assumed unlimited concurrency and reported no max
drawdown. The capped total from the fixed code is still being
computed; this table will be replaced when the E re-run lands.**

| | |
|---|---|
| Trades | 437 |
| Win rate | 78.5% (343 of 437) |
| PnL | +$30.62 |
| Funding | +$31.03 |
| Price (basis) | +$14.91 |
| Fees | −$15.48 |

(Components sum to $30.46; the $0.16 gap is rounding in the stored
per-trade values.)

Exits: 338× `low_apr_6`, 50× `smart_neg_3`, 33× `max_hold`, 16×
`smart_decl_6`, 0 hard stops. Most positions end the same way the
majors-only run did: the spread decays before anything else happens.

Top coins: APEX 19 trades +$2.79, S 24 trades +$2.02, 0G 21 trades
+$1.50, ZORA 21 trades +$1.45, BERA 10 trades +$1.31. Worst: BIO 6
trades −$0.14, SEI −$0.05, PENDLE −$0.02, ARB −$0.02. No coin lost
more than 14 cents.

**The honest result: the universe matters more than the threshold.**
On the three majors the strategy barely trades; across 81 coins it
trades 437 times and the funding leg — not price — carries the PnL
(+$31.03 funding vs +$14.91 price). That is the carry working as
designed. It is still small money: +$30.62 in 208 days, about 7
cents a trade, on $20 margin per arb. A working machine, not a
return stream. Nothing in this run says lowering the 70% threshold
would help — the trades that exist already come from in-band
spreads, and the exits show decay, not missed entries.

Same limits as the majors run: volume gates not applied, slippage
not modeled, hard stop at hour closes only, per-leg liquidation not
modeled. One gap in the universe itself: scale variants were not
matched (Lighter lists 1000BONK/1000FLOKI/1000NOT/1000PEPE/
1000SHIB/1000TOSHI while Hyperliquid lists kBONK/kFLOKI/kNOT/
kPEPE/kSHIB/kTOSHI — same underlying at 1000x scale). A follow-up
could add them with an explicit scale map; silently equating them
would hide the mapping.

**Backtest limits (stated, not hidden):** volume gates NOT applied
(no historical per-venue volume in public endpoints); slippage NOT
modeled; hard stop evaluated at hour closes only (hourly cycle
cadence), not intra-hour; per-leg liquidation is NOT modeled — a
leg liquidating intra-hour (or a venue outage mid-hour) is not in
the model, and a real two-leg position can lose one leg while the
other stays open (that did not happen in this window; it is a
stated limit, not a claim it cannot happen); Lighter funding
history missing ~7 of 4,992 hours (those hours accrue no funding
and skip soft exits); capital base differs from Strategy A ($20
margin per arb for $50 notional per leg vs $100 notional 1x) —
compare PnL in USD and return-on-margin, not raw return_pct,
across strategies.

### Live multi-venue scan — first run (2026-11-16 ~10:00 UTC)

`scripts/multivenue_scan.py` →
`results/multivenue-scan-2026-11-16-1000.json`. All four direct
venues OK: Hyperliquid 235 coins; Lighter funding-rates 231 coins
across binance/bybit/hyperliquid/lighter; Aster 750 coins; OKX 22
matched (candidates only). 285 coins on ≥ 2 venues, 31 candidates
with net APR ≥ 50%.

**Top 5 spreads (all SKIP — above the 200% hard cap):**

| Rank | Coin | Long | Short | Net APR |
|---:|---|---|---|---:|
| 1 | SKR | bybit −3,611.3% | aster −1,738.0% | **+1,873.4%** |
| 2 | BIRB | bybit −1,404.0% | aster −630.2% | **+773.8%** |
| 3 | PRL | lighter −463.4% | aster +30.6% | **+493.9%** |
| 4 | SAGA | aster +5.5% | hyperliquid +439.7% | **+434.2%** |
| 5 | BOT | bybit −661.9% | lighter −272.4% | **+389.4%** |

These are spikes on illiquid books — exactly what the 120% MAX and
200% hard cap exist to reject. **In-band (70–120%), all WATCH:**
HYUNDAI 117.4% (binance→bybit), LIT 93.6% (HL→aster), AZTEC 91.5%
(blacklisted), RESOLV 82.8% (aster→HL), W 77.9% (HL→okx), CC 77.2%
(blacklisted), SOPH 74.5% (HL→aster), ARC 73.3% (aster→bybit), GMX
73.0% (HL volume below the $250k per-leg gate). None is PASS: the
spike filters need spread history, which is only computable for
HL↔Lighter pairs, and none of these nine has its best pair on
HL↔Lighter. WATCH means "worth watching", not "open now".

## 14. Unified execution (optional, live since v0.7.0)

Strategy E's live execution path is a unified multi-venue execution
API: one interface every venue adapter implements, with ranked
funding strategies, batch order placement, and margin visibility
across venues. It is wired in since v0.7.0: the adapters live in
`scripts/live/`, `scripts/execute.py --mode live` runs them, and
`LIVE.md` is the setup guide. Testnet by default; mainnet needs
`--mainnet`.

Custom venues, since v0.8.0: the execution engine no longer knows
only Hyperliquid and Lighter. Any venue can be added by writing one
adapter file that implements the interface below. Since v0.9.0 all
five venues have working adapters: Hyperliquid, Lighter, dYdX,
Aster and Derive. A ticket whose venue has no working adapter is
refused with a clear message, never half-run. That is the whole
point: test the agent on the testnet of the venue you pick, with
your own credentials, before you run live arbitrage anywhere.

**What it needs (all three, no exceptions):**

1. Accounts with balance on at least two venues (any two of
   the five working adapters).
2. API access to that unified API (credentials for the account).
   Whoever runs live mode needs their own; never ask anyone to paste
   a credential in chat. Reads need the credentials too — there is no
   public/keyless mode for execution.
3. The user's explicit per-action approval for each exact plan. Their
   standing rule governs every real order.

### The adapter interface

Every adapter is a subclass of `VenueAdapter`
(`scripts/live/venue_base.py`) and implements the same methods.
`scripts/live/registry.py` maps a venue name to its adapter module
and its credential env prefix, and `get_adapter(venue, testnet)`
connects only the venues a ticket actually needs, cached per venue
and network.

- `connect(testnet)`: build the client from environment variables
  only. Testnet credentials use the `TESTNET` infix, for example
  `HL_TESTNET_PRIVATE_KEY` on testnet and `HL_PRIVATE_KEY` on
  mainnet. Never put a key in a file, never paste one in chat,
  never log one.
- `symbol_for(coin)`: the symbol the venue lists for one canonical
  scanner coin. The scanner uses the 1000-form for meme coins
  (1000PEPE); Hyperliquid lists the k-form (kPEPE), Lighter lists
  the 1000-form as-is.
- `get_balance()`, `get_positions()`, `get_open_orders()`: account
  state. Positions carry the side, size and entry price, plus
  funding accrued when the venue reports it.
- `fetch_funding_rate(symbol)`: the current funding APR as a float
  in percent. Positive means longs pay shorts; negative means shorts
  pay longs. Convert the venue per-interval rate with APR% = rate
  times intervals per year times 100. For an 8h rate that is rate
  times 3 times 365 times 100; forgetting the final times 100
  understates the APR by 100x. Not every venue quotes per 8h:
  dYdX and Derive quote per hour, so their APR is rate times 24
  times 365 times 100, and Aster quotes per symbol (8h for BTC,
  other symbols settle 4h or hourly), so the adapter reads the
  interval live and falls back to 8h only when that read fails.
  The scanner must not apply its 8h factor to a Derive or dYdX row.
- `fetch_mark_price(symbol)`: the current mark price as a float.
  Raise when the venue does not list the symbol on this network.
- `place_market_order(symbol, side, size, dry_run, confirm)`,
  `place_limit_order(...)`, `place_bracket(...)`: every write
  defaults to `dry_run=True` and touches nothing. A live write
  needs `dry_run=False` AND `confirm=True`, enforced before any
  network traffic. Side is the string "buy" or "sell"; size is in
  base units.
- `cancel_order(...)`, `cancel_all_orders()`, `reduce_only_close(...)`
  / `rollback_close(...)`: the cancel and unwind primitives. The
  base class supplies a default cancel-all (loop open orders,
  cancel each one) and a default rollback (find the position, work
  out its side, reduce-only close it). A venue overrides them only
  when it has a native call that does the same thing.
  `reduce_only_close` accepts both spellings for the side:
  "buy"/"sell" is already the closing order side, and "long"/"short"
  names the leg being closed (a long closes with a sell, a short
  with a buy). Both callers use it: the execution engine passes the
  order side, the base rollback passes the position side.

### Add your own venue

1. Copy `scripts/live/custom_venue.py` to
   `scripts/live/<venue>_client.py` and rename the class. Every
   method raises `NotImplementedError` with a comment saying what
   to fill in: auth, endpoints, signing, symbol mapping, testnet
   URL. Fill them in one at a time; a method you leave raising
   simply refuses that operation with a plain reason.
2. Register it in `scripts/live/registry.py`: the module name and
   the credential env prefix. Add its SDK to
   `requirements-live.txt` when it has one. The core skill stays
   stdlib-only; only `scripts/live/` may need extra packages.
3. Test on testnet first, with throwaway keys and a tiny size:
   connect, read the mark price and the funding rate, place a dry
   run, then one small live order, then a bracket, then a cancel,
   then a reduce-only close. Watch every step on the venue itself.
   Only after a full cycle works on testnet should mainnet even be
   discussed, and mainnet still needs `--mainnet` plus a yes per
   ticket.

### What is verified about each venue

- dYdX: implemented on the official `dydx-v4-client` SDK (1.1.6).
  Testnet app https://v4.testnet.dydx.exchange (HTTP 200). The
  testnet indexer works from this VM at /v4/perpetualMarkets (226
  markets); the earlier 404 was the wrong path (/v4/markets does
  not exist). Funding is hourly, so APR is rate times 24 times 365
  times 100. Public reads verified live (mark price, funding,
  balance, positions, orders) and dry runs simulate. A live signed
  market order was received and rejected on-chain (unfunded
  subaccount, equity tier limit 0), which proves key derivation,
  order construction, signing, broadcast and chain processing. NOT
  verified: a filled order, a resting bracket, a real cancel, a
  reduce-only close, the two-leg rollback. The faucet API returns
  202 but its USDC leg did not land for three fresh addresses after
  15 to 60 minutes; the native gas faucet works (10 DV4TNT). Fund
  a testnet subaccount in the browser first (deposit/recharge,
  300 test USDC). All perps USDC-settled. Auth is an Ethereum
  wallet signature. Testnet chain id dydx-testnet-4.
- Aster: implemented as raw HTTP plus `eth-account` (no maintained
  official V3 Python SDK exists). Auth is EIP-712 agent signing,
  not an API key and secret: set ASTER_TESTNET_SIGNER (the API
  wallet address) and ASTER_TESTNET_SIGNER_PRIVATE_KEY (its private
  key). Testnet app https://www.asterdex-testnet.com (HTTP 200).
  Public reads verified live (ping, exchangeInfo with 21 symbols,
  premiumIndex, funding rates; funding interval read live per
  symbol, 8h for BTC). Dry runs simulate, and a signed request
  reached the venue and got a parsed auth error ("No agent
  found"), which proves the signature path works. NOT verified: a
  filled order, a resting bracket, a real cancel, a reduce-only
  close, the two-leg rollback. A funded testnet account plus a
  registered API wallet can only be created in the browser. Watch
  for venue code -5050 (deposit gate, in force since 1 Sep 2026):
  if testnet returns it on the first signed call, signed calls are
  blocked until the venue lifts it. Chain id 714 testnet, 1666
  mainnet. The scanner already reads Aster funding live via
  premiumIndex (750 coins).
- Derive: implemented on the official `derive-py` SDK (0.1.5, MIT),
  v3 API https://testnet.api.derive.xyz/v3. Testnet app
  https://testnet.derive.xyz (HTTP 200). Public reads verified live
  on testnet (20 perps, mark price, per-hour funding as APR);
  writes are code-complete with the usual dry-run and confirm
  gates but NOT yet verified on a funded account. Funding is
  quoted per hour, so APR is rate times 24 times 365 times 100.
  Trading is gasless (off-chain signed orders); deposit/withdraw
  needs gas (ETH, OP Stack L2). Auth via session keys: set
  DERIVE_TESTNET_PRIVATE_KEY (session key), DERIVE_TESTNET_WALLET
  (owner wallet) and DERIVE_TESTNET_SUBACCOUNT_ID. The old v2 demo
  host api-demo.lyra.finance is not used.
- Lighter testnet: app https://testnet.app.lighter.xyz, host
  testnet.zklighter.elliot.ai, only 8 perps on testnet (AKBNKB,
  ASELS, ASELSB, BTC, ETH, SOL, THYAOB, TUPRSB). 10 dollar minimum
  notional per order. The API key index is set by the app (default
  3 in the adapter). Verified on a funded account: a market order
  that filled, a bracket whose entry filled with the stop-loss and
  take-profit legs resting on the venue, a real cancel that cleared
  both legs, and a reduce-only close that left the account flat.
  The two-leg live rollback is still NOT observed; it needs both
  venues funded at once.
- Hyperliquid testnet: app https://app.hyperliquid-testnet.xyz.
  The drip gives 1,000 mock USDC via a gasless API claim, only for
  addresses that deposited on mainnet. Verified: public reads, dry
  runs, refusal gates, and a real signed order submission that the
  venue received and rejected (unfunded throwaway address). A
  filled order, a resting bracket, a real cancel, and the live
  rollback were NOT observed.

Mainnet is wired for all five venues and documented in LIVE.md,
but it is NOT verified: no mainnet order was placed, no mainnet
key was used. Mainnet is for the user's own keys, verified by the
user with small size after a full testnet cycle works.

**Capabilities a unified API must expose for the cycle (§13):**

| Purpose | Capability |
|---|---|
| List ranked arb opportunities | ranked funding strategies (net APR, per-venue rates) |
| Pricing / fees / liq price | quotes per leg |
| Slippage estimate | per-leg slippage estimate |
| Market metadata (decimals, max leverage) | markets |
| Account balances | accounts |
| Open positions (with funding accrued) | positions |
| Outstanding orders (SL/TP audit) | open orders |
| Place orders (legs + bracket) | batch order placement (preferred), single order (fallback) |
| Cancel orders | order cancel / batch cancel |
| Historical spread | funding-spread history |

Never use deposit / withdraw / transfer / leverage / margin-mode
tools in a routine cycle — those are explicit user-initiated
operations, surfaced separately.

**Venue quirks — they bite at execution:**

- Lighter ignores `clientOrderId` when a bracket is present — match
  Lighter orders by (baseSymbol, side, size) in reconcile.
- Hyperliquid bracket prices need max 5 sig figs (and max
  `6 - baseDecimals` decimal places) — round before sending.
- Aster does not support bracket `stopLoss`/`takeProfit` — use a
  separate trigger order instead, or omit SL/TP.
- Aster funding fields are `null` in positions — fall back to
  estimating funding from spread history × held hours.
- A write failing with **401** = stale credentials: stop the cycle,
  surface it, do not retry. A write failing with **5xx**: do NOT retry
  automatically (the original may have committed on-venue → double
  open) — surface the error verbatim.
- **Half-leg risk is the killer.** If one leg fills and the other
  fails, roll back immediately (reduce-only market close on the
  filled leg); if rollback fails, surface an emergency-unwind notice
  and STOP the cycle.

## 15. CCXT — optional unified venue access

CCXT is one library for 100+ exchanges (104 in v4.5.92, MIT): the same
method names and return shapes on every venue. It is an optional way
to read the same public data this skill reads per venue. The core
scripts stay stdlib-only — nothing in this skill needs CCXT, and
adding it changes no rule, threshold, or result.

Install:

```bash
pip install ccxt
```

The methods that matter here:

| Method | What it returns, for this skill |
|---|---|
| `fetchTicker(symbol)` | Last price, bid/ask — the mark for a signal check |
| `fetchFundingRate(symbol)` | Current funding rate (hourly on Hyperliquid) — APR = rate × 24 × 365 |
| `fetchFundingRateHistory(symbol, since, limit)` | Funding history — persistence and trailing means |
| `fetchMarkets()` | All pairs on the venue — which coins exist, with limits and precision |
| `fetchOHLCV(symbol, timeframe)` | Candles — the trend and volatility inputs |

Symbols are `BASE/QUOTE`, and perps add the settle currency:
`BTC/USDT:USDT` on USDT-margined venues, `HYPE/USDC:USDC` on
Hyperliquid — Hyperliquid is USDC-margined, so `HYPE/USDT:USDT`
does not exist there and raises `BadSymbol`.

### The proxy quirk on this VM (verified live 2026-11-16)

CCXT ignores the `HTTPS_PROXY` environment variable — it sets
`trust_env = False` on its session. Without an explicit proxy, its
requests go direct and fail. Pass the proxy in the constructor,
read from the environment at runtime, never hard-coded:

```python
import os, ccxt
ex = ccxt.hyperliquid({
    'enableRateLimit': True,
    'https_proxy': os.environ['HTTPS_PROXY'],
})
```

### Which venues work through CCXT on this VM (verified live 2026-11-16)

| Venue | Through CCXT on this VM |
|---|---|
| Hyperliquid | **Works end to end.** `load_markets()` → 871 markets; ticker and funding rate fetched live. |
| Lighter | **Fails** at `load_markets()` — `CERTIFICATE_VERIFY_FAILED`, sync and async. |
| OKX | **Fails** at `load_markets()` — `CERTIFICATE_VERIFY_FAILED`, sync and async. |

The failures are the egress proxy's TLS interception: its CA is not
in the `certifi` bundle CCXT forces per request, and CCXT collapses
any custom `verify` value to a boolean, so pointing it at a CA bundle
does not work. Raw `urllib` / `requests` / `curl` reach both venues
fine — they use the system CA store. **Do not "fix" this by turning
verification off** (`validateServerSsl = False`) — that disables TLS
checks on a trading library.

The rule, then: keep the existing raw-HTTP calls for Lighter and
OKX; use CCXT where it works. `scripts/ccxt_funding.py` is the
example — it reads current funding APR on Hyperliquid only, and
says so in its output. Binance and Bybit are geo-blocked from this
VM either way (451/403) — CCXT does not bypass that. On a normal
network CCXT works for all three venues; this split is specific to
this VM's proxy, not a CCXT bug.

Live execution still needs §14's three requirements (funded accounts
on ≥ 2 venues, API access, the user's explicit per-action approval).
CCXT changes how data is read, not what is authorized.

## 16. Unified execution cycle — one cycle, many venues

Strategy E's cycle (§13) works for any trade that needs two venues,
not just Strategy E. This section pulls it out so it stands on its
own. Same steps, same discipline — no new numbers, no new thresholds.

When one trade needs two venues, you do not run two trades. You run
one cycle:

1. Load state — open positions, cooldown, history. If the state file
   is missing or corrupt, stop. Never trade from memory.
2. Fetch — funding and prices on every venue, same hour, same coins.
3. Reconcile — compare what the venues say you hold with what your
   state says. A leg the venue shows but the state does not is an
   orphan: close it or record it before you open anything new. A leg
   the state shows but the venue does not is drift: trust the venue,
   fix the state.
4. Score closes — every open position, in priority order (hard stop →
   max hold → smart negatives → low APR).
5. Score opens — only what passes every filter, at most 3 new opens
   per cycle.
6. Bracket — stop and take-profit for every planned open, written
   before the plan is approved.
7. Plan — one page: what closes, what opens, sizes, legs, lock class,
   weakest leg, kill condition.
8. Wait — nothing executes until the user says yes. A plan without a
   yes is a draft.
9. Execute closes first, then opens. If an open leg fails, close the
   other leg at once. Never leave a one-legged trade.
10. Persist — write the new state before the cycle ends. A trade that
    is not written down did not happen.

State lives in the same three files §13 names (positions, cooldown,
history). One place holds the credentials — never in the repo, never
in chat. One view shows balances across venues. One signing flow per
venue.

None of this runs live on its own. §14's three requirements still
gate every live run: funded accounts on at least two venues, API
access, and the user's explicit per-action approval for each exact
plan.

## Sources

- Hyperliquid docs: funding mechanics, Info API
  (hyperliquid.gitbook.io/hyperliquid-docs)
- Lighter public API: funding-rates (multi-venue funding in one
  call), fundings history, markPriceCandles (500-row cap per call —
  chunk 20d). Working host: mainnet.zklighter.elliot.ai
  (api.lighter.xyz is unreachable from this VM).
- Aster public API: premiumIndex (bulk funding). OKX public API:
  funding-rate (candidates only). Binance/Bybit direct APIs are
  geo-blocked from this VM (451/403); their funding rates are read
  via Lighter's funding-rates endpoint instead.

The strategy parameters, the decision-cycle procedure, the scorer,
and the backtests are this skill's own work. No third-party product
is named or endorsed in this repo.
