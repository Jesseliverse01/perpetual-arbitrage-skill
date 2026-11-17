#!/usr/bin/env python3
"""dYdX adapter stub. NOT IMPLEMENTED.

Fill it in via the template in custom_venue.py, then register it in
registry.py (it is already listed there as a stub).

Verified facts only (2026-11-17):
- Testnet app: https://v4.testnet.dydx.exchange (verified HTTP 200).
- Faucet: the app deposit/recharge button sends 300 test USDC
  automatically; the faucet API at https://faucet.v4testnet.dydx.exchange
  (POST /faucet/tokens) sends up to 2,000 test USDC.
- Testnet indexer https://indexer.v4testnet.dydx.exchange returned
  404 from this VM on 2026-11-17. Do not claim it works; the
  funding-rate endpoint is not yet verified from here.
- All perps are USDC-settled. Auth is an Ethereum wallet signature
  (the dYdX Chain address derives from the ETH wallet).
- Testnet chain id: dydx-testnet-4. Gas token DV4TNT on testnet,
  from the same faucet.
- The official testnet host is https://v4.testnet.dydx.exchange
  (the same app URL). The websocket/API base for a client is not
  yet verified from here; fill it in when you verify it.
"""

from venue_base import VenueAdapter, normalize_side


class DydxClient(VenueAdapter):
    """dYdX v4 adapter. Not implemented yet: every method raises."""

    venue_name = "dydx"
    testnet_url = "https://v4.testnet.dydx.exchange"
    mainnet_url = ""
    env_prefix = "DYDX_"

    @classmethod
    def connect(cls, testnet=True):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in scripts/live/dydx_client.py "
            "from the template in custom_venue.py. Verified so far: "
            "testnet app https://v4.testnet.dydx.exchange, faucet API "
            "https://faucet.v4testnet.dydx.exchange, perps USDC-settled, "
            "auth is an ETH wallet signature, chain id dydx-testnet-4."
        )

    def symbol_for(self, coin):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in symbol_for first."
        )

    def get_balance(self):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in get_balance first."
        )

    def get_positions(self):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in get_positions first."
        )

    def get_open_orders(self, symbol=None):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in get_open_orders first."
        )

    def fetch_funding_rate(self, symbol):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in fetch_funding_rate first. "
            "The testnet indexer returned 404 from this VM on 2026-11-17, "
            "so verify the funding endpoint before using it."
        )

    def fetch_mark_price(self, symbol):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in fetch_mark_price first."
        )

    def place_market_order(self, symbol, side, size, dry_run=True,
                           confirm=False):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in place_market_order first."
        )

    def place_limit_order(self, symbol, side, size, price, dry_run=True,
                          confirm=False, time_in_force="GTC"):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in place_limit_order first."
        )

    def place_bracket(self, symbol, side, size, entry_price=None,
                      take_profit=None, stop_loss=None, dry_run=True,
                      confirm=False):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in place_bracket first."
        )

    def cancel_order(self, symbol, order_id, dry_run=True, confirm=False):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in cancel_order first."
        )

    def reduce_only_close(self, symbol, side, size, dry_run=True,
                          confirm=False):
        raise NotImplementedError(
            "dYdX adapter is a stub: fill in reduce_only_close first."
        )


def connect(testnet=True):
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to the class."""
    return DydxClient.connect(testnet=testnet)
