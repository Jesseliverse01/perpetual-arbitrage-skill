#!/usr/bin/env python3
"""Backtest Strategy E — delta-neutral cross-venue funding harvest
(Hyperliquid vs Lighter) — for the perpetual-arbitrage
skill. Paper/backtest only. No keys, no trading.

The trade: long the venue paying funding, short the venue charging it,
same asset, same size ($50 notional per leg). Legs cancel price
exposure; what remains is the funding-rate spread, collected hourly
while the spread holds. Close rules, sizing, cooldown and filters are
the Strategy E defaults, adapted to public data:

  leg collat $10, effective leverage 5 (target 10, cap 5) -> leg
  notional $50; max hold 96h; min hold 12h (soft exits blocked before);
  hard stop -5% of (leg collat x 2) = -$1.00 net, fires any time;
  safe floor max(open_apr x 0.70, 70%); smart_neg (last 3 spread
  readings all < -10%); smart_decl (last 7 readings strictly
  declining); low_apr (last 6 readings all < 10%, muted once funding
  has paid back friction x1.5); cooldown 2 losses -> 48h block;
  entry 70% <= net APR <= 120% (hard cap 200%); apr_24h >= 0 and
  apr_7d >= 0; spike ratios 1h/24h <= 5, 24h/7d <= 3; one arb per asset.

Data (all public, no keys):
  Hyperliquid fundingHistory (hourly fraction) + candleSnapshot 1h
  Lighter /fundings (hourly; `rate` is a PERCENT string, signed by
  `direction`: +rate when direction == 'long', -rate when 'short';
  calibrated live 2026-11-16: Lighter BTC fundings rate 0.0012%
  direction long == funding-rates endpoint Lighter BTC 8h rate 9.6e-05
  -> hourly 1.2e-05) + /markPriceCandles 1h (OHLC marks).

Price PnL (uPnL) is REAL basis drift, not assumed zero:
  uPnL = N * [(P_L(t)/P_L0 - 1) - (P_S(t)/P_S0 - 1)]
with P_L / P_S the mark closes of the long / short venue. net_usd =
uPnL + funding_cum. Fees modeled: HL taker 0.035% per side on the HL
leg only; Lighter taker fee is 0 on these markets (orderBooks
taker_fee 0.0000). Slippage is NOT modeled (no historical book data)
— the friction estimate (15bps x notional x 2 legs x 2 = $0.30
per arb) is used ONLY for the funding-breakeven flag.

Honest limits (read before quoting any number):
  - Volume gates are NOT applied: historical per-venue volume does not
    exist in these public endpoints (same dropped-dimension situation
    as the v0.3 scorer's liquidity dimension).
  - No slippage model; entries/exits fill at hour-close marks.
  - The hard stop is evaluated at hour closes (hourly cycle cadence),
    not intra-hour — a fast basis move inside one hour is missed.
  - Lighter funding history is missing ~7 of 4992 hours in the window;
    missing hours accrue no funding and skip soft-exit evaluation.
  - Capital base differs from Strategy A ($100 notional 1x per coin):
    Strategy E deploys $20 margin per arb ($10 x 2 legs) for $50
    notional per leg. Compare PnL in USD and return-on-margin, not
    raw return_pct, across strategies.

Usage:
  python3 backtest_strategy_e.py [--days 208] [--end 2026-11-16]
      [--coins BTC ETH HYPE] [--out results/backtest-strategy-e-....json]
Stdlib only, no keys, no trading. No lookahead: every decision uses
data up to the current hour only.
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
from scan import hl_post  # noqa: E402
from multivenue_scan import lighter_fundings_history  # noqa: E402

HOUR = 3600_000
LIGHTER_API = "https://mainnet.zklighter.elliot.ai/api/v1"

# --- Strategy E defaults ---
LEG_COLLAT_USD = 10.0
EFFECTIVE_LEV = 5
LEG_NOTIONAL_USD = LEG_COLLAT_USD * EFFECTIVE_LEV  # $50 per leg
MIN_NET_APR = 0.70
MAX_NET_APR = 1.20
APR_UPPER_CAP = 2.00
MAX_HOLD_HOURS = 96
MIN_HOLD_HOURS = 12
STOP_LOSS_PCT = 0.05  # of (leg collat x 2) -> -$1.00
SAFE_FLOOR_MULT = 0.70
NEGATIVE_WINDOW = 3
SMART_NEG_VALUE_FLOOR = -0.10
DECLINE_WINDOW = 6  # smart_decl: last 7 readings strictly declining
LOW_APR_THRESHOLD = 0.10
LOW_APR_WINDOW = 6
ESTIMATED_FRICTION_BPS = 15
FRICTION_USD = (LEG_NOTIONAL_USD * 2) * (ESTIMATED_FRICTION_BPS / 10000) * 2
FB_THRESHOLD_USD = FRICTION_USD * 1.5  # $0.45
COOLDOWN_AFTER_LOSSES = 2
COOLDOWN_HOURS = 48
HL_TAKER_FEE = 0.00035  # per side, HL leg only; Lighter taker = 0
WARMUP_HOURS = 168  # need a full 7d of spread history before entries

BLACKLIST = {
    "YZY", "STBL", "2Z", "CC", "CHIP", "VVV", "MEGA", "AZTEC", "JTO",
    "STABLE", "alias:mu", "alias:bmnr", "EIGEN", "WLFI", "XMR", "PYTH",
    "ZRO", "CRV",
}


def fetch_hl_funding(coin, start_ms, end_ms):
    rows, cur = [], start_ms
    while True:
        page = hl_post({"type": "fundingHistory", "coin": coin,
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
    return {(int(r["time"]) // HOUR) * HOUR: float(r["fundingRate"])
            for r in rows}


def fetch_hl_candles(coin, start_ms, end_ms):
    data = hl_post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": start_ms, "endTime": end_ms}})
    out = {}
    for c in data:
        out[int(c["t"])] = float(c["c"])
    return out


def fetch_lighter_candles(market_id, start_ms, end_ms):
    """Mark-price closes by hour-open (ms). Chunked 20d per call: the
    endpoint caps at 500 rows/call (a 30d chunk silently returns only
    the last 500h — found live 2026-11-16: 7x30d chunks gave 3500
    rows instead of 4992)."""
    out, cur = {}, start_ms
    while cur < end_ms:
        chunk_end = min(cur + 20 * 24 * HOUR, end_ms)
        url = (f"{LIGHTER_API}/markPriceCandles?market_id={market_id}"
               f"&resolution=1h&start_timestamp={cur}"
               f"&end_timestamp={chunk_end}&count_back=0")
        req = urllib.request.Request(
            url, headers={"User-Agent": "perpetual-arb-strategy-e/0.4"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d.get("code") != 200:
            raise RuntimeError(f"Lighter markPriceCandles code {d.get('code')}")
        for c in d.get("c", []):
            out[int(c["t"])] = float(c["c"])
        cur = chunk_end
        time.sleep(0.1)
    return out


def lighter_market_ids():
    req = urllib.request.Request(
        f"{LIGHTER_API}/orderBooks",
        headers={"User-Agent": "perpetual-arb-strategy-e/0.4"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return {o["symbol"]: o["market_id"] for o in d.get("order_books", [])
            if o.get("market_type") == "perp"}


def upnl_now(pos, p_hl, p_li, N):
    """Mark-to-market of the pair from basis drift vs entry marks."""
    if pos["long_venue"] == "lighter":
        return (N * (p_li / pos["p_li0"] - 1)
                - N * (p_hl / pos["p_hl0"] - 1))
    return (N * (p_hl / pos["p_hl0"] - 1)
            - N * (p_li / pos["p_li0"] - 1))


def simulate(coin, hours, hl_rate, li_rate, hl_close, li_close):
    """hours: sorted hour-open ms list. Rates are hourly fractions keyed
    by hour-close bucket (t + HOUR). Closes keyed by hour-open."""
    N = LEG_NOTIONAL_USD
    trades = []
    pos = None
    spread_hist = []  # net APR readings, appended hour by hour
    cooldown_losses = 0
    cooldown_last_loss = None
    signals_seen = 0
    signals_filtered = 0
    hours_with_data = 0

    for t in hours:
        close_key = t + HOUR
        r_hl = hl_rate.get(close_key)
        r_li = li_rate.get(close_key)
        p_hl = hl_close.get(t)
        p_li = li_close.get(t)
        if r_hl is None or r_li is None or p_hl is None or p_li is None:
            # Missing data this hour: no entry, no funding accrual, no
            # soft-exit evaluation. Hard stop / max hold still apply
            # using the hour-close marks, with cooldown updated.
            if pos is not None and p_hl is not None and p_li is not None:
                upnl = upnl_now(pos, p_hl, p_li, N)
                net_usd = upnl + pos["funding"]
                pos["hours"] += 1
                reason = None
                if net_usd < -STOP_LOSS_PCT * LEG_COLLAT_USD * 2:
                    reason = "hard_stop_loss"
                elif pos["hours"] > MAX_HOLD_HOURS:
                    reason = "max_hold"
                if reason is not None:
                    realized = close_pos(trades, pos, t + HOUR, p_hl, p_li,
                                         reason, N, upnl_override=upnl)
                    if realized < 0:
                        cooldown_losses += 1
                        cooldown_last_loss = t + HOUR
                    else:
                        cooldown_losses = 0
                        cooldown_last_loss = None
                    pos = None
            continue
        hours_with_data += 1
        # Net spread APR this hour: long the lower-rate venue.
        if r_hl <= r_li:
            long_venue, r_long, r_short = "hyperliquid", r_hl, r_li
        else:
            long_venue, r_long, r_short = "lighter", r_li, r_hl
        net_apr = (r_short - r_long) * 24 * 365
        spread_hist.append(net_apr)
        apr_24h = (sum(spread_hist[-24:]) / len(spread_hist[-24:])
                   if len(spread_hist) >= 24 else None)
        apr_7d = (sum(spread_hist[-168:]) / len(spread_hist[-168:])
                  if len(spread_hist) >= 168 else None)

        if pos is None:
            if coin in BLACKLIST:
                continue
            if len(spread_hist) < WARMUP_HOURS:
                continue
            if cooldown_losses >= COOLDOWN_AFTER_LOSSES and cooldown_last_loss is not None \
                    and (t - cooldown_last_loss) < COOLDOWN_HOURS * HOUR:
                continue
            signals_seen += 1
            ok = (MIN_NET_APR <= net_apr <= min(MAX_NET_APR, APR_UPPER_CAP)
                  and apr_24h is not None and apr_24h >= 0
                  and apr_7d is not None and apr_7d >= 0
                  and net_apr / max(apr_24h, 0.0001) <= 5
                  and apr_24h / max(apr_7d, 0.0001) <= 3)
            if not ok:
                signals_filtered += 1
                continue
            pos = {"long_venue": long_venue,
                   "short_venue": "lighter" if long_venue == "hyperliquid" else "hyperliquid",
                   "entry": t + HOUR, "t0": t,
                   "open_net_apr": net_apr,
                   "p_hl0": p_hl, "p_li0": p_li,
                   "funding": 0.0, "peak_funding": 0.0,
                   "fb_achieved": False, "hours": 0,
                   "apr_hist": [net_apr]}
            continue

        # --- position open: accrue funding, update marks, evaluate closes
        pos["funding"] += (r_short - r_long) * N
        pos["peak_funding"] = max(pos["peak_funding"], pos["funding"])
        if not pos["fb_achieved"] and pos["peak_funding"] > FB_THRESHOLD_USD:
            pos["fb_achieved"] = True
        pos["hours"] += 1
        pos["apr_hist"].append(net_apr)
        # uPnL from basis drift (both legs' mark moves vs entry)
        upnl = upnl_now(pos, p_hl, p_li, N)
        net_usd = upnl + pos["funding"]

        reason = None
        if net_usd < -STOP_LOSS_PCT * LEG_COLLAT_USD * 2:
            reason = "hard_stop_loss"
        elif pos["hours"] > MAX_HOLD_HOURS:
            reason = "max_hold"
        elif pos["hours"] < MIN_HOLD_HOURS:
            reason = None  # min-hold gate: soft exits blocked
        else:
            safe_floor = max(pos["open_net_apr"] * SAFE_FLOOR_MULT, MIN_NET_APR)
            check_floor = not (pos["fb_achieved"])  # fb skips the floor
            below_floor = (net_apr < safe_floor) or (not check_floor)
            h = pos["apr_hist"]
            if below_floor and len(h) >= NEGATIVE_WINDOW \
                    and all(x < SMART_NEG_VALUE_FLOOR for x in h[-NEGATIVE_WINDOW:]):
                reason = "smart_neg_3"
            elif below_floor and len(h) >= DECLINE_WINDOW + 1 \
                    and all(h[-i] < h[-i - 1] for i in range(1, DECLINE_WINDOW + 1)):
                reason = "smart_decl_6"
            elif (not pos["fb_achieved"]) and len(h) >= LOW_APR_WINDOW \
                    and all(x < LOW_APR_THRESHOLD for x in h[-LOW_APR_WINDOW:]):
                reason = "low_apr_6"
        if reason is None:
            continue
        realized = close_pos(trades, pos, t + HOUR, p_hl, p_li, reason, N,
                             upnl_override=upnl)
        if realized < 0:
            cooldown_losses += 1
            cooldown_last_loss = t + HOUR
        else:
            cooldown_losses = 0
            cooldown_last_loss = None
        pos = None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    rets = [t["pnl"] / (LEG_COLLAT_USD * 2) for t in trades]
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
        "return_on_margin_pct": round(total / (LEG_COLLAT_USD * 2) * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "sharpe_per_trade": round(sharpe, 2) if sharpe is not None else None,
        "avg_hours": round(sum(t["hours"] for t in trades) / len(trades), 1) if trades else None,
        "funding_pnl_usd": round(sum(t["funding_pnl"] for t in trades), 2),
        "price_pnl_usd": round(sum(t["price_pnl"] for t in trades), 2),
        "fees_usd": round(sum(t["fees"] for t in trades), 2),
        "exit_reasons": {r: sum(1 for t in trades if t["reason"] == r)
                         for r in ("hard_stop_loss", "max_hold", "smart_neg_3",
                                   "smart_decl_6", "low_apr_6")},
        "signals_seen": signals_seen,
        "signals_filtered": signals_filtered,
        "hours_with_data": hours_with_data,
        "trade_list": trades,
    }


def close_pos(trades, pos, exit_t, p_hl, p_li, reason, N, upnl_override=None):
    """Close at hour-close marks; returns realized PnL (for cooldown)."""
    upnl = (upnl_override if upnl_override is not None
            else upnl_now(pos, p_hl, p_li, N))
    fees = HL_TAKER_FEE * N * 2  # HL leg open+close; Lighter leg 0
    pnl = upnl + pos["funding"] - fees
    trades.append({
        "coin": None, "long_venue": pos["long_venue"],
        "short_venue": pos["short_venue"],
        "entry_time": pos["entry"], "exit_time": exit_t,
        "open_net_apr_pct": round(pos["open_net_apr"] * 100, 2),
        "hours": pos["hours"], "reason": reason,
        "funding_pnl": round(pos["funding"], 4),
        "price_pnl": round(upnl, 4), "fees": round(fees, 4),
        "pnl": round(pnl, 4),
        "funding_breakeven_achieved": pos["fb_achieved"],
    })
    return pnl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=208)
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "HYPE"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.end:
        end_ms = int(datetime.strptime(args.end, "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        end_ms = int(time.time() * 1000)
    end_ms = (end_ms // HOUR) * HOUR
    start_ms = end_ms - args.days * 24 * HOUR

    mids = lighter_market_ids()
    print("Lighter market ids:", {c: mids.get(c) for c in args.coins}, flush=True)

    results = {
        "strategy": "E",
        "window_days": args.days,
        "start": time.strftime("%Y-%m-%d", time.gmtime(start_ms / 1000)),
        "end": time.strftime("%Y-%m-%d", time.gmtime(end_ms / 1000)),
        "leg_collat_usd": LEG_COLLAT_USD,
        "leg_notional_usd": LEG_NOTIONAL_USD,
        "margin_per_arb_usd": LEG_COLLAT_USD * 2,
        "params": {
            "min_net_apr": MIN_NET_APR, "max_net_apr": MAX_NET_APR,
            "apr_upper_cap": APR_UPPER_CAP, "max_hold_h": MAX_HOLD_HOURS,
            "min_hold_h": MIN_HOLD_HOURS,
            "stop_loss_usd": -STOP_LOSS_PCT * LEG_COLLAT_USD * 2,
            "safe_floor_mult": SAFE_FLOOR_MULT,
            "friction_usd": FRICTION_USD,
            "funding_breakeven_threshold_usd": FB_THRESHOLD_USD,
            "hl_taker_fee": HL_TAKER_FEE, "lighter_taker_fee": 0.0,
            "warmup_h": WARMUP_HOURS,
        },
        "notes": [
            "Volume gates NOT applied (no historical per-venue volume in "
            "public endpoints). Slippage NOT modeled (no historical book "
            "data). Hard stop evaluated at hour closes only (hourly "
            "cycle cadence), not intra-hour.",
            "Lighter fundings `rate` is a PERCENT string, signed by "
            "`direction` (calibrated live 2026-11-16).",
        ],
        "pairs": {},
    }
    for coin in args.coins:
        print(f"[{coin}] fetching HL funding + candles ...", flush=True)
        hl_rate = fetch_hl_funding(coin, start_ms, end_ms)
        hl_close = fetch_hl_candles(coin, start_ms, end_ms)
        print(f"[{coin}] fetching Lighter fundings + mark candles ...", flush=True)
        li_rows = lighter_fundings_history(mids[coin], start_ms, end_ms)
        li_rate = {(r["t"] // HOUR) * HOUR: r["rate"] for r in li_rows}
        li_close = fetch_lighter_candles(mids[coin], start_ms, end_ms)
        print(f"[{coin}] HL funding {len(hl_rate)}h, Lighter funding "
              f"{len(li_rate)}h, HL candles {len(hl_close)}, Lighter "
              f"candles {len(li_close)}", flush=True)
        hours = sorted(set(hl_close) | set(li_close))
        hours = [h for h in hours if start_ms <= h < end_ms]
        res = simulate(coin, hours, hl_rate, li_rate, hl_close, li_close)
        for t in res["trade_list"]:
            t["coin"] = coin
        results["pairs"][coin] = res
        print(f"[{coin}] trades={res['trades']} pnl=${res['total_pnl_usd']} "
              f"win={res['win_rate']} funding=${res['funding_pnl_usd']} "
              f"price=${res['price_pnl_usd']} "
              f"signals={res['signals_seen']}/{res['signals_filtered']} "
              f"filtered", flush=True)
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
