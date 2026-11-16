#!/usr/bin/env python3
"""Cross-exchange funding-spread matcher (v0.3) for the perpetual-arbitrage
skill. Detection only — no orders, no wallet, no trading.

Match the SAME asset across venues, compare the numbers, and flag the
spread. Matching is exact after symbol normalization (the matched key
is a coin symbol), and the number compared is the funding APR instead
of a market price:

  match:   HL coin "BTC"  ==  OKX "BTC-USDT-SWAP"  ==  Binance "BTCUSDT"
           (normalize by stripping "-USDT-SWAP" / "USDT" suffixes)
  spread:  other_venue_apr - hl_apr, in percentage points
  flag:    |spread| > 10pp is a candidate (venue-specific funding dislocation)
  confidence: 1.0 for an exact normalized-symbol match; a coin listed on
           only one venue has no match and is skipped (never guessed).

Venues (public endpoints, no keys):
  Hyperliquid  metaAndAssetCtxs (always works from this VM)
  OKX          /api/v5/public/funding-rate?instId=COIN-USDT-SWAP (works)
  Binance      /fapi/v1/premiumIndex (HTTP 451 geo-block from this VM)
  Bybit        /v5/market/tickers?category=linear (HTTP 403 from this VM)
Binance/Bybit are attempted best-effort and skipped gracefully on
geo-block — a blocked venue never crashes the run and never produces a
fake "no spread" row. Only coins actually matched on a venue are
reported for that venue.

Scope note: OKX funding is fetched per instrument, so this script only
checks the interesting set — HL coins with funding APR < -10% (negative,
entry-relevant) or > +30% (genuinely extreme positive), plus the majors
(BTC, ETH, HYPE, SOL) — not all 235 HL perps. The +10.95% APR that many
HL perps sit at is the funding floor (0.00125%/h), not a signal, so it is
deliberately NOT in the interesting set. A spread on a coin outside that
set is not covered; the full-universe scan is scripts/scan.py's job.

Outputs:
  results/cross-venue-YYYY-MM-DD-HHMM.json  (matches + per-venue APRs)
  (table printed to stdout)

Usage:
  python3 cross_venue.py [--min-spread-pp 10] [--out-dir ../results]
Stdlib only, no keys, no trading.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import hl_post, okx_funding  # noqa: E402

HOUR = 3600_000
MAJORS = ["BTC", "ETH", "HYPE", "SOL"]


def normalize_symbol(sym):
    """Strip venue suffixes so 'BTC-USDT-SWAP', 'BTCUSDT', 'BTC' all -> 'BTC'."""
    s = sym.upper()
    for suffix in ("-USDT-SWAP", "-USD-SWAP", "USDT", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[:-len(suffix)]
            break
    return s


def binance_funding_map():
    """{COIN: APR fraction} from Binance premiumIndex, or {} if blocked.

    Binance returns HTTP 451 (geo-block) from this VM. Any failure returns
    an empty map — never a partial or fabricated one.
    """
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "perpetual-arb-xv/0.3"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        if isinstance(data, dict):  # error payload, e.g. {"code": 0, "msg": ...}
            print(f"Binance unavailable ({data.get('msg', 'unknown')[:80]}), skipped",
                  flush=True)
            return {}
        out = {}
        for x in data:
            try:
                out[normalize_symbol(x["symbol"])] = float(x["lastFundingRate"]) * 3 * 365
            except (KeyError, ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f"Binance unavailable ({e}), skipped", flush=True)
        return {}


def bybit_funding_map():
    """{COIN: APR fraction} from Bybit linear tickers, or {} if blocked.

    Bybit returns HTTP 403 from this VM. Any failure returns an empty map.
    """
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "perpetual-arb-xv/0.3"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        if d.get("retCode") != 0:
            print(f"Bybit unavailable ({d.get('retMsg', 'unknown')}), skipped", flush=True)
            return {}
        out = {}
        for x in d["result"]["list"]:
            try:
                out[normalize_symbol(x["symbol"])] = float(x["fundingRate"]) * 3 * 365
            except (KeyError, ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f"Bybit unavailable ({e}), skipped", flush=True)
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-spread-pp", type=float, default=10.0,
                    help="flag |spread| above this many pp as a candidate")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out_dir or os.path.join(here, "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")

    print("fetching Hyperliquid universe ...", flush=True)
    meta, ctxs = hl_post({"type": "metaAndAssetCtxs"})
    hl = {}
    for m, c in zip(meta["universe"], ctxs):
        hl[m["name"]] = float(c["funding"]) * 24 * 365

    interesting = sorted({c for c, a in hl.items() if a < -0.10 or a > 0.30}
                         | set(MAJORS))
    print(f"{len(hl)} HL perps; {len(interesting)} coins of interest "
          f"(APR < -10% or > +30%, + majors)", flush=True)

    print("fetching OKX funding ...", flush=True)
    okx = {}
    for coin in interesting:
        apr = okx_funding(coin)
        if apr is not None:
            okx[coin] = apr
        time.sleep(0.1)
    print(f"OKX matched {len(okx)}/{len(interesting)} coins", flush=True)

    binance = binance_funding_map()
    print(f"Binance matched {len(binance)} coins", flush=True)
    bybit = bybit_funding_map()
    print(f"Bybit matched {len(bybit)} coins", flush=True)

    venues = {"OKX": okx, "Binance": binance, "Bybit": bybit}
    rows = []
    for coin in interesting:
        hl_apr = hl[coin]
        for venue, mapping in venues.items():
            if coin not in mapping:
                continue
            other_apr = mapping[coin]
            spread_pp = (other_apr - hl_apr) * 100
            rows.append({
                "coin": coin,
                "venue": venue,
                "hl_apr_pct": round(hl_apr * 100, 2),
                "venue_apr_pct": round(other_apr * 100, 2),
                "spread_pp": round(spread_pp, 2),
                "match_confidence": 1.0,  # exact normalized-symbol match
                "candidate": abs(spread_pp) > args.min_spread_pp,
                # Direction for a funding-spread pair (detection only):
                # long the venue where funding is more negative.
                "long_venue": "Hyperliquid" if hl_apr < other_apr else venue,
            })
    rows.sort(key=lambda r: abs(r["spread_pp"]), reverse=True)
    candidates = [r for r in rows if r["candidate"]]

    payload = {
        "generated_utc": stamp,
        "min_spread_pp": args.min_spread_pp,
        "venues_available": {v: len(m) > 0 for v, m in venues.items()},
        "coins_checked": len(interesting),
        "matches": len(rows),
        "candidates": len(candidates),
        "rows": rows,
    }
    jpath = os.path.join(out_dir, f"cross-venue-{stamp}.json")
    with open(jpath, "w") as f:
        json.dump(payload, f, indent=2)

    print()
    print(f"{'coin':<8} {'venue':<8} {'HL APR %':>9} {'venue APR %':>11} "
          f"{'spread pp':>10} {'candidate':<9}")
    for r in rows:
        print(f"{r['coin']:<8} {r['venue']:<8} {r['hl_apr_pct']:>+9.1f} "
              f"{r['venue_apr_pct']:>+11.1f} {r['spread_pp']:>+10.1f} "
              f"{'YES' if r['candidate'] else '-':<9}")
    print()
    print(f"matches: {len(rows)} — candidates (|spread| > "
          f"{args.min_spread_pp}pp): {len(candidates)}")
    print("wrote", jpath)


if __name__ == "__main__":
    main()
