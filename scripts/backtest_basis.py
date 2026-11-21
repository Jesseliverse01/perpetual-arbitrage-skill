#!/usr/bin/env python3
"""Backtest Strategy B (basis / cash-and-carry) of the perpetual-arbitrage
skill as a real hedged two-leg trade on Hyperliquid, using only the public
Info API (no key needed).

The trade: long perp + short spot when the perp trades at a discount to
spot (basis < 0), or short perp + long spot when the perp trades at a
premium (basis > 0). Same coin, same notional on both legs, so price moves
cancel and what is left is the basis converging toward zero, plus funding
on the perp leg. Basis = (perp_mark - spot_mid) / spot_mid, hourly.

Costs modeled on every trade (all four lines, before the edge counts):
  - taker fees: 0.035% per fill, 4 fills per round trip (entry + exit,
    both legs) = 0.14% of notional = $0.14 on $100.
  - funding: accrued hourly on the perp leg, signed. Positive funding
    rate means longs pay shorts, so a long perp leg pays and a short
    perp leg earns.
  - borrow: the long-perp + short-spot direction needs borrowed spot
    assets (Hyperliquid spot is cash-only and cannot be shorted
    directly). Borrow cost = --borrow-apr / 8760 per hour on the spot
    notional. Default 5% APR. This direction is modeled, not solved:
    borrow is a LIVE blocker, see below.
  - slippage: NOT modeled. No historical order-book data exists in the
    public API, so fills use hour-close prices. Live fills would be
    worse. Treat every PnL number here as an upper bound.

Defaults (justified from the data, not tuned): entry |basis| > 20 bps.
Round-trip fees are 14 bps, and the 99th percentile of |basis| over the
window is 14-19 bps on BTC/ETH/SOL, so 20 bps is the smallest entry
that clears fees with margin and is still a genuine dislocation rather
than noise. Exit on convergence (|basis| <= 5 bps), a 72h timeout, or
an adverse stop (basis widens to 2x entry against the position).
A hard stop at -$1.00 net unrealized also applies.

Confirmation filter (--confirm-hours, default 2): the basis must exceed
the entry threshold on 2 consecutive hour closes, and the fill is at
the second close. This exists because the naive 1-close version
manufactured profits: spot closes are last-trade prices, and a single
hour can print a transient spike that reverts the next hour (the worst
case was a -100.6 bps BTC print on 2026-07-08 that vanished in one
hour and alone contributed +$0.90). Fills at one stray close are not
executable. The naive run (saved as
results/backtest-basis-2026-11-16-naive.json) showed +$5.66 on 53
trades with a 100% win rate and zero adverse exits, which is the
signature of harvesting close-price noise, not an edge. The confirmed
run is the honest number; the naive run is kept only to show why the
filter exists. Do not quote the naive total as a result.

Live feasibility, read this before quoting any number:
  - short-perp + long-spot (perp at a premium, ~70% of entry hours):
    feasible on Hyperliquid alone. Spot long is a cash buy, perp short
    is a normal short. Needs cash for the spot leg plus margin for the
    perp leg, about $200 capital per $100-notional trade.
  - long-perp + short-spot (perp at a discount): NOT feasible on
    Hyperliquid alone. Spot cannot be shorted without borrowing the
    asset on a lending venue, which this backtest prices but does not
    provide. Do not present this direction as runnable until a borrow
    source is wired up.
  - both directions need both legs filled at the same time. A fill on
    one leg without the other leaves a naked directional position.
    This backtest assumes simultaneous fills at hour closes.

Data: candleSnapshot 1h for the perp (coin BTC/ETH/SOL) and for the
spot pair (resolved at runtime from spotMeta: UBTC/USDC, UETH/USDC,
USOL/USDC), plus fundingHistory (500 rows/page). Spot history starts
2026-04-26, so with the default window the effective per-coin window
is ~203 days, not 208; the script reports the actual window per coin.
Decisions use data up to that hour only, no lookahead.

Standalone: stdlib only, no keys, no local state, no hardcoded paths.
Fetches go through curl (subprocess) so the script works behind an
authenticated egress proxy as well as on a plain connection. Run it
from a fresh clone of the repo:

  python3 scripts/backtest_basis.py [--days 208] [--end 2026-11-16]
      [--coins BTC ETH SOL] [--entry-bps 20] [--exit-bps 5]
      [--adverse-bps 40] [--timeout 72] [--borrow-apr 0.05]
      [--confirm-hours 2]
      [--notional 100] [--out results.json] [--cache-dir DIR]

  --cache-dir DIR: cache raw API responses under DIR so a re-run does
      not re-fetch. Optional; without it every run fetches fresh.
"""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"
HOUR = 3600_000
TAKER_FEE = 0.00035  # 0.035% per fill, per leg
SPOT_BASE_TOKEN = {"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL", "HYPE": "HYPE"}
QUOTE_TOKEN = "USDC"


