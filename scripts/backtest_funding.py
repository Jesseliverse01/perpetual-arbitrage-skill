#!/usr/bin/env python3
"""Backtest the funding-harvest strategy (Strategy A of the perpetual-arbitrage
skill) on Hyperliquid perps, using only the public Info API (no key needed).

Validated "gacor" defaults (v0.2, 208-day validation 2026-04-22 -> 2026-11-16,
BTC/ETH/HYPE, $100 notional 1x, 166 trades, +$43.52 combined):
  - Long-only. ENTER long when funding APR < -10% (shorts paying longs).
  - EXIT when funding APR >= 0, or stop-loss -5% on price, or 24h timeout
    (timeout never binds in practice — funding flips exit first).
  - The short side (APR > +30%) and stricter entries (-20%/-30%) were tested
    in scripts/sweep.py and UNDERPERFORMED out-of-sample — do not use them
    as defaults.

Funding APR = hourly funding rate * 24 * 365. While in a position, each hour
accrues funding = -funding_rate * notional (long). Fees: 0.035% taker/side.

Data: fundingHistory (500 rows/page, paginated) + candleSnapshot 1h
(max ~5,000 candles, ~208 days). Funding row `time` is the payment time at
the END of its hour, so a row with time in (candle_open, candle_close] is
the payment for holding that hour. Decisions use the funding rate of the
hour just closed — no lookahead.

Usage:
  python3 backtest_funding.py [--days 120] [--end 2026-11-16]
      [--coins BTC ETH HYPE] [--entry-apr -0.10] [--exit-apr 0.0]
      [--stop 0.05] [--timeout 24] [--side long] [--out results.json]
      [--min-score 6]

  --min-score N (v0.3): gate long entries on the point-in-time 0-10 score
      from scripts/score.py. At each entry decision the score is computed
      from data up to that hour ONLY (current APR, last 24h of funding,
      last 25 hourly closes + highs/lows) — no lookahead. Only the 4
      dimensions computable from Hyperliquid history are used (funding
      extremity, persistence, trend, volatility); liquidity and
      cross-venue need data that does not exist historically, so they are
      dropped and the remaining weights renormalize (see score.py).
      --min-score is unset by default: the unscored v0.2 setup is the
      validated default, and the scored variant is an experiment to be
      judged on its own numbers.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import score_signal  # noqa: E402

API = "https://api.hyperliquid.xyz/info"
HOUR = 3600_000
TAKER_FEE = 0.00035  # 0.035% per side


def post(payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_funding(coin, start_ms, end_ms):
    """All hourly funding rows in [start_ms, end_ms). Paginates (500/page)."""
    rows, cur = [], start_ms
    while True:
        page = post({"type": "fundingHistory", "coin": coin,
                     "startTime": cur, "endTime": end_ms})
        if not page:
            break
        rows.extend(page)
        last_t = int(page[-1]["time"])
        if len(page) < 500 or last_t <= cur:
            break
        cur = last_t + 1
        if len(rows) > 20000:
            break
    return rows


def fetch_candles(coin, start_ms, end_ms):
    data = post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": start_ms, "endTime": end_ms}})
    out = []
    for c in data:
        out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                    "l": float(c["l"]), "c": float(c["c"])})
    out.sort(key=lambda x: x["t"])
    return out


def simulate(coin, candles, funding_by_close, notional=100.0,
             entry_apr=-0.10, exit_apr=0.0, stop_pct=0.05, timeout_h=24,
             side="long", short_entry_apr=0.30, min_score=None):
    trades, equity, peak, max_dd = [], notional, notional, 0.0
    pos = None
    signals_seen, signals_taken, signals_filtered = 0, 0, 0
    # Point-in-time history, appended hour by hour as the loop advances.
    # At any decision, these contain only hours up to and including the
    # current one — nothing from the future ever enters a score.
    apr_hist, close_hist, high_hist, low_hist = [], [], [], []
    for cd in candles:
        t, close = cd["t"], cd["c"]
        rate = funding_by_close.get(t + HOUR)
        if rate is None:
            continue
        apr = rate * 24 * 365
        apr_hist.append(apr)
        close_hist.append(close)
        high_hist.append(cd["h"])
        low_hist.append(cd["l"])
        if pos is None:
            if side in ("long", "both") and apr < entry_apr:
                entry_score = None
                if min_score is not None:
                    # Score gate (long entries only): 4 dimensions that
                    # Hyperliquid history can supply. Liquidity and
                    # cross-venue are dropped and weights renormalize.
                    res = score_signal({
                        "apr": apr,
                        "apr_history": apr_hist[-24:],
                        "closes": close_hist[-25:],
                        "highs": high_hist[-24:],
                        "lows": low_hist[-24:],
                    })
                    entry_score = res["score"]
                    signals_seen += 1
                    if entry_score < min_score:
                        signals_filtered += 1
                        continue
                    signals_taken += 1
                pos = {"side": 1, "entry": close, "t0": t + HOUR,
                       "funding": 0.0, "hours": 0, "entry_score": entry_score}
            elif side in ("short", "both") and apr > short_entry_apr:
                pos = {"side": -1, "entry": close, "t0": t + HOUR,
                       "funding": 0.0, "hours": 0, "entry_score": None}
            continue
        s = pos["side"]
        pos["funding"] += -s * rate * notional
        pos["hours"] += 1
        exit_price, reason = None, None
        if s == 1 and cd["l"] <= pos["entry"] * (1 - stop_pct):
            exit_price, reason = pos["entry"] * (1 - stop_pct), "stoploss"
        elif s == -1 and cd["h"] >= pos["entry"] * (1 + stop_pct):
            exit_price, reason = pos["entry"] * (1 + stop_pct), "stoploss"
        elif s == 1 and apr >= exit_apr:
            exit_price, reason = close, "funding_flip"
        elif s == -1 and apr <= -exit_apr:
            exit_price, reason = close, "funding_flip"
        elif pos["hours"] >= timeout_h:
            exit_price, reason = close, "timeout"
        if exit_price is None:
            continue
        price_pnl = s * (exit_price - pos["entry"]) / pos["entry"] * notional
        fees = 2 * TAKER_FEE * notional
        pnl = price_pnl + pos["funding"] - fees
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        trades.append({
            "coin": coin, "side": "long" if s == 1 else "short",
            "entry_time": pos["t0"], "exit_time": t + HOUR,
            "entry_price": pos["entry"], "exit_price": exit_price,
            "entry_score": pos.get("entry_score"),
            "hours": pos["hours"], "reason": reason,
            "price_pnl": round(price_pnl, 4), "funding_pnl": round(pos["funding"], 4),
            "fees": round(fees, 4), "pnl": round(pnl, 4),
        })
        pos = None
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    rets = [t["pnl"] / notional for t in trades]
    sharpe = None
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var > 0:
            sharpe = mean / math.sqrt(var) * math.sqrt(len(rets))
    return {
        "coin": coin, "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "total_pnl_usd": round(total, 2),
        "return_pct": round(total / notional * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "sharpe_per_trade": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_hours": round(sum(t["hours"] for t in trades) / len(trades), 1) if trades else None,
        "funding_pnl_usd": round(sum(t["funding_pnl"] for t in trades), 2),
        "price_pnl_usd": round(sum(t["price_pnl"] for t in trades), 2),
        "exit_reasons": {r: sum(1 for t in trades if t["reason"] == r)
                         for r in ("funding_flip", "stoploss", "timeout")},
        "signals_seen": signals_seen,
        "signals_taken": signals_taken,
        "signals_filtered": signals_filtered,
        "avg_entry_score": (round(sum(t["entry_score"] for t in trades
                                     if t["entry_score"] is not None)
                                / max(1, sum(1 for t in trades
                                             if t["entry_score"] is not None)), 2)
                            if trades and any(t["entry_score"] is not None
                                              for t in trades) else None),
        "trade_list": trades,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "HYPE"])
    ap.add_argument("--entry-apr", type=float, default=-0.10)
    ap.add_argument("--exit-apr", type=float, default=0.0)
    ap.add_argument("--stop", type=float, default=0.05)
    ap.add_argument("--timeout", type=int, default=24)
    ap.add_argument("--side", default="long", choices=["long", "short", "both"])
    ap.add_argument("--min-score", type=float, default=None,
                    help="gate long entries on the point-in-time 0-10 score "
                         "(v0.3, default: unset = unscored v0.2 behavior)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.end:
        end_ms = int(datetime.strptime(args.end, "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        end_ms = int(time.time() * 1000)
    end_ms = (end_ms // HOUR) * HOUR
    start_ms = end_ms - args.days * 24 * HOUR
    params = {"entry_apr": args.entry_apr, "exit_apr": args.exit_apr,
              "stop_pct": args.stop, "timeout_h": args.timeout,
              "side": args.side, "min_score": args.min_score}
    results = {"window_days": args.days,
               "start": time.strftime("%Y-%m-%d", time.gmtime(start_ms / 1000)),
               "end": time.strftime("%Y-%m-%d", time.gmtime(end_ms / 1000)),
               "notional_usd": 100.0, "leverage": "1x",
               "taker_fee_per_side": TAKER_FEE, "params": params, "pairs": {}}
    if args.min_score is not None:
        results["scoring"] = {
            "min_score": args.min_score,
            "dimensions_used": ["funding_extremity", "persistence",
                                "trend_alignment", "volatility"],
            "dimensions_dropped": ["liquidity", "cross_venue_spread"],
            "note": "Score computed point-in-time at each entry decision "
                    "from data up to that hour only (no lookahead). "
                    "Liquidity and cross-venue need data that does not "
                    "exist historically, so they are dropped and the "
                    "remaining weights renormalize (see scripts/score.py).",
        }
    for coin in args.coins:
        print(f"[{coin}] fetching funding + candles ...", flush=True)
        funding = fetch_funding(coin, start_ms, end_ms)
        candles = fetch_candles(coin, start_ms, end_ms)
        print(f"[{coin}] {len(funding)} funding rows, {len(candles)} candles",
              flush=True)
        by_close = {}
        for row in funding:
            ts = int(row["time"])
            by_close[(ts // HOUR) * HOUR] = float(row["fundingRate"])
        res = simulate(coin, candles, by_close, **params)
        if candles:
            res["buy_and_hold_pct"] = round(
                (candles[-1]["c"] / candles[0]["o"] - 1) * 100, 2)
        results["pairs"][coin] = res
        print(f"[{coin}] trades={res['trades']} pnl=${res['total_pnl_usd']} "
              f"return={res['return_pct']}% win={res['win_rate']} "
              f"signals_seen={res['signals_seen']} "
              f"filtered={res['signals_filtered']}", flush=True)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)
        print("wrote", args.out)
    else:
        print(json.dumps({k: (v if k != "pairs" else
                         {c: {kk: vv for kk, vv in d.items() if kk != "trade_list"}
                          for c, d in v.items()})
                          for k, v in results.items()}, indent=2))


if __name__ == "__main__":
    main()
