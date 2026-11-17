#!/usr/bin/env python3
"""Aster adapter stub. NOT IMPLEMENTED.

Fill it in via the template in custom_venue.py, then register it in
registry.py (it is already listed there as a stub).

Verified facts only (2026-11-17):
- The skill's scanner already reads Aster funding live via its
  premiumIndex endpoint (750 coins).
- Testnet futures REST host https://fapi.asterdex-testnet.com
  verified HTTP 200 from this VM: /fapi/v1/ping answers {},
  /fapi/v3/exchangeInfo answers 200 (25KB, U_MARGINED), and
  /fapi/v1/premiumIndex?symbol=BTCUSDT answers 200. The API follows
  the Binance-style futures shape (/fapi/v3/...).
- The official demo script sets the testnet chain id to 714. That
  value comes from a third-party guide citing it; re-check it before
  using it.
- NOT verified from this VM: the testnet app URL
  (https://testnet.aster.finance was unreachable) and the API-wallet
  page (https://asterdex-testnet.com/en/api-wallet was unreachable).
  The faucet (reported elsewhere as 1,000 test USDT + 1,000 test
  ASTER once per day) is NOT verified from here either. Do not
  present any of those as verified; check them yourself first.
- Adapter quirks already documented in SKILL.md section 14: Aster
  has no bracket stopLoss/takeProfit, so place a separate trigger
  order or omit the leg; its positions endpoint returns null funding
  fields, so estimate funding from spread history times held hours.
"""

from venue_base import VenueAdapter, normalize_side


class AsterClient(VenueAdapter):
    """Aster adapter. Not implemented yet: every method raises."""

    venue_name = "aster"
    testnet_url = "https://fapi.asterdex-testnet.com"
    mainnet_url = ""
    env_prefix = "ASTER_"

    @classmethod
    def connect(cls, testnet=True):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in scripts/live/aster_client.py "
            "from the template in custom_venue.py. Verified so far: "
            "testnet futures REST https://fapi.asterdex-testnet.com "
            "(ping, exchangeInfo and premiumIndex all answer 200). "
            "Testnet app URL and faucet are NOT verified; check them "
            "yourself before using them."
        )

    def symbol_for(self, coin):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in symbol_for first."
        )

    def get_balance(self):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in get_balance first."
        )

    def get_positions(self):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in get_positions first."
        )

    def get_open_orders(self, symbol=None):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in get_open_orders first."
        )

    def fetch_funding_rate(self, symbol):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in fetch_funding_rate first. "
            "The scanner already reads Aster funding via premiumIndex, "
            "so that endpoint is the natural source."
        )

    def fetch_mark_price(self, symbol):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in fetch_mark_price first."
        )

    def place_market_order(self, symbol, side, size, dry_run=True,
                           confirm=False):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in place_market_order first."
        )

    def place_limit_order(self, symbol, side, size, price, dry_run=True,
                          confirm=False, time_in_force="GTC"):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in place_limit_order first."
        )

    def place_bracket(self, symbol, side, size, entry_price=None,
                      take_profit=None, stop_loss=None, dry_run=True,
                      confirm=False):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in place_bracket first. "
            "Aster has no bracket stopLoss/takeProfit, so place a "
            "separate trigger order or omit the leg."
        )

    def cancel_order(self, symbol, order_id, dry_run=True, confirm=False):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in cancel_order first."
        )

    def reduce_only_close(self, symbol, side, size, dry_run=True,
                          confirm=False):
        raise NotImplementedError(
            "Aster adapter is a stub: fill in reduce_only_close first."
        )


def connect(testnet=True):
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to the class."""
    return AsterClient.connect(testnet=testnet)