def hl_post(payload, cache_dir=None):
    """One Info API call via curl. Optional on-disk cache keyed by payload."""
    raw = json.dumps(payload, sort_keys=True).encode()
    if cache_dir:
        key = hashlib.sha256(raw).hexdigest()[:16]
        path = os.path.join(cache_dir, key + ".json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    proc = subprocess.run(
        ["curl", "-s", "-m", "60", "-X", "POST", API,
         "-H", "Content-Type: application/json", "-d", raw.decode()],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed rc={proc.returncode}: {proc.stderr[:200]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"bad JSON from API (first 200 chars): {proc.stdout[:200]}")
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    return data


def resolve_spot_coin(coin, cache_dir=None):
    """Map a perp coin to its Hyperliquid spot pair id (e.g. BTC -> @142).

    Reads spotMeta at runtime instead of hardcoding pair indices.
    Returns None when no USDC spot pair exists for the coin.
    """
    base = SPOT_BASE_TOKEN.get(coin)
    if base is None:
        return None
    meta = hl_post({"type": "spotMeta"}, cache_dir)
    tokens = {t["index"]: t["name"] for t in meta["tokens"]}
    for pair in meta["universe"]:
        names = [tokens[i] for i in pair["tokens"]]
        if base in names and QUOTE_TOKEN in names:
            return "@" + str(pair["index"])
    return None


def fetch_candles(coin, start_ms, end_ms, cache_dir=None):
    """All 1h candles in [start_ms, end_ms). Paginates (~5000/call)."""
    out, cur = [], start_ms
    while True:
        data = hl_post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1h",
            "startTime": cur, "endTime": end_ms}}, cache_dir)
        if not data:
            break
        out.extend(data)
        last_t = int(data[-1]["t"])
        if len(data) < 5000 or last_t <= cur:
            break
        cur = last_t + 1
        if len(out) > 20000:
            break
    rows = [{"t": int(c["t"]), "c": float(c["c"])} for c in out]
    rows.sort(key=lambda x: x["t"])
    return rows


def fetch_funding(coin, start_ms, end_ms, cache_dir=None):
    """All hourly funding rows in [start_ms, end_ms). Paginates (500/page)."""
    rows, cur = [], start_ms
    while True:
        page = hl_post({"type": "fundingHistory", "coin": coin,
                        "startTime": cur, "endTime": end_ms}, cache_dir)
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


