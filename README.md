# Perpetual Arbitrage Skill

> **Disclaimer — please read this first.** This is not financial advice.
> It is a research tool for studying how funding rates behave
> and testing ideas on old data. Nothing here trades live — no orders,
> no wallet, no keys. A backtest is not a promise: past results do not
> predict what happens next. Funding rates can flip every hour, and a
> -4,000% APR is one hour annualized, not a year's return. You can lose
> money fast — liquidation, a venue going down, one leg of a two-leg
> trade filling while the other does not, slippage, and fees eating the
> whole edge are all real risks, on top of venue and smart-contract
> risk. No returns are guaranteed. Do your own research, paper-trade
> first, and never trade money you cannot afford to lose.

Version 0.5.1, 2026-11-16. Paper/backtest only.

## What this is

Perpetual futures ("perps") have a trick built in: every hour, one side
pays the other. That payment is the **funding rate**. When too many
people are short, shorts pay longs — and the rate goes deeply negative.
This skill watches those payments and asks one question: is there a
trade here?

It covers five ideas, all on public data, all paper/backtest only.

## What it covers

| Strategy | Status |
|---|---|
| **A. Funding harvest (long-only)** | ✅ Validated — the default |
| B. Basis / cash-and-carry (two-leg) | Blueprint with honest limits |
| C. Cross-exchange funding spread | ✅ Detection implemented (`scripts/cross_venue.py`), execution not backtested |
| D. Funding squeeze | Experimental (backtest was negative) |
| **E. Delta-neutral cross-venue funding harvest** | ✅ Backtested + live scanner (`scripts/backtest_strategy_e.py`, `scripts/multivenue_scan.py`) — paper only; live execution needs accounts + API access on ≥2 venues (Jesse has none set up) |

**Strategy A — the validated setup:** buy a perp when funding APR is
deeply negative (shorts pay longs), exit when funding flips back to ≥ 0.

| Rule | Value |
|---|---|
| Long entry | funding APR < **-10%** |
| Long exit | funding APR ≥ 0 |
| Stop-loss | **-5%** |
| Max hold | 24h (never binds — funding flips exit first) |
| Size | $100 notional, 1x per coin |
| Fees | 0.035% taker per side |
| Coins | BTC, ETH, HYPE |

## What's in this repo

| File | What it is |
|---|---|
| `SKILL.md` | The full skill: workflow, pipeline, scoring rubric, all five strategies, data sources, run commands, rules. Start here. |
| `README.md` | This file — the short version. |
| `LICENSE` | MIT. |
| `scripts/score.py` | Score one signal 0–10 (six weighted dimensions + liquidity gate). |
| `scripts/scan.py` | Live scan of all Hyperliquid perps, ranked table + JSON/markdown report. |
| `scripts/cross_venue.py` | Cross-venue funding-spread matcher (HL vs OKX, Binance/Bybit best-effort). Detection only. |
| `scripts/backtest_funding.py` | Strategy A backtest + `--min-score` scored variant. |
| `scripts/sweep.py` | 48-combo parameter sweep + out-of-sample validation. |
| `scripts/multivenue_scan.py` | Strategy E live scanner (HL + Lighter + Aster + OKX). |
| `scripts/backtest_strategy_e.py` | Strategy E backtest (HL vs Lighter). |
| `results/` | Every result file: backtests, sweeps, scans, frequency data. Nothing is deleted; old runs stay for the record. |

## Strategy E backtest — honest result

`results/backtest-strategy-e-2026-11-16-208d.json`, run with
`python3 scripts/backtest_strategy_e.py --days 208 --coins BTC ETH HYPE`.
Data: Hyperliquid fundingHistory + candles, Lighter fundings history
+ markPriceCandles (all public, no keys, no lookahead). Full data
existed for all three coins (4,985–4,993h of 4,992).

| Coin | Trades | Win | PnL | Funding | Price (basis) |
|---|---|---|---|---|---|
| BTC | 0 | — | $0 | $0 | $0 |
| ETH | 0 | — | $0 | $0 | $0 |
| HYPE | 5 | 80% | +$0.19 | +$0.14 | +$0.23 |
| **Combined** | **5** | **80%** | **+$0.19** | **+$0.14** | **+$0.23** |

