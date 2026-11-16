#!/usr/bin/env python3
"""Optional: current funding APR on Hyperliquid, via CCXT.

Prints a small table — coin, last price, hourly funding rate, APR.
Hyperliquid only on this VM: Lighter and OKX fail through CCXT here
(see SKILL.md section 15). Stdlib + ccxt; the core scripts stay
stdlib-only and never need this file.

Public data, no key, no orders.

Usage:
    python3 scripts/ccxt_funding.py              # BTC ETH HYPE
    python3 scripts/ccxt_funding.py SOL DOGE     # any coins
"""
import os
import sys

try:
    import ccxt
except ImportError:
    print("ccxt is not installed. Install it with: pip install ccxt")
    print("This script is optional - the core scripts need no installs.")
    sys.exit(2)

COINS = [c.strip().upper() for a in sys.argv[1:] for c in a.split(",")
         if c.strip()] or ["BTC", "ETH", "HYPE"]


def main():
    cfg = {"enableRateLimit": True}
    # CCXT ignores the HTTPS_PROXY env var (trust_env=False), so pass
    # it explicitly when it exists. Read at runtime, never hard-coded.
    if os.environ.get("HTTPS_PROXY"):
        cfg["https_proxy"] = os.environ["HTTPS_PROXY"]
    ex = ccxt.hyperliquid(cfg)
    ex.load_markets()

    print(f"{'Coin':<6} {'Last':>12} {'Funding/h':>14} {'APR':>10}")
    failed = 0
    for coin in COINS:
        symbol = f"{coin}/USDC:USDC"  # Hyperliquid is USDC-margined
        try:
            ticker = ex.fetch_ticker(symbol)
            funding = ex.fetch_funding_rate(symbol)
            rate = funding["fundingRate"]  # hourly rate on Hyperliquid
            apr = rate * 24 * 365
            print(f"{coin:<6} {ticker['last']:>12,.2f} {rate:>14.8f} "
                  f"{apr * 100:>9.2f}%")
        except Exception as e:  # keep going; one bad coin is not fatal
            print(f"{coin:<6} error: {type(e).__name__}: {str(e)[:100]}")
            failed += 1
    print("Hyperliquid only on this VM - Lighter/OKX need the raw "
          "clients (SKILL.md section 15). Public data, no key.")
    return 1 if failed == len(COINS) else 0


if __name__ == "__main__":
    sys.exit(main())
