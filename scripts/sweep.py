#!/usr/bin/env python3
"""Parameter sweep for the perpetual-arbitrage funding-harvest strategy.

Tune on window A (2026-07-19 -> 2026-11-16, 120d — same window as the
first backtest), then validate the top-3 parameter sets on window B
(2026-04-22 -> 2026-07-19, 88d, out-of-sample). 1h candles only go back
~208 days on Hyperliquid's public API, so B is as far back as we can go.

Grid: long entry APR {-10%, -20%, -30%}, exit APR {0%, -5%},
stop {5%, 3%}, timeout {24h, 48h}, side {long, both}.
Short entry is fixed at APR > +30%, short exit mirrors the long exit.

Ranking: combined PnL across BTC/ETH/HYPE, minimum 15 trades combined.
Overfitting warning: the top set is the best OF THE GRID on window A —
always check window B before trusting it.

2026-11-17 audit fix: funding for the exit hour is no longer accrued on
stop-loss exits (same fix as backtest_funding.py). The stored sweep
results predate this fix; the affected amounts are cents per stop trade,
so the ranking conclusion is unchanged.

Usage:
  python3 sweep.py [--out results/sweep-YYYY-MM-DD.json]
"""
import itertools
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_funding import fetch_candles, fetch_funding, HOUR, TAKER_FEE

