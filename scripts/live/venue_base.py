#!/usr/bin/env python3
"""Venue adapter interface for the perpetual-arbitrage skill.

One interface every live venue must implement, so the execution
engine (scripts/execute.py --mode live) can run a Strategy E ticket
on any two venues without knowing which ones they are. Hyperliquid
and Lighter implement it for real. dYdX, Aster and Derive ship as
stubs that raise NotImplementedError until someone fills them in.
To add a venue, copy scripts/live/custom_venue.py, fill in the
methods, and register it in scripts/live/registry.py.

The contract, in short
----------------------
- Keys come from the environment only. Each venue has an env prefix
  (HL_, LIGHTER_, DYDX_ and so on). Testnet vars are
  <PREFIX>TESTNET_<KEY>, mainnet vars are <PREFIX><KEY>. Never put a
  key in a file, never paste one in chat, never log one.
- Every write defaults to dry_run=True and touches nothing. A live
  write needs dry_run=False AND confirm=True. One alone is refused
  before any network traffic. There is no other way to place an order.
- Side is always the string "buy" or "sell" ("long" and "short" mean
  the same). Sizes are in base units (for example 0.01 ETH). Prices
  are plain floats in the quote currency.
- fetch_funding_rate returns the funding APR as a float, in percent
  (so -80.0 means shorts pay longs 80 percent a year). Funding rates
  arrive per interval, so convert with APR% = rate * intervals per
  year * 100. For an 8h rate that is rate * 3 * 365 * 100. Forgetting
  the final x100 understates the APR by 100x.
- fetch_mark_price returns the mark price as a float.
- A 401 means stale credentials: stop the cycle, do not retry. A 5xx
  means do NOT auto-retry: the original may have committed on-venue,
  and a retry could double-open. Surface the venue error verbatim.
- If one leg of a two-leg trade fills and the other fails, the filled
  leg is closed at once with a reduce-only market order
  (reduce_only_close). If that close fails, the cycle stops and says
  so loudly. Never leave a one-legged trade.

What each method must do is written on the method itself. A method
that cannot do its job on a venue must raise NotImplementedError
with a plain reason, never silently pretend it worked.
"""

import logging

logger = logging.getLogger(__name__)


def normalize_side(side):
    """Normalize a side to "buy" or "sell".

    Accepts "buy", "sell", "long", "short" (any case), and the booleans
    True (buy) and False (sell), which older Hyperliquid callers used.
    Anything else raises ValueError.
    """
    if isinstance(side, bool):
        return "buy" if side else "sell"
    s = str(side).strip().lower()
    if s in ("buy", "long"):
        return "buy"
    if s in ("sell", "short"):
        return "sell"
    raise ValueError(f"side must be buy/sell (or long/short), got {side!r}")


