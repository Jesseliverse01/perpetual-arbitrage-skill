#!/usr/bin/env python3
"""0-10 signal scorer for the perpetual-arbitrage skill (v0.3).

Scores a funding-harvest entry signal (Strategy A: long when funding APR is
deeply negative) on six dimensions. Pure functions, stdlib only, no network,
no keys. Every input must be computable from data available AT DECISION
TIME — no lookahead anywhere.

Weights (full 6-dimension set):
  funding_extremity   30%   how deeply negative the funding APR is now
  persistence         20%   how much of the last 24h funding was < -10% APR
  trend_alignment     15%   price vs its 24h average (recovery in progress?)
  liquidity           15%   24h notional volume AND open interest (min of both)
  volatility          10%   LOW volatility scores HIGH (survive to the flip)
  cross_venue_spread  10%   HL funding vs another venue (OKX) in pp

Missing dimensions are DROPPED and the remaining weights are renormalized,
so a score is always a weighted average over the dimensions actually
available. The result dict lists which dimensions were used. In particular:

- Live scan (scripts/scan.py): all 6 dimensions when OKX data exists;
  cross_venue_spread is dropped for coins OKX does not list.
- Backtest (scripts/backtest_funding.py --min-score): only the 4 dimensions
  computable from Hyperliquid history (extremity, persistence, trend,
  volatility). Liquidity and cross-venue need data that does not exist
  historically, so they are not used there. The threshold (--min-score 6)
  is applied to the same 0-10 scale in both cases.

Backtest note (2026-11-17 audit): because liquidity is dropped in
backtest mode, the liquidity gate above never applies there — the scored
backtest cannot validate the trap filter. Treat the trap filter as
unvalidated for illiquid coins until a scored run on trap coins exists.

Why these weights (justification):
- Extremity (30%) is the entry condition itself — without it there is no
  signal at all, so it carries the most weight.
- Persistence (20%) is the v0.2 lesson: extreme funding is usually a 1-hour
  spike that normalizes right after entry. A signal that has persisted for
  hours is a different, stronger animal than a single-hour print.
- Trend (15%) and volatility (10%) encode the second v0.2 lesson: at 1x the
  PnL comes from price moves, not funding. A recovering price (above its
  24h average) and calm price action (range well inside the -5% stop) are
  what let the trade survive until the funding flip.
- Liquidity (15%) is the trap filter: the known illiquid coins
  (SKR, MELANIA, CC, BIGTIME, NIL, INIT, SOPH, PONS, USELESS) print
  absurd funding (-100% to -4000% APR) on tiny books. Their extremity
  score is 10, but their liquidity score is ~0, which caps their total.
- Cross-venue spread (10%) is the smallest weight because it is a
  supporting signal, not the trade itself: HL funding far more negative
  than OKX means the pessimism is venue-specific, not market-wide.

Thresholds are documented per dimension below. None of them are
backtest-validated on their own — the scored backtest
(results/backtest-2026-11-16-208d-scored.json) is the test of whether
the composite helps. If it does not, the unscored v0.2 setup stays the
default. See SKILL.md "What changed in v0.3".

Usage:
  python3 score.py --apr=-0.183 --apr-history=-0.05,-0.12,-0.18,...
      --closes=1790,1795,1801,... [--highs=...] [--lows=...]
      [--volume=1200000000] [--oi=350000000] [--other-apr=-0.059]
  (Use the `--flag=value` form for lists that start with a minus sign.)
  Prints a JSON object: {"score": 7.3, "dimensions": {...}, ...}
"""
import argparse
import json

WEIGHTS = {
    "funding_extremity": 0.30,
    "persistence": 0.20,
    "trend_alignment": 0.15,
    "liquidity": 0.15,
    "volatility": 0.10,
    "cross_venue_spread": 0.10,
}

# Known illiquid traps: documented here so the liquidity dimension can be
# sanity-checked against them, but the score itself comes only from the
# volume/OI numbers — no hardcoded coin list is used in scoring.
KNOWN_ILLIQUID = {"SKR", "MELANIA", "CC", "BIGTIME", "NIL", "INIT",
                  "SOPH", "PONS", "USELESS"}


