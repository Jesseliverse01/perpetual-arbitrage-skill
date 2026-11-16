#!/usr/bin/env python3
"""Live multi-venue funding-spread scanner (v0.4) for the
perpetual-arbitrage skill — Strategy E (delta-neutral cross-venue
funding harvest, Vooi-grade).

Venues (all public endpoints, no keys, no trading):
  Hyperliquid  metaAndAssetCtxs (hourly funding, volume, OI) — always works
  Lighter      /api/v1/funding-rates — ONE call returns funding for
               hyperliquid, lighter, binance AND bybit (the endpoint
               reports every venue's rate). This is how Binance/Bybit
               rates are readable from this VM despite their direct
               APIs being geo-blocked (451/403).
  Aster        /fapi/v1/premiumIndex (bulk) — works from this VM
  OKX          /api/v5/public/funding-rate?instId=COIN-USDT-SWAP
               (per coin, fetched only for candidate coins)

Rate semantics (verified live 2026-11-16 against Hyperliquid):
  - Lighter's funding-rates `rate` is an 8h-normalized rate for EVERY
    venue: APR = rate * 3 * 365. Proof: HL BTC live hourly funding
    9.711e-06 -> APR 8.51%; Lighter endpoint HL BTC rate 7.786e-05
    -> as 8h APR 8.53% (match), as hourly APR 68.21% (wrong).
  - Aster `lastFundingRate` is an 8h rate: APR = rate * 3 * 365.
  - OKX `fundingRate` is an 8h rate: APR = rate * 3 * 365.
  - Hyperliquid `funding` is an hourly rate: APR = rate * 24 * 365.

For each coin listed on >= 2 venues the best pair is computed:
long the venue with the most negative APR, short the venue with the
most positive APR. net APR = short_apr - long_apr (fraction).

Vooi open filters applied where computable from public data:
  - MIN_NET_APR 0.70 <= net APR <= MAX_NET_APR 1.20 (hard cap 2.00)
  - Asset blacklist (Vooi list, incl. XMR, CC, PYTH, CRV, WLFI, EIGEN)
  - Volume gates where the venue exposes volume: HL dayNtlVlm >= $100k
    (MIN) and >= $250k (per-leg). Lighter/Aster/OKX funding endpoints
    expose no volume — the gate is NOT applied there and the row is
    flagged volume_unknown instead of silently passed.
  - Spike filters (apr_24h >= 0, apr_7d >= 0, 1h/24h <= 5, 24h/7d <= 3)
    need spread history: computed only for candidates whose pair is
    HL<->Lighter (both have public hourly funding history). Other pairs
    are flagged history_unavailable — the filter is not faked.

Verdicts: PASS = all computable filters pass; WATCH = in the APR band
but a filter fails or is uncomputable; SKIP = outside the band.
PASS here means "worth the full Strategy E evaluation" (SKILL.md §13),
not "open now" — live execution needs a VOOI account + token + Jesse's
explicit approval, none of which exists in this skill.

Outputs:
  results/multivenue-scan-YYYY-MM-DD-HHMM.json (rows + venue status)

Usage:
  python3 multivenue_scan.py [--min-net-apr 0.70] [--max-net-apr 1.20]
      [--out-dir ../results]
Stdlib only, no keys, no trading.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import hl_post, okx_funding  # noqa: E402

HOUR = 3600_000
LIGHTER_API = "https://mainnet.zklighter.elliot.ai/api/v1"
ASTER_API = "https://fapi.asterdex.com/fapi/v1/premiumIndex"

# Vooi asset blacklist (vooi-funding-arb-cycle SKILL.md, verbatim).
BLACKLIST = {
    "YZY", "STBL", "2Z", "CC", "CHIP", "VVV", "MEGA", "AZTEC", "JTO",
    "STABLE", "alias:mu", "alias:bmnr", "EIGEN", "WLFI", "XMR", "PYTH",
    "ZRO", "CRV",
}
MIN_NET_APR = 0.70
MAX_NET_APR = 1.20
APR_UPPER_CAP = 2.00
MIN_VOLUME_24H_USD = 100_000
PER_LEG_VOLUME_MIN_USD = 250_000
APR_RATIO_1H_TO_24H_MAX = 5
APR_RATIO_24H_TO_7D_MAX = 3


def normalize_symbol(sym):
    """'BTC-USDT-SWAP' / 'BTCUSDT' / 'BTC' -> 'BTC'."""
    s = sym.upper()
    for suffix in ("-USDT-SWAP", "-USD-SWAP", "USDT", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[:-len(suffix)]
            break
    return s


def lighter_funding_rates():
    """{(exchange, coin): APR fraction} from Lighter's cross-venue
    endpoint. Returns ({}, error) on failure — never a partial map."""
    url = f"{LIGHTER_API}/funding-rates"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "perpetual-arb-multivenue/0.4"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d.get("code") != 200:
            return {}, f"code {d.get('code')}: {d.get('message', '')[:80]}"
        out = {}
        for row in d.get("funding_rates", []):
            try:
                # rate is 8h-normalized for every venue (verified live):
                # APR = rate * 3 * 365.
                out[(row["exchange"], row["symbol"])] = float(row["rate"]) * 3 * 365
            except (KeyError, ValueError, TypeError):
                continue
        return out, None
    except Exception as e:
        return {}, str(e)[:120]


def aster_funding_map():
    """{COIN: APR fraction} from Aster premiumIndex (bulk), or ({}, err)."""
    try:
        req = urllib.request.Request(
            ASTER_API, headers={"User-Agent": "perpetual-arb-multivenue/0.4"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        if isinstance(data, dict):
            return {}, f"error payload: {str(data)[:80]}"
        out = {}
        for x in data:
            try:
                # Aster funds every 8h: APR = 8h rate * 3 * 365.
                out[normalize_symbol(x["symbol"])] = float(x["lastFundingRate"]) * 3 * 365
            except (KeyError, ValueError, TypeError):
                continue
        return out, None
    except Exception as e:
        return {}, str(e)[:120]


def lighter_fundings_history(market_id, start_ms, end_ms):
    """Lighter hourly funding rates (fraction, signed) for a market.

    Endpoint /fundings returns `rate` as a PERCENT string (0.0012 means
    0.0012% = 1.2e-05 per hour) and `direction` long/short for the sign:
    signed rate = +rate when direction == 'long' (longs pay), -rate when
    'short'. Calibrated live 2026-11-16: Lighter BTC fundings rate
    0.0012% direction long == funding-rates endpoint Lighter BTC 8h rate
    9.6e-05 -> hourly 1.2e-05. Max 750 rows/call — chunk by 30d.
    """
    rows, cur = [], start_ms
    while cur < end_ms:
        chunk_end = min(cur + 30 * 24 * HOUR, end_ms)
        url = (f"{LIGHTER_API}/fundings?market_id={market_id}"
               f"&resolution=1h&start_timestamp={cur}"
               f"&end_timestamp={chunk_end}&count_back=0")
        req = urllib.request.Request(
            url, headers={"User-Agent": "perpetual-arb-multivenue/0.4"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d.get("code") != 200:
            raise RuntimeError(f"Lighter fundings code {d.get('code')}")
        for f in d.get("fundings", []):
            sign = 1.0 if f.get("direction") == "long" else -1.0
            rows.append({
                "t": int(f["timestamp"]) * 1000,
                "rate": sign * float(f["rate"]) / 100.0,
            })
        if len(d.get("fundings", [])) == 0 and chunk_end >= end_ms:
            break
        cur = chunk_end
        time.sleep(0.1)
    rows.sort(key=lambda x: x["t"])
    return rows


def hl_funding_history(coin, start_ms, end_ms):
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
    return [{"t": int(r["time"]), "rate": float(r["fundingRate"])}
            for r in rows]


def spread_history_stats(long_venue, short_venue, coin, market_id, now_ms):
    """Trailing spread stats for an HL<->Lighter pair, or None if the
    history cannot be computed. Returns dict with apr_1h, apr_24h,
    apr_7d (fractions) — point-in-time, last 7d only."""
    if {long_venue, short_venue} != {"hyperliquid", "lighter"}:
        return None
    try:
        hl = hl_funding_history(coin, now_ms - 7 * 24 * HOUR, now_ms)
        li = lighter_fundings_history(market_id, now_ms - 7 * 24 * HOUR, now_ms)
    except Exception as e:
        print(f"  [{coin}] spread history failed: {e}", flush=True)
        return None
    hl_by_h = {(r["t"] // HOUR) * HOUR: r["rate"] for r in hl}
    li_by_h = {(r["t"] // HOUR) * HOUR: r["rate"] for r in li}
    hours = sorted(set(hl_by_h) & set(li_by_h))
    if len(hours) < 24:
        return None
    # net APR per hour: short venue rate minus long venue rate.
    # long_venue is the one with the lower rate at scan time; keep the
    # same leg assignment across history.
    spread = []
    for h in hours:
        r_hl, r_li = hl_by_h[h], li_by_h[h]
        r_long = r_hl if long_venue == "hyperliquid" else r_li
        r_short = r_li if long_venue == "hyperliquid" else r_hl
        spread.append((r_short - r_long) * 24 * 365)
    apr_1h = spread[-1]
    apr_24h = sum(spread[-24:]) / len(spread[-24:])
    apr_7d = sum(spread) / len(spread)
    return {"apr_1h": apr_1h, "apr_24h": apr_24h, "apr_7d": apr_7d,
            "hours": len(hours)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-net-apr", type=float, default=MIN_NET_APR)
    ap.add_argument("--max-net-apr", type=float, default=MAX_NET_APR)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out_dir or os.path.join(here, "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    now_ms = int(time.time() * 1000)
    now_ms = (now_ms // HOUR) * HOUR
    stamp = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d-%H%M")

    venue_status = {}

    # --- Hyperliquid (live funding + volume + OI) ---
    print("fetching Hyperliquid universe ...", flush=True)
    meta, ctxs = hl_post({"type": "metaAndAssetCtxs"})
    hl = {}
    for m, c in zip(meta["universe"], ctxs):
        hl[m["name"]] = {
            "apr": float(c["funding"]) * 24 * 365,
            "mark": float(c["markPx"]),
            "volume_usd": float(c["dayNtlVlm"]),
            "oi_usd": float(c["openInterest"]) * float(c["markPx"]),
        }
    venue_status["hyperliquid"] = {"ok": True, "coins": len(hl)}

    # --- Lighter funding-rates (HL + Lighter + Binance + Bybit) ---
    print("fetching Lighter cross-venue funding-rates ...", flush=True)
    lr, lr_err = lighter_funding_rates()
    venues_from_lighter = sorted({ex for (ex, _) in lr})
    venue_status["lighter"] = {"ok": len(lr) > 0,
                               "coins": len({c for (_, c) in lr}),
                               "venues_included": venues_from_lighter,
                               "error": lr_err}
    print(f"Lighter funding-rates: {len(lr)} rows "
          f"({', '.join(venues_from_lighter) or 'none'})", flush=True)

    # --- Aster (bulk premiumIndex) ---
    print("fetching Aster premiumIndex ...", flush=True)
    aster, aster_err = aster_funding_map()
    venue_status["aster"] = {"ok": len(aster) > 0, "coins": len(aster),
                             "error": aster_err}
    print(f"Aster: {len(aster)} coins", flush=True)

    # --- Build per-coin venue map ---
    # rates[coin][venue] = APR fraction. Lighter's endpoint is the source
    # for binance/bybit (direct APIs geo-blocked); its hyperliquid rows
    # are kept as a cross-check, not as the HL source (HL direct is live).
    rates = {}
    for coin, u in hl.items():
        rates.setdefault(coin, {})["hyperliquid"] = u["apr"]
    for (ex, coin), apr in lr.items():
        if ex == "hyperliquid":
            continue  # HL direct is authoritative for HL
        rates.setdefault(coin, {})[ex] = apr
    for coin, apr in aster.items():
        rates.setdefault(coin, {})["aster"] = apr

    # Candidate coins: >= 2 venues and best net APR in a wide band
    # (50%-200%) so near-misses are visible as WATCH, not invisible.
    cands = []
    for coin, vmap in rates.items():
        if len(vmap) < 2:
            continue
        long_venue = min(vmap, key=lambda v: vmap[v])
        short_venue = max(vmap, key=lambda v: vmap[v])
        net_apr = vmap[short_venue] - vmap[long_venue]
        if net_apr >= 0.50:
            cands.append((coin, vmap, long_venue, short_venue, net_apr))
    cands.sort(key=lambda x: x[4], reverse=True)
    print(f"{len(rates)} coins on >=1 venue; {len(cands)} with net APR "
          f">= 50% across venues", flush=True)

    # Lighter market ids (for spread history) — from orderBooks.
    market_ids = {}
    try:
        req = urllib.request.Request(
            f"{LIGHTER_API}/orderBooks",
            headers={"User-Agent": "perpetual-arb-multivenue/0.4"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        for o in d.get("order_books", []):
            if o.get("market_type") == "perp":
                market_ids[o["symbol"]] = o["market_id"]
    except Exception as e:
        print(f"orderBooks failed ({e}) — spread history unavailable",
              flush=True)

    # --- OKX for candidate coins only ---
    okx = {}
    for coin, _, _, _, _ in cands:
        apr = okx_funding(coin)
        if apr is not None:
            okx[coin] = apr
        time.sleep(0.05)
    venue_status["okx"] = {"ok": len(okx) > 0, "coins": len(okx),
                           "note": "per-coin fetch, candidates only"}
    # Recompute best pair with OKX included.
    for coin, apr in okx.items():
        rates.setdefault(coin, {})["okx"] = apr
    cands = []
    for coin, vmap in rates.items():
        if len(vmap) < 2:
            continue
        long_venue = min(vmap, key=lambda v: vmap[v])
        short_venue = max(vmap, key=lambda v: vmap[v])
        net_apr = vmap[short_venue] - vmap[long_venue]
        if net_apr >= 0.50:
            cands.append((coin, vmap, long_venue, short_venue, net_apr))
    cands.sort(key=lambda x: x[4], reverse=True)

    rows = []
    for coin, vmap, long_venue, short_venue, net_apr in cands:
        reasons = []
        # 1. Blacklist
        if coin in BLACKLIST:
            reasons.append(f"blacklisted:{coin}")
        # 2. APR band (Vooi MIN/MAX + hard cap)
        if net_apr < args.min_net_apr:
            reasons.append(f"net_apr {net_apr * 100:.1f}% < MIN {args.min_net_apr * 100:.0f}%")
        if net_apr > args.max_net_apr:
            reasons.append(f"net_apr {net_apr * 100:.1f}% > MAX {args.max_net_apr * 100:.0f}%")
        if net_apr > APR_UPPER_CAP:
            reasons.append(f"net_apr {net_apr * 100:.1f}% > hard cap {APR_UPPER_CAP * 100:.0f}%")
        # 3. Volume gates where computable (HL leg only)
        volume_ok, volume_unknown = True, []
        if "hyperliquid" in (long_venue, short_venue):
            vol = hl[coin]["volume_usd"]
            if vol < MIN_VOLUME_24H_USD:
                volume_ok = False
                reasons.append(f"HL vol24h ${vol:,.0f} < ${MIN_VOLUME_24H_USD:,}")
            elif vol < PER_LEG_VOLUME_MIN_USD:
                volume_ok = False
                reasons.append(f"HL vol24h ${vol:,.0f} < per-leg ${PER_LEG_VOLUME_MIN_USD:,}")
        other = [v for v in (long_venue, short_venue) if v != "hyperliquid"]
        if other:
            volume_unknown.append(f"volume unknown on {','.join(other)}")
        # 4. Spike filters — only for HL<->Lighter pairs (history exists)
        hist = None
        hist_note = None
        if {long_venue, short_venue} == {"hyperliquid", "lighter"} and coin in market_ids:
            print(f"[{coin}] fetching 7d spread history ...", flush=True)
            hist = spread_history_stats(long_venue, short_venue, coin,
                                        market_ids[coin], now_ms)
            if hist is None:
                hist_note = "spread history unavailable"
            else:
                a1, a24, a7 = hist["apr_1h"], hist["apr_24h"], hist["apr_7d"]
                if a24 < 0:
                    reasons.append(f"apr_24h {a24 * 100:.1f}% < 0")
                if a7 < 0:
                    reasons.append(f"apr_7d {a7 * 100:.1f}% < 0")
                if a1 / max(a24, 0.0001) > APR_RATIO_1H_TO_24H_MAX:
                    reasons.append(f"1h/24h ratio {a1 / max(a24, 0.0001):.1f} > 5")
                if a24 / max(a7, 0.0001) > APR_RATIO_24H_TO_7D_MAX:
                    reasons.append(f"24h/7d ratio {a24 / max(a7, 0.0001):.1f} > 3")
        elif {long_venue, short_venue} != {"hyperliquid", "lighter"}:
            hist_note = "spread history only computable for HL<->Lighter pairs"
        else:
            hist_note = "no Lighter market id"

        if reasons:
            verdict = "WATCH"
        elif hist_note:
            verdict = "WATCH"
        else:
            verdict = "PASS"
        if net_apr < args.min_net_apr or net_apr > APR_UPPER_CAP:
            verdict = "SKIP"
        rows.append({
            "coin": coin,
            "long_venue": long_venue,
            "short_venue": short_venue,
            "long_apr_pct": round(vmap[long_venue] * 100, 2),
            "short_apr_pct": round(vmap[short_venue] * 100, 2),
            "net_apr_pct": round(net_apr * 100, 2),
            "venues": {v: round(a * 100, 2) for v, a in vmap.items()},
            "hl_volume_usd": round(hl[coin]["volume_usd"], 2) if coin in hl else None,
            "volume_ok": volume_ok,
            "volume_unknown": volume_unknown,
            "spread_history": hist,
            "history_note": hist_note,
            "verdict": verdict,
            "reject_reasons": reasons,
        })
        time.sleep(0.05)

    passes = [r for r in rows if r["verdict"] == "PASS"]
    payload = {
        "generated_utc": stamp,
        "min_net_apr": args.min_net_apr,
        "max_net_apr": args.max_net_apr,
        "venue_status": venue_status,
        "coins_with_2plus_venues": sum(1 for v in rates.values() if len(v) >= 2),
        "candidates": len(rows),
        "passes": len(passes),
        "rows": rows,
    }
    jpath = os.path.join(out_dir, f"multivenue-scan-{stamp}.json")
    with open(jpath, "w") as f:
        json.dump(payload, f, indent=2)

    print()
    print("venue status:")
    for v, s in venue_status.items():
        print(f"  {v}: {'OK' if s.get('ok') else 'UNAVAILABLE'} "
              f"({s.get('coins', 0)} coins)"
              + (f" — {s['error']}" if s.get("error") else "")
              + (f" — includes {', '.join(s['venues_included'])}"
                 if s.get("venues_included") else ""))
    print()
    print(f"{'coin':<8} {'long':<12} {'short':<12} {'long APR %':>10} "
          f"{'short APR %':>11} {'net APR %':>10} {'verdict':<6}")
    for r in rows:
        print(f"{r['coin']:<8} {r['long_venue']:<12} {r['short_venue']:<12} "
              f"{r['long_apr_pct']:>+10.1f} {r['short_apr_pct']:>+11.1f} "
              f"{r['net_apr_pct']:>+10.1f} {r['verdict']:<6}")
    print()
    print(f"candidates: {len(rows)} — PASS: {len(passes)}")
    for r in rows:
        if r["reject_reasons"] or r["history_note"]:
            print(f"  {r['coin']}: {'; '.join(r['reject_reasons'])}"
                  + (f" | {r['history_note']}" if r["history_note"] else ""))
    print("wrote", jpath)


if __name__ == "__main__":
    main()
