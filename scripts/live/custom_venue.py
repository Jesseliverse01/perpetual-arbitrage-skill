#!/usr/bin/env python3
"""Template adapter: copy this file to add your own venue.

How to add a venue
------------------
1. Copy this file to scripts/live/<venue>_client.py, where <venue>
   is the lowercase name the scanner uses (for example
   dydx_client.py). Rename the class to <Venue>Client.
2. Set venue_name, testnet_url, mainnet_url and env_prefix below.
   Credentials follow the same shape as every other venue:
   <PREFIX>TESTNET_<KEY> on testnet, <PREFIX><KEY> on mainnet.
   Never put a key in this file, never paste one in chat, never log
   one. Read them from os.environ only.
3. Fill in every method that raises NotImplementedError. Each one
   carries a comment saying what it needs. Delete nothing; a method
   you leave raising simply refuses that operation with a plain
   reason, which is safer than a silent half-implementation.
4. Register the venue in scripts/live/registry.py (module name and
   env prefix) and add its SDK to requirements-live.txt when it has
   one. The core skill stays stdlib-only; only scripts/live/ may
   need extra packages.
5. Test on testnet first, with throwaway keys and a tiny size:
   connect, read the mark price and the funding rate, place a dry
   run, then one small live order, then a bracket, then a cancel,
   then a reduce-only close. Watch every step on the venue itself.
   Only after a full cycle works on testnet should mainnet even be
   discussed, and mainnet still needs --mainnet plus a yes per
   ticket.

The rules every adapter follows (SKILL.md section 14)
------------------------------------------------------
- Every write defaults to dry_run=True and touches nothing. A live
  write needs dry_run=False AND confirm=True. Enforce that before
  any network traffic.
- A 401 means stale credentials: stop the cycle, do not retry. A 5xx
  means do NOT auto-retry: the original may have committed on-venue.
- If one leg of a two-leg trade fills and the other fails, the filled
  leg is closed at once with a reduce-only market order. If that
  close fails, stop the cycle loudly. Never leave a one-legged trade.
- Side is always the string "buy" or "sell". Sizes are in base
  units. Prices are plain floats in the quote currency.
  fetch_funding_rate returns the funding APR as a float in percent:
  APR% = rate * intervals per year * 100 (for an 8h rate, rate * 3 *
  365 * 100; forgetting the final x100 understates the APR by 100x).
"""

import os

from venue_base import VenueAdapter, normalize_side