def _interp(x, anchors):
    """Piecewise-linear interpolation over (x, y) anchor points, clamped."""
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return anchors[-1][1]


def score_extremity(apr):
    """0-10 from the current funding APR (fraction, e.g. -0.183 = -18.3%).

    Entry needs APR < -10%. Deeper is stronger, saturating at -100% APR —
    beyond that the extra extremity is usually an illiquid-coin artefact.
    """
    if apr is None or apr >= -0.10:
        return 0.0
    return round(_interp(-apr, [(0.10, 0.0), (0.20, 4.0),
                                (0.50, 8.0), (1.00, 10.0)]), 2)


def score_persistence(apr_history):
    """0-10 from the fraction of the last 24h with APR < -10%.

    apr_history: hourly APRs (fractions) up to and including the decision
    hour, oldest first. A 1-hour spike scores ~0.4; funding negative all
    day scores 10.
    """
    if not apr_history:
        return None
    window = apr_history[-24:]
    frac = sum(1 for a in window if a < -0.10) / len(window)
    return round(10.0 * frac, 2)


def score_trend(closes):
    """0-10 from price vs its 24h simple moving average.

    closes: hourly closes up to and including the decision hour, oldest
    first. A price already recovering above its 24h average scores high;
    a price still falling into the dip scores low. Needs >= 6 closes,
    otherwise None (dimension skipped).
    """
    if not closes or len(closes) < 6:
        return None
    window = closes[-25:]  # current close + up to 24 prior
    sma = sum(window[:-1]) / len(window[:-1])
    ret = (window[-1] - sma) / sma
    return round(_interp(ret, [(-0.05, 2.0), (0.0, 5.0),
                               (0.02, 8.0), (0.05, 10.0)]), 2)


def _liquidity_component(usd):
    """0-10 from a single USD figure (24h volume or open interest).

    Log-scale anchors: <$1M -> 0, $10M -> 3, $50M -> 6, $200M -> 8,
    $1B+ -> 10.
    """
    import math
    if usd is None or usd <= 0:
        return 0.0
    lx = math.log10(usd)
    return _interp(lx, [(6.0, 0.0), (7.0, 3.0), (7.7, 6.0),
                        (8.3, 8.0), (9.0, 10.0)])


def score_liquidity(volume_usd, oi_usd):
    """0-10 from 24h notional volume (USD) and open interest (USD).

    The score is the MINIMUM of the two components: a coin needs both
    traded depth and open interest to be genuinely liquid. If either is
    missing, that component scores 0 (which caps the total at 0 for that
    leg) — a missing figure is treated as illiquid, not as neutral.
    """
    if volume_usd is None and oi_usd is None:
        return None
    vol = _liquidity_component(volume_usd)
    oi = _liquidity_component(oi_usd)
    return round(min(vol, oi), 2)


def score_volatility(highs, lows, closes=None):
    """0-10 from the 24h high-low range as % of price. LOW vol scores HIGH.

    The -5% stop must not trigger before the funding flip, so calm price
    action is worth more than wild price action. Needs >= 6 hours of data.
    If highs/lows are not given, falls back to the close-to-close range.
    """
    if highs and lows and len(highs) >= 6 and len(lows) >= 6:
        hi, lo = max(highs[-24:]), min(lows[-24:])
        ref = closes[-1] if closes else (hi + lo) / 2
    elif closes and len(closes) >= 6:
        hi, lo = max(closes[-24:]), min(closes[-24:])
        ref = closes[-1]
    else:
        return None
    rng = (hi - lo) / ref if ref else 0.0
    return round(_interp(rng, [(0.03, 10.0), (0.05, 7.0), (0.08, 4.0),
                               (0.12, 2.0), (0.15, 0.0)]), 2)