class VenueAdapter:
    """One live venue account on one network (testnet or mainnet).

    Subclass this and implement every method below. The execution
    engine only ever talks to a venue through this interface, so a
    new venue works the moment its adapter implements it and is
    registered in registry.py.
    """

    # Override in the subclass. venue_name is the lowercase name the
    # scanner and the ticket use (hyperliquid, lighter, dydx, ...).
    # testnet_url / mainnet_url are the API base URLs the adapter
    # connects to. env_prefix builds the credential variable names.
    venue_name = "venue"
    testnet_url = ""
    mainnet_url = ""
    env_prefix = ""

    def __init__(self, testnet=True):
        self.testnet = testnet
        self.network = "testnet" if testnet else "mainnet"

    # -- setup -----------------------------------------------------

    @classmethod
    def connect(cls, testnet=True):
        """Connect and return an adapter for one network.

        Read credentials from the environment only, using this
        venue's env_prefix. Testnet is the default. A client that
        cannot connect (bad host, bad credentials) must raise here,
        before any order is placed. A client without credentials may
        still connect for public reads if the venue allows it, but
        every write must then refuse until credentials are present.
        """
        raise NotImplementedError(
            f"{cls.__name__}.connect is not implemented: fill in the "
            "venue auth (env vars, signing, API host) first."
        )

    def symbol_for(self, coin):
        """The symbol this venue lists for one canonical coin.

        The scanner uses canonical names (1000PEPE for the 1000-form
        meme coins). A venue that lists a different form maps it here:
        Hyperliquid lists kPEPE, Lighter lists 1000PEPE as-is. The
        default returns the coin unchanged.
        """
        return coin

    # -- reads -----------------------------------------------------

    def get_balance(self):
        """Account balances. Return a dict with at least the usable
        USDC (or quote) balance. Keys are venue-specific; keep the
        balance fields in USD and name them plainly."""
        raise NotImplementedError(
            f"{self.venue_name}.get_balance is not implemented."
        )

    def get_positions(self):
        """Open positions as a list of dicts. Each dict carries the
        symbol, the side ("long" or "short"), the size in base units,
        and the entry price. Include funding accrued when the venue
        reports it."""
        raise NotImplementedError(
            f"{self.venue_name}.get_positions is not implemented."
        )

    def get_open_orders(self, symbol=None):
        """Outstanding orders as a list of dicts, optionally scoped to
        one symbol. Each dict carries the venue order id, the symbol,
        the side, the size, and the price, so the SL/TP audit and the
        default cancel-all can find every resting order."""
        raise NotImplementedError(
            f"{self.venue_name}.get_open_orders is not implemented."
        )

    def fetch_funding_rate(self, symbol):
        """Current funding APR for one symbol, as a float in percent.

        Positive means longs pay shorts; negative means shorts pay
        longs. Convert the venue's per-interval rate with
        APR% = rate * intervals per year * 100 (for an 8h rate, that
        is rate * 3 * 365 * 100). Raise when the venue has no funding
        row for the symbol."""
        raise NotImplementedError(
            f"{self.venue_name}.fetch_funding_rate is not implemented."
        )

    def fetch_mark_price(self, symbol):
        """Current mark price for one symbol, as a float. Raise when
        the venue does not list the symbol on this network."""
        raise NotImplementedError(
            f"{self.venue_name}.fetch_mark_price is not implemented."
        )

    # -- writes ----------------------------------------------------
    # Every write defaults to dry_run=True and touches nothing. A live
    # write needs dry_run=False AND confirm=True. Enforce that in the
    # adapter itself, before any network traffic, the way the two
    # built-in adapters do.

    def place_market_order(self, symbol, side, size, dry_run=True,
                           confirm=False):
        """Market order for `size` base units. `side` is "buy" or
        "sell". Dry run: log what would happen and return a simulated
        result. Live: needs confirm=True, returns the venue result."""
        raise NotImplementedError(
            f"{self.venue_name}.place_market_order is not implemented."
        )

    def place_limit_order(self, symbol, side, size, price, dry_run=True,
                          confirm=False, time_in_force="GTC"):
        """Limit order at `price`. `time_in_force` is "GTC" unless the
        venue needs otherwise (IOC and post-only are venue-specific;
        document what yours supports). Same dry-run and confirm rules
        as a market order."""
        raise NotImplementedError(
            f"{self.venue_name}.place_limit_order is not implemented."
        )

    def place_bracket(self, symbol, side, size, entry_price=None,
                      take_profit=None, stop_loss=None, dry_run=True,
                      confirm=False):
        """Bracket: entry plus take-profit and stop-loss triggers.

        Entry is a market order when entry_price is None, else a limit
        order at entry_price. The TP and SL legs sit on the opposite
        side of the entry, reduce-only, same size. When the venue has
        no bracket primitive, place the entry, then place the TP and
        SL as separate trigger orders. Half-leg rule: if the entry
        fills and a bracket leg fails, close the entry at once with a
        reduce-only market order; if that close fails, raise loudly
        and stop the cycle."""
        raise NotImplementedError(
            f"{self.venue_name}.place_bracket is not implemented."
        )

    def cancel_order(self, symbol, order_id, dry_run=True, confirm=False):
        """Cancel one open order by its venue order id. Same dry-run
        and confirm rules as every write."""
        raise NotImplementedError(
            f"{self.venue_name}.cancel_order is not implemented."
        )

    def cancel_all_orders(self, symbol=None, dry_run=True, confirm=False):
        """Cancel every resting order, optionally scoped to one symbol.

        The default loops get_open_orders and cancels each one; a
        venue with a native cancel-all should override it. The
        default needs get_open_orders dicts to carry the symbol and
        the order id under "coin"/"symbol" and
        "oid"/"order_index"/"client_order_index"/"id"."""
        orders = self.get_open_orders(symbol)
        cancelled = []
        for order in orders:
            coin = order.get("coin") or order.get("symbol")
            oid = (order.get("oid") or order.get("order_index")
                   or order.get("client_order_index") or order.get("id"))
            if coin is None or oid is None:
                raise NotImplementedError(
                    f"{self.venue_name} orders do not carry a symbol and "
                    "an order id, so the default cancel-all cannot run. "
                    "Override cancel_all_orders in the adapter."
                )
            cancelled.append(
                self.cancel_order(coin, oid, dry_run=dry_run, confirm=confirm)
            )
        return {"dry_run": dry_run, "cancelled": cancelled}

    def reduce_only_close(self, symbol, side, size, dry_run=True,
                          confirm=False):
        """Reduce-only market close of `size` base units. `side` is
        the side of the leg being closed: a long closes with a sell,
        a short with a buy. This is the half-leg rollback primitive.
        If it fails, surface it and stop the cycle."""
        raise NotImplementedError(
            f"{self.venue_name}.reduce_only_close is not implemented."
        )

    def rollback_close(self, symbol, size=None, dry_run=True, confirm=False):
        """Emergency unwind: close the open position on `symbol`.

        The default finds the position through get_positions, works
        out its side, and closes it with reduce_only_close. A venue
        whose close needs no side (Hyperliquid closes the position
        outright) overrides this with its own primitive. `size` closes
        part of a position when given; None closes the whole thing."""
        positions = self.get_positions()
        match = None
        for pos in positions:
            coin = pos.get("coin") or pos.get("symbol")
            if coin is not None and str(coin).upper() == str(symbol).upper():
                match = pos
                break
        if match is None:
            raise NotImplementedError(
                f"{self.venue_name} has no open position on {symbol} to "
                "roll back (or get_positions does not report one)."
            )
        side = match.get("side")
        if side is None:
            size_field = match.get("size", 0)
            side = "long" if float(size_field) > 0 else "short"
        close_size = size if size is not None else abs(float(match.get("size", 0)))
        if close_size <= 0:
            raise NotImplementedError(
                f"{self.venue_name} position on {symbol} has no size to close."
            )
        return self.reduce_only_close(symbol, side, close_size,
                                      dry_run=dry_run, confirm=confirm)

    # -- helpers ---------------------------------------------------

    def require_live(self, dry_run, confirm, action):
        """Shared live gate. Returns True when the call is a dry run
        (simulate it). Raises ValueError when a live call arrives
        without confirm=True. Adapters may use this instead of their
        own guard; the two built-in adapters keep theirs."""
        if dry_run:
            return True
        if not confirm:
            raise ValueError(
                f"live {action} refused: pass dry_run=False AND "
                "confirm=True. Nothing was sent."
            )
        return False
