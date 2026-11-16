# Perpetual Arbitrage Skill

A trading skill for **perpetual futures arbitrage on Hyperliquid** — with a
runnable backtest, a parameter sweep, and honest numbers. Paper/backtest
only. **Not financial advice.**

## What it covers

| Strategy | Status |
|---|---|
| **A. Funding harvest (long-only)** | ✅ Validated — the default |
| B. Basis / cash-and-carry (two-leg) | Blueprint with honest limits |
| C. Cross-exchange funding spread | Screen-first, not backtested |
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

### Honest finding

Across all 166 trades, funding income was only **$0.10–$0.25 per coin**.
The PnL came from price moves. Extreme funding is a 1-hour spike that
normalizes right after entry — at 1x this is a **positioning /
mean-reversion signal**, not a carry harvest. Real funding harvest needs
leverage or longer holds, both of which add risk.

## Use as an agent skill

Copy this folder into your agent's skills directory (e.g. Muse
`~/workspace/custom-skills/` or `~/.agents/skills/`), or read `SKILL.md`
directly — it contains the full workflow, data sources, and rules.

## Run the backtest

```bash
python3 scripts/backtest_funding.py --days 208 --coins BTC ETH HYPE --out results/backtest-YYYY-MM-DD.json
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
