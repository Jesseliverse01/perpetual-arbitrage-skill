# Perpetual Arbitrage Skill

A trading skill for **perpetual futures arbitrage on Hyperliquid** — with a
runnable backtest, a parameter sweep, a 0-10 signal scorer, a live scanner,
and a cross-venue funding-spread matcher. Paper/backtest only.
**Not financial advice.**

## What it covers

| Strategy | Status |
|---|---|
| **A. Funding harvest (long-only)** | ✅ Validated — the default |
| B. Basis / cash-and-carry (two-leg) | Blueprint with honest limits |
| C. Cross-exchange funding spread | ✅ Detection implemented (`scripts/cross_venue.py`), execution not backtested |
| D. Funding squeeze | Experimental (backtest was negative) |

**Strategy A — the validated setup:** long a perp when funding APR is
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

## v0.3 — what's new

v0.2 was a validated entry rule plus a backtest. v0.3 adds the evaluation
discipline around the rule, borrowed from three reference repos
(TauricResearch's TradingAgents — prioritized — plus the Event-Driven
Arbitrage Scanner and CloddsBot; see `SKILL.md` Sources):

1. **Staged workflow** — Scan → Verify → Score → Debate → Decide →
   Report, with one JSON + one short markdown summary per scan in
   `results/`.
2. **Multi-agent evaluation pipeline** (procedure, not code) — Funding,
   Market and Liquidity Analysts in parallel → Bull/Bear debate →
   Risk Manager verdict (**GO / NO-GO / REDUCE**) → decision log.
3. **0-10 signal scorer** (`scripts/score.py`) — funding extremity
   30%, persistence 20%, trend 15%, liquidity 15%, volatility 10%,
   cross-venue spread 10%. No lookahead. A liquidity gate caps signals
   on untradeable books at 4.0.
4. **Live scanner** (`scripts/scan.py`) — scores every Hyperliquid perp
   below -10% APR, ranked table, JSON + markdown report.
5. **Cross-venue matcher** (`scripts/cross_venue.py`) — CloddsBot-style
   normalized-symbol matching across Hyperliquid vs OKX (Binance/Bybit
   best-effort; both geo-blocked from this VM), spread in pp, > 10pp
   flagged. Detection only.
6. **Scored backtest** (`--min-score` on `scripts/backtest_funding.py`)
   — honest result below: the score gate does **not** improve total PnL.
   The unscored v0.2 setup stays the validated default.

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
  ($33.42 vs $43.52). The -10% default is the robust choice.
- **The short side hurts.** Every "both" combo underperformed its
  long-only twin. Long-only is the default.
- **-30% entry and late exits (-5%) are worse.** Worst combo: -$7.95.

### Scored backtest — honest result (v0.3)

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
```

Stdlib only (Python 3). No keys, no trading, no installs.

## Disclaimer

Educational material only — not financial advice, not a promise of returns.
Past backtest performance does not predict future results. Do not trade
real money with this without your own validation, risk management, and a
paper-trading phase first.

## License

MIT — see [LICENSE](LICENSE).