Exits: 4× low_apr_6, 1× smart_decl_6; no stop-losses, no max-holds.
Holds 13–45h. **On the three majors the 70% threshold almost never
triggers** — mean net APR is only ~5% (p99 22–54%); BTC's max spread
in 208 days was 111.7% for a single hour, ETH never reached 70% (max
67.2%). **+$0.19 in 208 days is not a return stream, it is proof the
machinery works.** The strategy's real opportunity set is the full
200+ coin universe (the live scan below found 9 in-band candidates
across all coins, vs 0 on the three majors). Do NOT lower the 70%
threshold to manufacture trades on majors — friction dominates below
it. Backtest limits: volume gates not applied (no historical volume),
slippage not modeled, hard stop evaluated at hour closes only,
Lighter funding missing ~7 of 4,992 hours. Capital base differs from
Strategy A ($20 margin per arb) — compare PnL in USD, not raw
return_pct.

### Live multi-venue scan — first run (2026-11-16 ~10:00 UTC)

`results/multivenue-scan-2026-11-16-1000.json` — all four direct
venues OK (HL 235 coins; Lighter endpoint 231 coins across 4 venues;
Aster 750; OKX 22 matched). 285 coins on ≥ 2 venues, 31 candidates
with net APR ≥ 50%, **0 PASS**. Top 5 spreads are all SKIP (above
the 200% hard cap): SKR +1,873.4%, BIRB +773.8%, PRL +493.9%, SAGA
+434.2%, BOT +389.4% — spikes on illiquid books, exactly what the
cap exists to reject. In-band (70–120%), all WATCH: HYUNDAI 117.4%,
LIT 93.6%, AZTEC 91.5% (blacklisted), RESOLV 82.8%, W 77.9%, CC
77.2% (blacklisted), SOPH 74.5%, ARC 73.3%, GMX 73.0% (volume gate).
None is PASS — the spike filters need spread history, only
computable for HL↔Lighter pairs, and none of these nine has its best
pair there.

## Backtest results — 208 days (2026-04-22 → 2026-11-16)

`results/backtest-2026-11-16-208d.json`, run with
`python3 scripts/backtest_funding.py --days 208 --coins BTC ETH HYPE`.
Data: Hyperliquid public API only (`fundingHistory` + `candleSnapshot`),
no keys, no lookahead.

| Coin | Trades | Win | PnL | Return | Max DD | Buy & hold |
|---|---|---|---|---|---|---|
| BTC | 31 | 51.6% | +$2.69 | +2.69% | 2.61% | -17.6% |
| ETH | 36 | 55.6% | +$11.97 | +11.97% | 7.61% | -24.8% |
| HYPE | 99 | 57.6% | +$28.86 | +28.86% | 4.19% | +63.6% |
| **Combined** | **166** | — | **+$43.52** | **+14.51% on $300** | — | — |

The strategy beat buy-and-hold on BTC and ETH, but lagged HYPE's +63.6%
rally — it is out of the market most of the time, so it wins in
chop/downtrends and lags strong uptrends.

### What the sweep proved

`scripts/sweep.py` tested 48 parameter combos on window A (120d) and
validated the top 3 on window B (88d, out-of-sample):

- **Stricter entry (-20%) is overfitting.** Better on A (+$8.81 vs +$8.29)
  but worse out-of-sample (B: +$24.61 vs +$35.23) and combined
  ($33.42 vs $43.52). The -10% default is the safer choice.
- **The short side hurts.** Every "both" combo underperformed its
  long-only twin. Long-only is the default.
- **-30% entry and late exits (-5%) are worse.** Worst combo: -$7.95.

### Scored backtest — honest result

`--min-score` gates long entries on the point-in-time 0-10 score
(4 dimensions available historically: extremity, persistence, trend,
volatility — liquidity and cross-venue dropped, weights renormalized).
Same 208-day window:

| Variant | Trades | PnL | Win | Signals seen | Filtered |
|---|---|---|---|---|---|
| **Baseline (unscored, v0.2)** | **166** | **+$43.52** | **56%** | — | — |
| min-score 2 | 158 | +$44.46 | 57% | 179 | 21 |
| min-score 3 | 100 | +$20.20 | 55% | 223 | 123 |
| min-score 6 | 2 | +$8.00 | 100% | 385 | 383 |

**The scorer does NOT improve total PnL.** Min-score 2 is +$0.94 above
baseline — noise, and the threshold was picked after looking at the
results. Min-score 3 is worse; min-score 6 barely trades at all.
Why: entries fire on the *first* hour below -10%, so at entry,
persistence is mechanically low and extremity is usually just past the
threshold — the mean entry score across the 166 baseline trades is
3.03 (median 2.93). A threshold of 6 was miscalibrated. Per-trade
quality does rise with score (win rate 56% → 72% at score ≥ 4), but
not enough to beat the baseline on total return. **The unscored v0.2
setup stays the default.** The scorer is a ranking/evaluation tool for
the live scan, not a validated entry filter.