COINS = ["BTC", "ETH", "HYPE"]
NOTIONAL = 100.0
END_MS = (int(time.time() * 1000) // HOUR) * HOUR
START_MS = END_MS - 208 * 24 * HOUR  # ~2026-04-22, max 1h-candle lookback
SPLIT_MS = int(time.mktime(time.strptime("2026-07-19", "%Y-%m-%d")) * 1000)


def load_data():
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "results", "sweep-data.json")
    if os.path.exists(cache):
        print("using cached data:", cache, flush=True)
        return json.load(open(cache))
    data = {}
    for coin in COINS:
        print(f"[{coin}] fetching funding + candles ...", flush=True)
        funding = fetch_funding(coin, START_MS, END_MS)
        candles = fetch_candles(coin, START_MS, END_MS)
        print(f"[{coin}] {len(funding)} funding rows, {len(candles)} candles",
              flush=True)
        by_close = {}
        for row in funding:
            ts = int(row["time"])
            by_close[(ts // HOUR) * HOUR] = float(row["fundingRate"])
        data[coin] = {"candles": candles, "funding": by_close}
    json.dump(data, open(cache, "w"))
    return data


def simulate(candles, funding_by_close, entry_apr, exit_apr, stop_pct,
             timeout_h, side, short_entry_apr=0.30):
    trades, equity, peak, max_dd = [], NOTIONAL, NOTIONAL, 0.0
    pos = None
    for cd in candles:
        t, close = cd["t"], cd["c"]
        rate = funding_by_close.get(t + HOUR)
        if rate is None:
            continue
        apr = rate * 24 * 365
        if pos is None:
            if side in ("long", "both") and apr < entry_apr:
                pos = {"side": 1, "entry": close, "t0": t + HOUR,
                       "funding": 0.0, "hours": 0}
            elif side in ("short", "both") and apr > short_entry_apr:
                pos = {"side": -1, "entry": close, "t0": t + HOUR,
                       "funding": 0.0, "hours": 0}
            continue
        s = pos["side"]
        exit_price, reason = None, None
        # Stop first: it fires intrabar, before the hour-end funding
        # payment, so a stop exit collects no funding for this hour
        # (same 2026-11-17 audit fix as backtest_funding.py).
        if s == 1 and cd["l"] <= pos["entry"] * (1 - stop_pct):
            exit_price, reason = pos["entry"] * (1 - stop_pct), "stoploss"
        elif s == -1 and cd["h"] >= pos["entry"] * (1 + stop_pct):
            exit_price, reason = pos["entry"] * (1 + stop_pct), "stoploss"
        if exit_price is None:
            pos["funding"] += -s * rate * NOTIONAL
            pos["hours"] += 1
            if s == 1 and apr >= exit_apr:
                exit_price, reason = close, "funding_flip"
            elif s == -1 and apr <= -exit_apr:
                exit_price, reason = close, "funding_flip"
            elif pos["hours"] >= timeout_h:
                exit_price, reason = close, "timeout"
        if exit_price is None:
            continue
        price_pnl = s * (exit_price - pos["entry"]) / pos["entry"] * NOTIONAL
        pnl = price_pnl + pos["funding"] - 2 * TAKER_FEE * NOTIONAL
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        trades.append({"pnl": round(pnl, 4), "funding": round(pos["funding"], 4),
                       "hours": pos["hours"], "reason": reason,
                       "side": "long" if s == 1 else "short"})
        pos = None
    wins = [t for t in trades if t["pnl"] > 0]
    total = sum(t["pnl"] for t in trades)
    return {"trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            "pnl": round(total, 2),
            "max_dd_pct": round(max_dd * 100, 2),
            "funding_pnl": round(sum(t["funding"] for t in trades), 2)}


def run_window(data, params, t0, t1):
    per_coin, tot_pnl, tot_trades = {}, 0.0, 0
    for coin in COINS:
        candles = [c for c in data[coin]["candles"] if t0 <= c["t"] < t1]
        funding = {k: v for k, v in data[coin]["funding"].items()
                   if t0 <= int(k) < t1 + HOUR}
        funding = {int(k): v for k, v in funding.items()}
        r = simulate(candles, funding, **params)
        per_coin[coin] = r
        tot_pnl += r["pnl"]
        tot_trades += r["trades"]
    wins = None
    return {"params": params, "combined_pnl": round(tot_pnl, 2),
            "combined_trades": tot_trades, "per_coin": per_coin}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data = load_data()
    grid = list(itertools.product(
        [-0.10, -0.20, -0.30],   # entry_apr
        [0.0, -0.05],            # exit_apr
        [0.05, 0.03],            # stop_pct
        [24, 48],                # timeout_h
        ["long", "both"],        # side
    ))
    print(f"grid: {len(grid)} combos x {len(COINS)} coins, window A "
          f"(2026-07-19 -> 2026-11-16)", flush=True)
    results_a = []
    for entry_apr, exit_apr, stop_pct, timeout_h, side in grid:
        params = {"entry_apr": entry_apr, "exit_apr": exit_apr,
                  "stop_pct": stop_pct, "timeout_h": timeout_h, "side": side}
        r = run_window(data, params, SPLIT_MS, END_MS)
        if r["combined_trades"] >= 15:
            results_a.append(r)
    results_a.sort(key=lambda r: r["combined_pnl"], reverse=True)
    print("\nTOP 5 on window A (tune):")
    for r in results_a[:5]:
        p = r["params"]
        print(f"  pnl=${r['combined_pnl']} trades={r['combined_trades']} "
              f"entry={p['entry_apr']*100:.0f}% exit={p['exit_apr']*100:.0f}% "
              f"stop={p['stop_pct']*100:.0f}% timeout={p['timeout_h']}h "
              f"side={p['side']} | "
              + " ".join(f"{c}:{d['pnl']:+.2f}({d['trades']}t)"
                         for c, d in r["per_coin"].items()))
    print("\nValidating top 3 on window B (2026-04-22 -> 2026-07-19, "
          "out-of-sample):")
    validations = []
    for r in results_a[:3]:
        v = run_window(data, r["params"], START_MS, SPLIT_MS)
        validations.append(v)
        p = r["params"]
        print(f"  pnl=${v['combined_pnl']} trades={v['combined_trades']} "
              f"entry={p['entry_apr']*100:.0f}% exit={p['exit_apr']*100:.0f}% "
              f"stop={p['stop_pct']*100:.0f}% timeout={p['timeout_h']}h "
              f"side={p['side']} | "
              + " ".join(f"{c}:{d['pnl']:+.2f}({d['trades']}t)"
                         for c, d in v["per_coin"].items()))
    if args.out:
        json.dump({"window_a": results_a, "window_b_top3": validations},
                  open(args.out, "w"), indent=2)
        print("wrote", args.out)


if __name__ == "__main__":
    main()
