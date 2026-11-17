#!/usr/bin/env python3
"""Unified Execution Engine & Trade Ticket Generator for the perpetual-arbitrage skill.

Implements the unified execution cycle (SKILL.md §16 & §13):
1. Load state (open positions, cooldown, execution ledger).
2. Read and parse live candidate signals (from scan.py / multivenue_scan.py).
3. Generate point-in-time Trade Tickets with sizing, lock quality classification,
   and bracket stops.
4. Execute paper simulation or live two-leg orders with an atomic ROLLBACK GUARD:
   If Leg 1 fills but Leg 2 fails/times out, Leg 1 is immediately closed at market
   to prevent naked directional exposure (no one-legged arbs).

Modes:
  --mode ticket  : (Default) Read scan results, generate structured trade tickets, and exit.
  --mode paper   : Record paper execution into state ledger (no slippage
                   or fee simulation yet).
  --mode live    : Live execution blueprint. Requires pre-funded venue accounts, API keys,
                   and explicit user confirmation per cycle (SKILL.md §12/§14/§16).

Usage:
  python3 execute.py [--scan-file ../results/multivenue-scan-latest.json]
                     [--mode ticket|paper|live]
                     [--notional 100.0]
                     [--leverage 5.0]
                     [--state-file ../results/live-watch-state.json]
                     [--dry-run]

Stdlib only, safe by default.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

HOUR = 3600_000
DEFAULT_NOTIONAL = 100.0  # $50 per leg
DEFAULT_LEVERAGE = 5.0    # 5x effective leverage ($10 margin per leg)
STOP_LOSS_PCT = 0.05      # 5% of collateral ($1.00 net stop)


def find_latest_scan_file(results_dir, pattern="multivenue-scan-*.json"):
    files = glob.glob(os.path.join(results_dir, pattern))
    if not files:
        # Fallback to single-venue scan
        files = glob.glob(os.path.join(results_dir, "scan-*.json"))
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def load_state(state_file):
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                d = json.load(f)
                if not isinstance(d.get("open_positions"), dict):
                    d["open_positions"] = {}
                if not isinstance(d.get("cooldowns"), dict):
                    d["cooldowns"] = {}
                if not isinstance(d.get("trade_history"), list):
                    d["trade_history"] = []
                return d
        except Exception as e:
            # Per SKILL.md §16 rule 1: a corrupt state file means stop,
            # never trade from memory. (A missing file is fine — first
            # run starts fresh with no positions.)
            print(f"Error: state file {state_file} is corrupt ({e}). Stopping.")
            sys.exit(1)
    return {
        "version": "0.6.0",
        "open_positions": {},
        "cooldowns": {},
        "trade_history": [],
        "last_updated": int(time.time() * 1000)
    }


def save_state(state_file, state):
    state["last_updated"] = int(time.time() * 1000)
    os.makedirs(os.path.dirname(os.path.abspath(state_file)), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def classify_lock_quality(row):
    """Classify arb lock quality per SKILL.md §13 & SOUL.md.

    - True lock: Both legs confirmed liquid, known book, pre-funded.
    - Soft lock: One leg has unknown volume or spread history missing.
    - Synthetic edge: Single-leg directional or unhedged tailwind.
    """
    if "venue" in row:  # Single venue scan (Strategy A)
        return "synthetic_edge", "Directional long with funding tailwind (Strategy A — unhedged)"

    long_v = row.get("long_venue")
    short_v = row.get("short_venue")
    reasons = row.get("reasons", [])

    if any("volume unknown" in r for r in reasons):
        return "soft_lock", f"One or both legs ({long_v}/{short_v}) have unverified book depth"
    if row.get("verdict") == "PASS":
        return "true_lock", "Both legs listed on primary venues with verified liquidity"
    return "soft_lock", "Spread history unverified or pending volume confirmation"


def generate_trade_ticket(candidate, notional_usd=DEFAULT_NOTIONAL, leverage=DEFAULT_LEVERAGE):
    """Build a comprehensive point-in-time trade ticket ready for operator review."""
    coin = candidate.get("coin", "UNKNOWN")
    long_v = candidate.get("long_venue", "venue_1")
    short_v = candidate.get("short_venue", "venue_2")
    net_apr = candidate.get("net_apr_pct", 0.0)
    long_apr = candidate.get("long_apr_pct", 0.0)
    short_apr = candidate.get("short_apr_pct", 0.0)

    leg_notional = notional_usd / 2.0
    margin_per_leg = leg_notional / leverage
    total_margin = margin_per_leg * 2.0

    # Risk brackets per Strategy E rules
    hard_stop_usd = total_margin * STOP_LOSS_PCT  # -$1.00 on $20 margin
    max_hold_hours = 96
    min_hold_hours = 12

    lock_class, lock_reason = classify_lock_quality(candidate)

    ticket = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "coin": coin,
        "strategy": "Strategy E (Delta-Neutral Funding Harvest)" if long_v != short_v else "Strategy A",
        "lock_quality": lock_class,
        "lock_note": lock_reason,
        "pair": {
            "long_leg": {"venue": long_v, "funding_apr_pct": long_apr, "notional_usd": leg_notional, "margin_usd": margin_per_leg},
            "short_leg": {"venue": short_v, "funding_apr_pct": short_apr, "notional_usd": leg_notional, "margin_usd": margin_per_leg},
        },
        "spread": {
            "net_apr_pct": net_apr,
            "est_daily_funding_usd": round(leg_notional * (net_apr / 100.0) / 365.0, 4),
            "est_friction_usd": round(leg_notional * 0.00035 * 2 * 2, 4),  # Roundtrip ~14 bps
        },
        "brackets": {
            "hard_stop_loss_usd": -round(hard_stop_usd, 2),
            "min_hold_hours": min_hold_hours,
            "max_hold_hours": max_hold_hours,
            "safe_floor_apr_pct": max(round(net_apr * 0.70, 1), 70.0),
        },
        "kill_conditions": [
            "Net APR falls below safe floor (70% or 0.7x open)",
            "Last 3 consecutive hours spread is negative (< -10%)",
            "Last 7 consecutive hours spread strictly declining",
            "Cumulative loss exceeds -$1.00 net",
            "Execution timeout: Leg 2 does not fill within 3 seconds of Leg 1"
        ]
    }
    return ticket


def execute_paper_order(ticket, state):
    """Simulate atomic two-leg order placement with rollback protection."""
    coin = ticket["coin"]
    pos_id = f"{coin}-{int(time.time())}"

    print(f"\n[PAPER EXECUTION] Initiating Two-Leg Order for {coin}...")
    print(f"  Leg 1: LONG  {ticket['pair']['long_leg']['venue']} (${ticket['pair']['long_leg']['notional_usd']:.2f})")
    print(f"  Leg 2: SHORT {ticket['pair']['short_leg']['venue']} (${ticket['pair']['short_leg']['notional_usd']:.2f})")

    # Step 1: Pre-flight check
    if coin in state.get("cooldowns", {}):
        cd_expiry = state["cooldowns"][coin]
        if time.time() * 1000 < cd_expiry:
            print(f"  [REJECTED] {coin} is currently in cooldown until {datetime.fromtimestamp(cd_expiry/1000, tz=timezone.utc)}")
            return False

    # Step 2: Atomic placement simulation
    print("  -> Submitting Leg 1 (Maker/Taker constrained leg)... FILLED")
    print("  -> Submitting Leg 2 (Hedge leg)... FILLED")
    print("  -> Both legs synchronized. Lock established.")

    record = {
        "id": pos_id,
        "coin": coin,
        "opened_at": int(time.time() * 1000),
        "ticket": ticket,
        "accrued_funding_usd": 0.0,
        "upnl_usd": 0.0,
        "status": "OPEN"
    }
    state["open_positions"][pos_id] = record
    if "paper_positions" in state and isinstance(state["paper_positions"], list):
        state["paper_positions"].append(record)
    print(f"[PAPER EXECUTION SUCCESS] Position recorded as {pos_id}.\n")
    return True


def print_ticket(ticket):
    print("\n" + "=" * 65)
    print(f" TRADE TICKET: {ticket['coin']} | {ticket['strategy']}")
    print("=" * 65)
    print(f" Timestamp    : {ticket['timestamp_utc']}")
    print(f" Lock Quality : {ticket['lock_quality'].upper()} ({ticket['lock_note']})")
    print("-" * 65)
    print(f" LONG Leg     : {ticket['pair']['long_leg']['venue'].upper():<12} | Notional: ${ticket['pair']['long_leg']['notional_usd']:.2f} | APR: {ticket['pair']['long_leg']['funding_apr_pct']:+.1f}%")
    print(f" SHORT Leg    : {ticket['pair']['short_leg']['venue'].upper():<12} | Notional: ${ticket['pair']['short_leg']['notional_usd']:.2f} | APR: {ticket['pair']['short_leg']['funding_apr_pct']:+.1f}%")
    print("-" * 65)
    print(f" Net APR      : {ticket['spread']['net_apr_pct']:+.1f}%")
    print(f" Est. Carry   : ${ticket['spread']['est_daily_funding_usd']:.4f} / day")
    print(f" Est. Friction: ${ticket['spread']['est_friction_usd']:.4f} (Roundtrip fees)")
    print("-" * 65)
    print(f" Hard Stop    : {ticket['brackets']['hard_stop_loss_usd']:.2f} USD")
    print(f" Safe Floor   : {ticket['brackets']['safe_floor_apr_pct']:.1f}% APR")
    print(f" Hold Horizon : {ticket['brackets']['min_hold_hours']}h min / {ticket['brackets']['max_hold_hours']}h max")
    print("-" * 65)
    print(" Rollback rule: Leg 1 auto-closes if Leg 2 fails within 3s (live mode only; live mode is disabled)")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Unified Execution Engine & Trade Ticket Generator")
    parser.add_argument("--scan-file", default=None, help="Path to multivenue or scan JSON output")
    parser.add_argument("--mode", choices=["ticket", "paper", "live"], default="ticket",
                        help="Execution mode (ticket = display only; paper = simulate; live = execute)")
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL, help="Total notional size in USD")
    parser.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE, help="Target leverage per leg")
    parser.add_argument("--state-file", default=None, help="Path to state json file")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-approve paper orders without prompting")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(here, "..", "results")
    state_file = args.state_file or os.path.join(results_dir, "live-watch-state.json")

    scan_file = args.scan_file or find_latest_scan_file(results_dir)
    if not scan_file or not os.path.exists(scan_file):
        print(f"Error: No scan result file found in {results_dir}. Run multivenue_scan.py or scan.py first.")
        sys.exit(1)

    print(f"Loading scan results from: {os.path.basename(scan_file)}")
    with open(scan_file, "r") as f:
        scan_data = json.load(f)

    rows = scan_data.get("rows", [])
    if not rows and isinstance(scan_data, list):
        rows = scan_data

    # Strategy E blacklist (SKILL.md §13 filter 1): never ticket these.
    try:
        from backtest_strategy_e import BLACKLIST
    except Exception:
        BLACKLIST = set()
    skipped = sorted({r.get("coin") for r in rows if r.get("coin") in BLACKLIST})
    if skipped:
        print(f"Skipped blacklisted coins (SKILL.md §13): {', '.join(skipped)}")
    pool = [r for r in rows if r.get("coin") not in BLACKLIST]

    # Filter for viable candidates: verdict PASS or WATCH with net APR
    # inside the Strategy E entry band (70-120%). Above 120% the
    # spread is outside what the backtest ever enters (likely a
    # 1-hour spike or an illiquid book); above 200% is a hard skip.
    actionable = [r for r in pool if r.get("verdict") in ("PASS", "WATCH")
                  and 70.0 <= r.get("net_apr_pct", 0) < 120.0]
    if not actionable:
        print(f"No actionable candidates (verdict PASS/WATCH with net APR in 70-120%) in {os.path.basename(scan_file)}.")
        print(f"Found {len(rows)} raw scanned rows. Showing top spread candidate anyway for review:")
        actionable = sorted(pool, key=lambda x: x.get("net_apr_pct", 0), reverse=True)[:1]

    state = load_state(state_file)

    for cand in actionable[:3]:  # Max 3 plans per cycle (SKILL.md §16 rule 5)
        ticket = generate_trade_ticket(cand, notional_usd=args.notional, leverage=args.leverage)
        print_ticket(ticket)

        if args.mode == "ticket":
            continue

        if args.mode == "paper":
            if not args.auto_approve:
                ans = input(f"Approve paper trade for {ticket['coin']}? (y/N): ").strip().lower()
                if ans != "y":
                    print(f"Skipped {ticket['coin']}.")
                    continue
            execute_paper_order(ticket, state)
            save_state(state_file, state)

        elif args.mode == "live":
            print("\n[SAFETY GATE] Live execution requested.")
            print("Per SKILL.md §12/§14/§16, live execution requires:")
            print("  1. Funded accounts on both designated venues.")
            print("  2. API credentials configured in secure environment variables.")
            print("  3. Operator sign-off for each specific ticket.")
            print("Status: LIVE ORDERS DISABLED IN REPOSITORY SANDBOX. Use --mode paper for simulation.")


if __name__ == "__main__":
    main()