### Honest finding

Across all 166 trades, funding income was only **$0.10–$0.25 per coin**.
The PnL came from price moves. Extreme funding is a 1-hour spike that
normalizes right after entry — at 1x this is a **positioning /
mean-reversion signal**, not a carry harvest. Real funding harvest needs
leverage or longer holds, both of which add risk.

## Which coins actually signal

Thirty days of hourly funding (2026-10-17 → 2026-11-16, 720 hours per
coin), from `results/funding-frequency-2026-11-16.json` — % of hours
below -10% APR:

SOPH 40.8%, NIL 38.2%, INIT 33.3%, MELANIA 30.3%, RUNE 18.1%,
CC 15.8%, BIGTIME 11.4%, DOGE 11.4%, XMR 7.8%, IMX 7.4%, ETH 5.3%,
SOL 5.1%, HYPE 3.3%, BTC 0.3%. SAGA, RESOLV, PONS: 0%. SKR: only
1.1% despite huge spikes (lowest hour -3,515.8%).

**Often does not mean profitable.** SOPH, NIL, INIT, MELANIA are
small, illiquid coins — thin books, wide spreads, violent price moves.
They were never backtested; only BTC/ETH/HYPE were validated. A coin
below -10% forty percent of the time is often a coin in a structural
downtrend — the shorts are right. The usable middle is RUNE, DOGE,
ETH, SOL, HYPE: liquid enough to actually trade, and they signal
often enough to matter. See `SKILL.md` §9 for the full table and the
warning.

## Live scan — first run (2026-11-16 ~10:00 UTC)

`results/scan-2026-11-16-1000.{json,md}` — 235 perps scanned, 26 below
-10% APR, 29 scored, 2 PASS: **TRUMP 6.33** and **WLD 6.12**. Next:
XMR 5.93, CC 5.86, SKR 5.84 (all WATCH). The known illiquid traps
(RUNE, NIL, MELANIA, BIGTIME, …) were capped at 4.00 by the liquidity
gate and did not top the ranking. PASS means "worth the full
evaluation", not "buy now" — both PASS coins have thin liquidity
dimensions and would face a hard liquidity question in debate.

Cross-venue run same day (`results/cross-venue-2026-11-16-1006.json`):
34 coins checked, 26 OKX matches, **18 candidates** (|spread| > 10pp).
Top: CASHCAT +146.6pp, RESOLV -91.8pp, TRUMP +75.2pp, W +71.0pp.
Binance (451) and Bybit (403) geo-blocked from this VM — skipped
gracefully, no fake rows.

## Use as an agent skill

Copy this folder into your agent's skills directory (e.g. Muse
`~/workspace/custom-skills/` or `~/.agents/skills/`), or read `SKILL.md`
directly — it contains the full workflow, the multi-agent pipeline, the
scoring rubric, the cross-venue rules, the report format, data sources,
and rules.

## Run the scripts

```bash
python3 scripts/scan.py                     # live scan -> results/scan-YYYY-MM-DD-HHMM.{json,md}
python3 scripts/cross_venue.py              # cross-venue matcher -> results/cross-venue-YYYY-MM-DD-HHMM.json
python3 scripts/score.py --apr=-0.183 --apr-history=-0.05,-0.08,... \
    --closes=1790,1795,... --volume=1200000000 --oi=350000000 \
    --other-apr=-0.059                      # score one signal (JSON out)

python3 scripts/backtest_funding.py --days 208 --coins BTC ETH HYPE --out results/backtest-YYYY-MM-DD.json
python3 scripts/backtest_funding.py --days 208 --coins BTC ETH HYPE --min-score 6 --out results/scored.json
python3 scripts/sweep.py --out results/sweep-YYYY-MM-DD.json

python3 scripts/multivenue_scan.py              # Strategy E live scanner -> results/multivenue-scan-YYYY-MM-DD-HHMM.json
python3 scripts/backtest_strategy_e.py --days 208 --coins BTC ETH HYPE --out results/backtest-strategy-e-YYYY-MM-DD.json
```

Stdlib only (Python 3). No keys, no trading, no installs.

## License

MIT — see [LICENSE](LICENSE).
