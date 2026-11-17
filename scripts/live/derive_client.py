#!/usr/bin/env python3
"""Derive adapter stub. NOT IMPLEMENTED.

Fill it in via the template in custom_venue.py, then register it in
registry.py (it is already listed there as a stub).

Verified facts only (2026-11-17):
- Testnet app: https://testnet.derive.xyz (verified HTTP 200,
  redirects to /trade/options).
- USDC faucet in the dashboard. Trading is gasless (off-chain signed
  orders); deposit/withdraw needs gas (ETH, OP Stack L2).
- Demo API host: api-demo.lyra.finance. That host answers (it
  returned 530 for /public/get_instruments on 2026-11-17, so the
  host exists but that path is not verified). Do not present any
  Derive API path as verified until you check it yourself.
- Auth is via EIP-712 signed messages / session keys.
- Derive runs perps as well as options, and the testnet dashboard
  is the place to claim test USDC.
"""

from venue_base import VenueAdapter, normalize_side


class DeriveClient(VenueAdapter):
    """Derive adapter. Not implemented yet: every method raises."""

    venue_name = "derive"
    testnet_url = "https://testnet.derive.xyz"
    mainnet_url = ""
    env_prefix = "DERIVE_"

    @classmethod
    def connect(cls, testnet=True):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in scripts/live/derive_client.py "
            "from the template in custom_venue.py. Verified so far: "
            "testnet app https://testnet.derive.xyz, USDC faucet in the "
            "dashboard, trading is gasless, demo API host "
            "api-demo.lyra.finance, auth via EIP-712 / session keys."
        )

    def symbol_for(self, coin):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in symbol_for first."
        )

    def get_balance(self):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in get_balance first."
        )

    def get_positions(self):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in get_positions first."
        )

    def get_open_orders(self, symbol=None):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in get_open_orders first."
        )

    def fetch_funding_rate(self, symbol):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in fetch_funding_rate first."
        )

    def fetch_mark_price(self, symbol):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in fetch_mark_price first."
        )

    def place_market_order(self, symbol, side, size, dry_run=True,
                           confirm=False):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in place_market_order first."
        )

    def place_limit_order(self, symbol, side, size, price, dry_run=True,
                          confirm=False, time_in_force="GTC"):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in place_limit_order first."
        )

    def place_bracket(self, symbol, side, size, entry_price=None,
                      take_profit=None, stop_loss=None, dry_run=True,
                      confirm=False):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in place_bracket first."
        )

    def cancel_order(self, symbol, order_id, dry_run=True, confirm=False):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in cancel_order first."
        )

    def reduce_only_close(self, symbol, side, size, dry_run=True,
                          confirm=False):
        raise NotImplementedError(
            "Derive adapter is a stub: fill in reduce_only_close first."
        )


def connect(testnet=True):
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to the class."""
    return DeriveClient.connect(testnet=testnet)
