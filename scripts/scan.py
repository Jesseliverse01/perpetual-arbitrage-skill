#!/usr/bin/env python3
"""Live funding-signal scanner (v0.3) for the perpetual-arbitrage skill.

Fetches all Hyperliquid perps (metaAndAssetCtxs), scores every coin whose
funding APR is below the -10% entry threshold with scripts/score.py, and
prints a ranked table. Also fetches OKX funding (public API, no key) for
the candidates, so the cross-venue dimension is real where OKX lists the
coin. Binance (HTTP 451) and Bybit (403) are geo-blocked from this VM and
are NOT attempted here — see scripts/cross_venue.py for the best-effort
multi-venue matcher.

Liquidity filter: a coin is only a candidate if its funding is below the
entry threshold. The score itself does the trap filtering — the known
illiquid coins (SKR, MELANIA, CC, BIGTIME, NIL, INIT, SOPH, PONS,
USELESS) print extreme funding on tiny books, so their liquidity
dimension scores ~0 and the liquidity gate in score.py caps their total
at 4.0, below the default --min-score 6.

Verdicts (score scale 0-10):
  >= 6   PASS   — worth the full multi-agent evaluation (SKILL.md §2)
  4–<6   WATCH  — real signal but weak on some dimension; monitor only
  < 4    SKIP   — do not evaluate further

Outputs:
  results/scan-YYYY-MM-DD-HHMM.json  (full rows + summary)
  results/scan-YYYY-MM-DD-HHMM.md    (short markdown summary, same name)

Usage:
  python3 scan.py [--min-apr -0.10] [--min-score 6] [--no-okx]
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
from score import score_signal  # noqa: E402

HL_API = "https://api.hyperliquid.xyz/info"
OKX_API = "https://www.okx.com/api/v5/public/funding-rate"
HOUR = 3600_000
REFERENCE_COINS = ["BTC", "ETH", "HYPE"]


def hl_post(payload, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                HL_API, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2)
                continue
            raise
    raise RuntimeError("Hyperliquid request failed after retries")


def okx_funding(coin):
    """OKX funding APR (fraction) for COIN-USDT-SWAP, or None if the coin
    is not listed / the request fails. Public endpoint, no key.
    Supports scale aliases (kPEPE -> PEPE, 1000PEPE -> PEPE)."""
    candidates = [coin]
    if coin.startswith("k") and len(coin) > 1:
        candidates.append(coin[1:])
    elif coin.startswith("1000") and len(coin) > 4:
        candidates.append(coin[4:])
    elif coin.upper().startswith("K") and len(coin) > 1:
        candidates.append(coin[1:])
    for c in candidates:
        url = f"{OKX_API}?instId={c}-USDT-SWAP"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "perpetual-arb-scan/0.3"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
            if d.get("code") == "0" and d.get("data"):
                # OKX funds every 8h: APR = 8h rate * 3 * 365
                return float(d["data"][0]["fundingRate"]) * 3 * 365
        except Exception:
            continue
    return None


def fetch_funding_history(coin, start_ms, end_ms):
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
        if len(rows) > 5000:
            break
    return rows


def fetch_candles(coin, start_ms, end_ms):
    data = hl_post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": start_ms, "endTime": end_ms}})
    out = [{"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
            "l": float(c["l"]), "c": float(c["c"])} for c in data]
    out.sort(key=lambda x: x["t"])
    return out


def verdict(score, min_score):
    if score >= min_score:
        return "PASS"
    if score >= 4.0:
        return "WATCH"
    return "SKIP"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-apr", type=float, default=-0.10,
                    help="entry APR threshold (fraction)")
    ap.add_argument("--min-score", type=float, default=6.0,
                    help="score at/above which a signal is PASS")
    ap.add_argument("--no-okx", action="store_true",
                    help="skip the OKX cross-venue fetch")
    ap.add_argument("--out-dir", default=None,
                    help="results dir (default: ../results next to this script)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out_dir or os.path.join(here, "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    now_ms = int(time.time() * 1000)
    now_ms = (now_ms // HOUR) * HOUR
    stamp = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d-%H%M")

    print("fetching Hyperliquid universe ...", flush=True)
    meta, ctxs = hl_post({"type": "metaAndAssetCtxs"})
    universe = {}
    for m, c in zip(meta["universe"], ctxs):
        coin = m["name"]
        apr = float(c["funding"]) * 24 * 365
        mark = float(c["markPx"])
        universe[coin] = {
            "coin": coin, "apr": apr, "mark": mark,
            "volume_usd": float(c["dayNtlVlm"]),
            "oi_usd": float(c["openInterest"]) * mark,
        }
    print(f"{len(universe)} perps; "
          f"{sum(1 for u in universe.values() if u['apr'] < args.min_apr)} "
          f"below {args.min_apr * 100:.0f}% APR", flush=True)

    candidates = [c for c, u in universe.items() if u["apr"] < args.min_apr]
    for ref in REFERENCE_COINS:
        if ref in universe and ref not in candidates:
            candidates.append(ref)
    candidates.sort(key=lambda c: universe[c]["apr"])

    rows = []
    for coin in candidates:
        u = universe[coin]
        print(f"[{coin}] APR {u['apr'] * 100:+.1f}% — fetching 24h history ...",
              flush=True)
        try:
            hist = fetch_funding_history(coin, now_ms - 25 * HOUR, now_ms)
            apr_hist = [float(r["fundingRate"]) * 24 * 365 for r in hist]
        except Exception as e:
            print(f"[{coin}] funding history failed: {e}", flush=True)
            apr_hist = []
        try:
            candles = fetch_candles(coin, now_ms - 48 * HOUR, now_ms)
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
        except Exception as e:
            print(f"[{coin}] candles failed: {e}", flush=True)
            closes, highs, lows = [], [], []
        other_apr = None
        if not args.no_okx:
            other_apr = okx_funding(coin)
            time.sleep(0.1)
        res = score_signal({
            "apr": u["apr"], "apr_history": apr_hist, "closes": closes,
            "highs": highs, "lows": lows, "volume_usd": u["volume_usd"],
            "oi_usd": u["oi_usd"], "other_apr": other_apr,
        })
        rows.append({
            "coin": coin, "apr_pct": round(u["apr"] * 100, 2),
            "mark": u["mark"], "volume_usd": round(u["volume_usd"], 2),
            "oi_usd": round(u["oi_usd"], 2),
            "okx_apr_pct": (round(other_apr * 100, 2)
                            if other_apr is not None else None),
            "score": res["score"],
            "dimensions": {k: v["score"] for k, v in res["dimensions"].items()},
            "missing_dimensions": res["missing"],
            "liquidity_gate_applied": res.get("liquidity_gate_applied", False),
            "verdict": verdict(res["score"], args.min_score),
        })
        time.sleep(0.15)

    rows.sort(key=lambda r: r["score"], reverse=True)
    passes = [r for r in rows if r["verdict"] == "PASS"]
    summary = {
        "generated_utc": stamp,
        "perps_scanned": len(universe),
        "below_entry_apr": sum(1 for u in universe.values()
                               if u["apr"] < args.min_apr),
        "candidates_scored": len(rows),
        "passes": len(passes),
        "min_apr": args.min_apr, "min_score": args.min_score,
    }
    payload = {"summary": summary, "rows": rows}

    jpath = os.path.join(out_dir, f"scan-{stamp}.json")
    with open(jpath, "w") as f:
        json.dump(payload, f, indent=2)

    lines = [f"# Funding Signal Scan — {stamp} UTC", "",
             f"Scanned {summary['perps_scanned']} Hyperliquid perps; "
             f"{summary['below_entry_apr']} below {args.min_apr * 100:.0f}% APR; "
             f"{summary['candidates_scored']} scored; {summary['passes']} PASS.",
             "",
             "| Rank | Coin | APR | Score | Verdict | Vol 24h | OI |",
             "|---:|---|---:|---:|---|---:|---:|",
             ]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['coin']} | {r['apr_pct']:+.1f}% | {r['score']:.2f} "
                     f"| {r['verdict']} | ${r['volume_usd']:,.0f} | "
                     f"${r['oi_usd']:,.0f} |")
    lines += ["",
              "Score = 0-10 (funding extremity 30%, persistence 20%, trend 15%, "
              "liquidity 15%, volatility 10%, cross-venue 10%). PASS >= "
              f"{args.min_score}. Full dimension breakdown in `{os.path.basename(jpath)}`."]
    mpath = os.path.join(out_dir, f"scan-{stamp}.md")
    with open(mpath, "w") as f:
        f.write("\n".join(lines) + "\n")

    print()
    print(f"{'rank':>4} {'coin':<8} {'APR %':>8} {'score':>6} {'verdict':<6} "
          f"{'vol24h $':>14} {'OI $':>14}")
    for i, r in enumerate(rows, 1):
        print(f"{i:>4} {r['coin']:<8} {r['apr_pct']:>+8.1f} {r['score']:>6.2f} "
              f"{r['verdict']:<6} {r['volume_usd']:>14,.0f} {r['oi_usd']:>14,.0f}")
    print()
    print(f"PASS: {len(passes)} — "
          + (", ".join(p['coin'] for p in passes) if passes else "none"))
    print("wrote", jpath)
    print("wrote", mpath)


if __name__ == "__main__":
    main()