class CustomVenueAdapter(VenueAdapter):
    """Template. Copy this file, rename the class, fill in the methods."""

    # Fill in: the lowercase venue name the scanner uses.
    venue_name = "custom"
    # Fill in: the API base URLs the adapter connects to.
    testnet_url = ""
    mainnet_url = ""
    # Fill in: the credential env prefix, for example "MYVENUE_".
    # Testnet vars become MYVENUE_TESTNET_PRIVATE_KEY, mainnet vars
    # MYVENUE_PRIVATE_KEY.
    env_prefix = "CUSTOM_"

    @classmethod
    def connect(cls, testnet=True):
        # Fill in: read the venue credentials from os.environ using
        # cls.env_prefix, build the venue SDK/API client, and return
        # an instance. Raise a plain error when a credential is
        # missing, so the run stops before any order is placed. A
        # client without credentials may still connect for public
        # reads when the venue allows it, but every write must then
        # refuse until credentials are present.
        raise NotImplementedError(
            "CustomVenueAdapter.connect is not implemented: copy this "
            "file to <venue>_client.py and fill in the venue auth."
        )

    def symbol_for(self, coin):
        # Fill in: map one canonical scanner coin to the symbol this
        # venue lists. The scanner uses the 1000-form for meme coins
        # (1000PEPE); some venues list the k-form (kPEPE) instead.
        # Return the coin unchanged when the venue lists it as-is.
        return coin

    def get_balance(self):
        # Fill in: return a dict with the usable quote balance, in
        # USD, plus whatever else the venue reports (account value,
        # margin used). Needs the account credential on most venues.
        raise NotImplementedError(
            "CustomVenueAdapter.get_balance is not implemented."
        )

    def get_positions(self):
        # Fill in: return a list of dicts, one per open position,
        # each with the symbol, the side ("long" or "short"), the size
        # in base units, and the entry price. Include funding accrued
        # when the venue reports it.
        raise NotImplementedError(
            "CustomVenueAdapter.get_positions is not implemented."
        )

    def get_open_orders(self, symbol=None):
        # Fill in: return a list of dicts, one per outstanding order,
        # each with the venue order id, the symbol, the side, the size
        # and the price. The SL/TP audit and the default cancel-all
        # read this, so every resting order must appear here.
        raise NotImplementedError(
            "CustomVenueAdapter.get_open_orders is not implemented."
        )

    def fetch_funding_rate(self, symbol):
        # Fill in: return the current funding APR as a float in
        # percent. Positive means longs pay shorts; negative means
        # shorts pay longs. Convert the venue per-interval rate with
        # APR% = rate * intervals per year * 100. Raise when the venue
        # has no funding row for the symbol.
        raise NotImplementedError(
            "CustomVenueAdapter.fetch_funding_rate is not implemented."
        )

    def fetch_mark_price(self, symbol):
        # Fill in: return the current mark price as a float. Raise
        # when the venue does not list the symbol on this network.
        raise NotImplementedError(
            "CustomVenueAdapter.fetch_mark_price is not implemented."
        )

    def place_market_order(self, symbol, side, size, dry_run=True,
                           confirm=False):
        # Fill in: place a market order for `size` base units. Use
        # normalize_side(side) to accept "buy"/"sell" (and "long"/
        # "short"). Dry run: log what would happen and return a
        # simulated result, no venue call. Live: refuse unless
        # confirm=True, then send the order and return the venue
        # result. Map the venue 401/5xx failures to a stop (never an
        # auto-retry).
        raise NotImplementedError(
            "CustomVenueAdapter.place_market_order is not implemented."
        )

    def place_limit_order(self, symbol, side, size, price, dry_run=True,
                          confirm=False, time_in_force="GTC"):
        # Fill in: place a limit order at `price`. Document which time
        # in force values the venue supports (GTC, IOC, post-only)
        # and what each one does there. Same dry-run and confirm rules
        # as a market order. Watch out for venue quirks like an expiry
        # that IOC orders reject.
        raise NotImplementedError(
            "CustomVenueAdapter.place_limit_order is not implemented."
        )

    def place_bracket(self, symbol, side, size, entry_price=None,
                      take_profit=None, stop_loss=None, dry_run=True,
                      confirm=False):
        # Fill in: entry plus take-profit and stop-loss triggers. The
        # entry is a market order when entry_price is None, else a
        # limit order at entry_price. The TP and SL legs sit on the
        # opposite side, reduce-only, same size. When the venue has no
        # bracket primitive, place the entry, then place the TP and SL
        # as separate trigger orders. Half-leg rule: when the entry
        # fills and a bracket leg fails, close the entry at once with
        # a reduce-only market order; when that close fails, raise
        # loudly and stop the cycle.
        raise NotImplementedError(
            "CustomVenueAdapter.place_bracket is not implemented."
        )

    def cancel_order(self, symbol, order_id, dry_run=True, confirm=False):
        # Fill in: cancel one open order by its venue order id. Same
        # dry-run and confirm rules as every write. Note whether the
        # venue cancels by its own order id or by a client order id;
        # the two are not the same on every venue.
        raise NotImplementedError(
            "CustomVenueAdapter.cancel_order is not implemented."
        )

    def cancel_all_orders(self, symbol=None, dry_run=True, confirm=False):
        # Fill in only when the venue has a native cancel-all; the
        # base class default (loop get_open_orders, cancel each one)
        # works otherwise. Delete this method to use the default.
        return super().cancel_all_orders(symbol, dry_run=dry_run,
                                         confirm=confirm)

    def reduce_only_close(self, symbol, side, size, dry_run=True,
                          confirm=False):
        # Fill in: reduce-only market close of `size` base units.
        # `side` is the side of the leg being closed (a long closes
        # with a sell, a short with a buy). This is the half-leg
        # rollback primitive; when it fails, surface it and stop the
        # cycle. It is often a market order with a reduce-only flag.
        raise NotImplementedError(
            "CustomVenueAdapter.reduce_only_close is not implemented."
        )

    def rollback_close(self, symbol, size=None, dry_run=True, confirm=False):
        # Fill in only when the venue close needs no side (it closes
        # the position outright). The base default finds the position
        # through get_positions, works out its side, and calls
        # reduce_only_close. Delete this method to use the default.
        return super().rollback_close(symbol, size=size, dry_run=dry_run,
                                      confirm=confirm)


def connect(testnet=True):
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to the class."""
    return CustomVenueAdapter.connect(testnet=testnet)