def score_cross_venue(hl_apr, other_apr):
    """0-10 from the funding spread vs another venue, in percentage points.

    spread = other_apr - hl_apr (both fractions). For a long on HL, HL
    funding far more negative than elsewhere means venue-specific
    pessimism. Returns None when the other venue has no data (dimension
    skipped, not scored neutral).
    """
    if hl_apr is None or other_apr is None:
        return None
    spread_pp = (other_apr - hl_apr) * 100
    return round(_interp(spread_pp, [(0.0, 0.0), (5.0, 3.0), (10.0, 6.0),
                                     (20.0, 8.0), (30.0, 10.0)]), 2)


def score_signal(features):
    """Score a signal from a features dict. Returns a result dict.

    features (all optional except apr):
      apr            current funding APR (fraction) — required
      apr_history    list of hourly APRs up to decision hour, oldest first
      closes         list of hourly closes up to decision hour, oldest first
      highs, lows    lists of hourly highs/lows (same alignment as closes)
      volume_usd     24h notional volume in USD
      oi_usd         open interest in USD
      other_apr      funding APR on another venue (fraction), same hour

    Result: {"score": 0-10 (weighted avg over available dims),
             "dimensions": {name: {"score": x, "detail": ...}},
             "weights_used": {name: renormalized weight},
             "missing": [dimension names not computable]}
    """
    dims = {}
    dims["funding_extremity"] = score_extremity(features.get("apr"))
    dims["persistence"] = score_persistence(features.get("apr_history"))
    dims["trend_alignment"] = score_trend(features.get("closes"))
    dims["liquidity"] = score_liquidity(features.get("volume_usd"),
                                        features.get("oi_usd"))
    dims["volatility"] = score_volatility(features.get("highs"),
                                          features.get("lows"),
                                          features.get("closes"))
    dims["cross_venue_spread"] = score_cross_venue(features.get("apr"),
                                                    features.get("other_apr"))
    present = {k: v for k, v in dims.items() if v is not None}
    missing = [k for k, v in dims.items() if v is None]
    wsum = sum(WEIGHTS[k] for k in present)
    weights_used = {k: round(WEIGHTS[k] / wsum, 4) for k in present}
    total = sum(present[k] * weights_used[k] for k in present)
    # Liquidity gate: a signal on a book with (effectively) no volume or no
    # open interest is not tradeable, however extreme the funding is. If the
    # liquidity dimension is present and scores 0, the total is capped at
    # 4.0 — below the default --min-score 6, so such a signal can never
    # pass the gate on extremity alone. The cap does not apply when
    # liquidity data is missing (e.g. the backtest, where it is dropped).
    gated = False
    if present.get("liquidity") == 0.0 and total > 4.0:
        total = 4.0
        gated = True
    return {
        "score": round(total, 2),
        "dimensions": {k: {"score": v} for k, v in present.items()},
        "weights_used": weights_used,
        "missing": missing,
        "liquidity_gate_applied": gated,
    }


def _parse_floats(s):
    return [float(x) for x in s.split(",") if x.strip() != ""] if s else None


def main():
    ap = argparse.ArgumentParser(description="Score a funding-harvest signal 0-10.")
    ap.add_argument("--apr", type=float, required=True,
                    help="current funding APR as fraction, e.g. -0.183")
    ap.add_argument("--apr-history", default=None,
                    help="comma-separated hourly APRs (fractions), oldest first")
    ap.add_argument("--closes", default=None,
                    help="comma-separated hourly closes, oldest first")
    ap.add_argument("--highs", default=None, help="comma-separated hourly highs")
    ap.add_argument("--lows", default=None, help="comma-separated hourly lows")
    ap.add_argument("--volume", type=float, default=None,
                    help="24h notional volume in USD")
    ap.add_argument("--oi", type=float, default=None,
                    help="open interest in USD")
    ap.add_argument("--other-apr", type=float, default=None,
                    help="funding APR on another venue (fraction)")
    args = ap.parse_args()
    res = score_signal({
        "apr": args.apr,
        "apr_history": _parse_floats(args.apr_history),
        "closes": _parse_floats(args.closes),
        "highs": _parse_floats(args.highs),
        "lows": _parse_floats(args.lows),
        "volume_usd": args.volume,
        "oi_usd": args.oi,
        "other_apr": args.other_apr,
    })
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