def simulate(coin, perp, spot, funding_by_close, notional=100.0,
             entry_bps=20.0, exit_bps=5.0, adverse_bps=40.0, timeout_h=72,
             borrow_apr=0.05, confirm_hours=2):
    """Hedged two-leg simulation on hour-close data. No lookahead: each
    hour's decision uses that hour's basis and funding only. Entry needs
    the basis beyond the threshold on `confirm_hours` consecutive closes,
    filled at the last of them (see the module docstring for why)."""
    entry_thr = entry_bps / 10000.0
    exit_thr = exit_bps / 10000.0
    adverse_thr = adverse_bps / 10000.0
    spot_map = {c["t"]: c["c"] for c in spot}
    trades, signals_seen = [], 0
    equity, peak, max_dd = notional, notional, 0.0
    pos = None
    streak = 0
    for cd in perp:
        t, perp_close = cd["t"], cd["c"]
        spot_close = spot_map.get(t)
        if spot_close is None:
            continue
        rate = funding_by_close.get(t + HOUR)
        if rate is None:
            continue
        basis = (perp_close - spot_close) / spot_close
        if pos is None:
            if abs(basis) > entry_thr:
                signals_seen += 1
                streak += 1
                if streak < confirm_hours:
                    continue
                streak = 0
                # Basis > 0: perp at a premium -> short perp + long spot.
                # Basis < 0: perp at a discount -> long perp + short spot
                # (short spot needs borrow: modeled, live blocker).
                direction = "short_perp_long_spot" if basis > 0 else "long_perp_short_spot"
                pos = {"direction": direction, "perp_side": -1 if basis > 0 else 1,
                       "entry_basis": basis, "entry_perp": perp_close,
                       "entry_spot": spot_close, "t0": t + HOUR,
                       "funding": 0.0, "borrow": 0.0, "hours": 0}
            else:
                streak = 0
            continue
        # Holding: accrue this hour's funding on the perp leg and borrow
        # on the spot leg (only when the spot leg is short). Funding rate
        # positive means longs pay shorts.
        pos["funding"] += -pos["perp_side"] * rate * notional
        if pos["direction"] == "long_perp_short_spot":
            pos["borrow"] += borrow_apr / 8760.0 * notional
        pos["hours"] += 1
        perp_pnl = pos["perp_side"] * (perp_close - pos["entry_perp"]) / pos["entry_perp"] * notional
        spot_side = -pos["perp_side"]
        spot_pnl = spot_side * (spot_close - pos["entry_spot"]) / pos["entry_spot"] * notional
        net_unreal = perp_pnl + spot_pnl + pos["funding"] - pos["borrow"]
        exit_price_perp, exit_price_spot, reason = None, None, None
        if net_unreal < -1.00:
            exit_price_perp, exit_price_spot = perp_close, spot_close
            reason = "hard_stop"
        elif abs(basis) <= exit_thr:
            exit_price_perp, exit_price_spot = perp_close, spot_close
            reason = "converged"
        elif pos["hours"] >= timeout_h:
            exit_price_perp, exit_price_spot = perp_close, spot_close
            reason = "timeout"
        elif (pos["direction"] == "short_perp_long_spot" and basis >= adverse_thr) or \
                (pos["direction"] == "long_perp_short_spot" and basis <= -adverse_thr):
            exit_price_perp, exit_price_spot = perp_close, spot_close
            reason = "adverse"
        if reason is None:
            continue
        fees = 4 * TAKER_FEE * notional
        pnl = net_unreal - fees
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        trades.append({
            "coin": coin, "direction": pos["direction"],
            "entry_time": pos["t0"], "exit_time": t + HOUR,
            "entry_basis_bps": round(pos["entry_basis"] * 10000, 1),
            "exit_basis_bps": round(basis * 10000, 1),
            "hours": pos["hours"], "reason": reason,
            "price_pnl": round(perp_pnl + spot_pnl, 4),
            "funding_pnl": round(pos["funding"], 4),
            "borrow_cost": round(-pos["borrow"], 4),
            "fees": round(-fees, 4), "pnl": round(pnl, 4),
        })
        pos = None
    if pos is not None:
        # Still open at end of data: mark to market at the last close.
        last_perp = perp[-1]["c"]
        last_spot = spot_map.get(perp[-1]["t"])
        if last_spot is not None:
            perp_pnl = pos["perp_side"] * (last_perp - pos["entry_perp"]) / pos["entry_perp"] * notional
            spot_side = -pos["perp_side"]
            spot_pnl = spot_side * (last_spot - pos["entry_spot"]) / pos["entry_spot"] * notional
            fees = 4 * TAKER_FEE * notional
            pnl = perp_pnl + spot_pnl + pos["funding"] - pos["borrow"] - fees
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            trades.append({
                "coin": coin, "direction": pos["direction"],
                "entry_time": pos["t0"], "exit_time": perp[-1]["t"] + HOUR,
                "entry_basis_bps": round(pos["entry_basis"] * 10000, 1),
                "exit_basis_bps": round((last_perp - last_spot) / last_spot * 10000, 1),
                "hours": pos["hours"], "reason": "eod",
                "price_pnl": round(perp_pnl + spot_pnl, 4),
                "funding_pnl": round(pos["funding"], 4),
                "borrow_cost": round(-pos["borrow"], 4),
                "fees": round(-fees, 4), "pnl": round(pnl, 4),
            })
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
            sharpe = mean / math.sqrt(var)
    by_direction = {}
    for direction in ("short_perp_long_spot", "long_perp_short_spot"):
        d_trades = [t for t in trades if t["direction"] == direction]
        by_direction[direction] = {
            "trades": len(d_trades),
            "pnl_usd": round(sum(t["pnl"] for t in d_trades), 2),
            "win_rate": round(sum(1 for t in d_trades if t["pnl"] > 0) / len(d_trades), 4)
                        if d_trades else None,
        }
    return {
        "coin": coin, "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "total_pnl_usd": round(total, 2),
        "return_pct": round(total / notional * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "sharpe_per_trade": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_hours": round(sum(t["hours"] for t in trades) / len(trades), 1) if trades else None,
        "price_pnl_usd": round(sum(t["price_pnl"] for t in trades), 2),
        "funding_pnl_usd": round(sum(t["funding_pnl"] for t in trades), 2),
        "borrow_cost_usd": round(sum(t["borrow_cost"] for t in trades), 2),
        "fees_usd": round(sum(t["fees"] for t in trades), 2),
        "exit_reasons": {r: sum(1 for t in trades if t["reason"] == r)
                         for r in ("converged", "timeout", "adverse", "hard_stop", "eod")},
        "signals_seen": signals_seen,
        "by_direction": by_direction,
        "trade_list": trades,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Hedged two-leg basis / cash-and-carry backtest "
                    "(Strategy B) on Hyperliquid public data. Paper only, "
                    "no keys, no orders.")
    ap.add_argument("--days", type=int, default=208)
    ap.add_argument("--end", default="2026-11-16",
                    help="YYYY-MM-DD (default: 2026-11-16)")
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--entry-bps", type=float, default=20.0,
                    help="enter when |basis| exceeds this, in bps (default 20)")
    ap.add_argument("--exit-bps", type=float, default=5.0,
                    help="exit on convergence when |basis| falls below this, in bps (default 5)")
    ap.add_argument("--adverse-bps", type=float, default=40.0,
                    help="exit when basis widens against the position past this, in bps (default 40)")
    ap.add_argument("--timeout", type=int, default=72,
                    help="max hold in hours (default 72)")
    ap.add_argument("--confirm-hours", type=int, default=2,
                    help="consecutive closes beyond the entry threshold "
                         "before filling (default 2; 1 reproduces the naive "
                         "noise-harvesting run, do not quote it)")
    ap.add_argument("--borrow-apr", type=float, default=0.05,
                    help="borrow APR for the short-spot leg (default 0.05)")
    ap.add_argument("--notional", type=float, default=100.0,
                    help="notional USD per leg (default 100)")
    ap.add_argument("--out", default=None, help="write full JSON results here")
    ap.add_argument("--cache-dir", default=None,
                    help="cache raw API responses under DIR (optional)")
    args = ap.parse_args()

    end_ms = int(datetime.strptime(args.end, "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = (end_ms // HOUR) * HOUR
    start_ms = end_ms - args.days * 24 * HOUR
    params = {"entry_bps": args.entry_bps, "exit_bps": args.exit_bps,
              "adverse_bps": args.adverse_bps, "timeout_h": args.timeout,
              "borrow_apr": args.borrow_apr, "notional_usd": args.notional,
              "confirm_hours": args.confirm_hours}
    results = {"strategy": "B basis / cash-and-carry (hedged two-leg)",
               "window_days_requested": args.days,
               "start_requested": time.strftime("%Y-%m-%d", time.gmtime(start_ms / 1000)),
               "end": time.strftime("%Y-%m-%d", time.gmtime(end_ms / 1000)),
               "leverage": "1x", "taker_fee_per_side": TAKER_FEE,
               "params": params,
               "notes": [
                   "Spot history starts 2026-04-26, so the effective window "
                   "is ~203 days even when 208 are requested; see "
                   "data_start per coin.",
                   "Entries need the basis beyond the entry threshold on "
                   "confirm_hours consecutive closes (default 2). The "
                   "1-close version harvests transient last-trade prints "
                   "and overstates PnL; see the module docstring.",
                   "Spot candle closes are used as the spot mid proxy; no "
                   "historical order-book data exists, so slippage is NOT "
                   "modeled and fills use hour closes. PnL is an upper bound.",
                   "long_perp_short_spot needs borrowed spot assets. Borrow "
                   "is priced at --borrow-apr but no borrow source is "
                   "wired up; that direction is a live blocker, not a "
                   "runnable trade on Hyperliquid alone.",
                   "A hedged trade ties up about 2x notional in capital "
                   "(cash for the spot leg plus margin for the perp leg).",
               ],
               "pairs": {}}
    for coin in args.coins:
        spot_coin = resolve_spot_coin(coin, args.cache_dir)
        if spot_coin is None:
            print(f"[{coin}] no USDC spot pair on Hyperliquid, skipped", flush=True)
            results["pairs"][coin] = {"coin": coin, "skipped": "no USDC spot pair"}
            continue
        print(f"[{coin}] spot pair {spot_coin}, fetching perp + spot + funding ...", flush=True)
        perp = fetch_candles(coin, start_ms, end_ms, args.cache_dir)
        spot = fetch_candles(spot_coin, start_ms, end_ms, args.cache_dir)
        funding = fetch_funding(coin, start_ms, end_ms, args.cache_dir)
        print(f"[{coin}] {len(perp)} perp candles, {len(spot)} spot candles, "
              f"{len(funding)} funding rows", flush=True)
        by_close = {}
        for row in funding:
            ts = int(row["time"])
            by_close[(ts // HOUR) * HOUR] = float(row["fundingRate"])
        res = simulate(coin, perp, spot, by_close,
                       notional=args.notional, entry_bps=args.entry_bps,
                       exit_bps=args.exit_bps, adverse_bps=args.adverse_bps,
                       timeout_h=args.timeout, borrow_apr=args.borrow_apr,
                       confirm_hours=args.confirm_hours)
        if perp and spot:
            first_t = max(perp[0]["t"], spot[0]["t"])
            res["data_start"] = time.strftime("%Y-%m-%d %H:%M",
                                              time.gmtime(first_t / 1000))
            res["data_hours"] = len(set(c["t"] for c in perp) &
                                    set(c["t"] for c in spot))
            res["buy_and_hold_perp_pct"] = round(
                (perp[-1]["c"] / perp[0]["c"] - 1) * 100, 2)
        results["pairs"][coin] = res
        print(f"[{coin}] trades={res['trades']} pnl=${res['total_pnl_usd']} "
              f"return={res['return_pct']}% win={res['win_rate']} "
              f"signals={res['signals_seen']}", flush=True)
    done = [r for r in results["pairs"].values() if "skipped" not in r]
    results["combined"] = {
        "trades": sum(r["trades"] for r in done),
        "total_pnl_usd": round(sum(r["total_pnl_usd"] for r in done), 2),
        "price_pnl_usd": round(sum(r["price_pnl_usd"] for r in done), 2),
        "funding_pnl_usd": round(sum(r["funding_pnl_usd"] for r in done), 2),
        "borrow_cost_usd": round(sum(r["borrow_cost_usd"] for r in done), 2),
        "fees_usd": round(sum(r["fees_usd"] for r in done), 2),
        "win_rate": (round(sum(1 for r in done for t in r["trade_list"] if t["pnl"] > 0)
                           / max(1, sum(r["trades"] for r in done)), 4)
                     if done else None),
    }
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
