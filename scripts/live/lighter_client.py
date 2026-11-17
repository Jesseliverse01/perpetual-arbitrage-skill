#!/usr/bin/env python3
"""Lighter live-trading adapter for the perpetual-arbitrage skill.

One venue adapter with the same shape as a Hyperliquid adapter, so the
skill's unified execution cycle (SKILL.md §13/§14/§16) can run a
Strategy E leg on Lighter. Stdlib + the official Lighter Python SDK
(`lighter-sdk`, https://github.com/elliottech/lighter-python,
Apache-2.0) only. No raw signing code is implemented here. Every
write goes through `lighter.SignerClient`.

Safety gates (no exceptions):
  * Every write method defaults to ``dry_run=True`` and touches
    nothing on the venue in that mode: it logs what it would do and
    returns a simulated result.
  * A live order needs BOTH ``dry_run=False`` AND ``confirm=True``.
    A live call without ``confirm=True`` raises LighterConfirmError
    and sends nothing.
  * Keys come ONLY from environment variables, never from a file and
    never hard-coded. The API private key (the trading key) is read
    from LIGHTER_TESTNET_PRIVATE_KEY (testnet) or LIGHTER_PRIVATE_KEY
    (mainnet). The L1 ETH private key is NEVER needed for trading; it
    is only used once, passed explicitly to ``register_api_key()``, to
    register a new API key, and it is never stored.
  * No mainnet order is ever placed by this module's demo, and the
    module never touches deposit / withdraw / transfer / leverage.

Venue quirks, from SKILL.md §14 (verified against the official SDK):
  * Lighter ignores ``clientOrderId`` when a bracket is present, so
    bracket legs are matched in reconcile by (baseSymbol, side,
    size). See ``reconcile_key()``. ``cancel_order()`` takes the
    client order index for plain orders; for bracket legs use
    ``find_active_order()`` + ``cancel_all_orders(symbol)``.
  * A write failing with 401 means stale credentials: the cycle
    stops, no retry (LighterAuthError).
  * A write failing with 5xx is NEVER auto-retried: the original may
    already have committed on-venue, so a retry could double-open
    (LighterServerError carries the venue message verbatim).
  * Half-leg risk: if one leg of a two-venue arb fills and the other
    fails, roll back immediately with a reduce-only market close of
    the filled leg. ``reduce_only_close()`` is that primitive. If
    the rollback itself fails, surface it and STOP the cycle.
  * Testnet and mainnet have DIFFERENT market indices (testnet ETH
    perp is 4095; mainnet ETH perp is 0). Market indices are always
    resolved live from ``orderBookDetails``, never hard-coded.
  * Amounts are integers: base_amount = size * 10**size_decimals,
    price = dollars * 10**price_decimals (testnet ETH: 4 and 2).
  * Lighter funding from /api/v1/funding-rates is an 8h-normalized
    rate: APR = rate * 3 * 365 (same conversion the skill's scanner
    already uses).

Environment (all optional except as noted):
  LIGHTER_TESTNET_PRIVATE_KEY / LIGHTER_PRIVATE_KEY
      API private key (hex, with or without 0x). Trading key only.
  LIGHTER_TESTNET_ACCOUNT_INDEX / LIGHTER_ACCOUNT_INDEX
      Lighter account index. Needed for balance / positions / orders.
  LIGHTER_TESTNET_API_KEY_INDEX / LIGHTER_API_KEY_INDEX
      API key slot, default 3 (the official system_setup.py example
      uses 3; never 0, which the web app owns).

Without keys the client still connects and serves public reads
(markets, mark price, funding). Writes without keys raise
LighterNotSignedInError. One-time setup for a fresh account (needs a
funded account first. On testnet that means connecting a wallet in
https://testnet.app.lighter.xyz, which credits $500 test USDC):
``register_api_key(eth_private_key, api_key_index)`` prints the new
API private key once; put it in the env var yourself.

Sources:
  * SDK: https://github.com/elliottech/lighter-python (v1.1.8 verified)
  * Docs: https://docs.lighter.xyz (Get Started / examples)
  * API hosts: https://mainnet.zklighter.elliot.ai,
    https://testnet.zklighter.elliot.ai
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import lighter
from lighter.signer_client import CreateOrderTxReq

from venue_base import VenueAdapter

logger = logging.getLogger("lighter_client")

MAINNET_URL = "https://mainnet.zklighter.elliot.ai"
TESTNET_URL = "https://testnet.zklighter.elliot.ai"

# Funding from /api/v1/funding-rates is an 8h rate (skill-verified).
FUNDING_APR_FACTOR = 3 * 365


class LighterError(Exception):
    """Base error for this adapter."""


class LighterConfigError(LighterError):
    """Missing configuration (env var, market, size below minimum)."""


class LighterAuthError(LighterError):
    """401 from the venue: stale credentials. Stop the cycle, no retry."""


class LighterServerError(LighterError):
    """5xx from the venue: never auto-retry (the order may already exist)."""


class LighterConfirmError(LighterError):
    """A live write was requested without confirm=True. Nothing sent."""


class LighterNotSignedInError(LighterError):
    """A write was requested but no API key is configured in the env."""


@dataclass
class MarketInfo:
    symbol: str
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base_amount: float
    mark_price: float | None = None


class LighterClient(VenueAdapter):
    """Sync wrapper around the official async Lighter SDK.

    Public reads work without keys. Writes are dry-run by default and
    need dry_run=False + confirm=True to touch the venue.
    """

    venue_name = "lighter"
    testnet_url = TESTNET_URL
    mainnet_url = MAINNET_URL
    env_prefix = "LIGHTER_"

    def __init__(self, testnet: bool = False):
        super().__init__(testnet)
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self._env_prefix = "LIGHTER_TESTNET_" if testnet else "LIGHTER_"
        self._loop = asyncio.new_event_loop()
        self._api_client = None
        self._account_api = None
        self._order_api = None
        self._funding_api = None
        self._signer = None
        self._markets: dict[str, MarketInfo] = {}
        self._client_order_index = int(time.time()) % (2**48 - 10)
        self.account_index = self._env_int("ACCOUNT_INDEX")
        self.api_key_index = self._env_int("API_KEY_INDEX", default=3)
        self._api_private_key = os.environ.get(self._env_prefix + "PRIVATE_KEY")

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, testnet: bool = False) -> "LighterClient":
        """Build a client for testnet or mainnet.

        Reads keys ONLY from the environment (see module docstring).
        Without an API private key the client still connects and serves
        public reads; writes raise LighterNotSignedInError.
        """
        self = cls(testnet=testnet)
        try:
            self._run(self._setup())
        except Exception:
            # Don't leak the aiohttp session / loop on a failed connect.
            try:
                if self._api_client is not None:
                    self._run(self._api_client.close())
            except Exception:
                pass
            if not self._loop.is_closed():
                self._loop.close()
            raise
        return self

    async def _setup(self) -> None:
        """Build the SDK clients. Must run on the owned loop: the SDK's
        ApiClient constructor needs a running event loop (aiohttp)."""
        self._api_client = lighter.ApiClient(
            configuration=lighter.Configuration(host=self.base_url)
        )
        self._account_api = lighter.AccountApi(self._api_client)
        self._order_api = lighter.OrderApi(self._api_client)
        self._funding_api = lighter.FundingApi(self._api_client)
        await self._load_markets_async()
        if self._api_private_key:
            if self.account_index is None:
                # No signer yet: dry runs and public reads still work.
                # A live write refuses at _require_signed() until the
                # index is set, so the account index is enforced where
                # it matters (the write), not at connect time.
                logger.warning(
                    "API key is set but %sACCOUNT_INDEX is not. "
                    "Dry runs and public reads still work; a live write "
                    "will refuse until the index is set.",
                    self._env_prefix,
                )
            else:
                self._signer = lighter.SignerClient(
                    url=self.base_url,
                    account_index=self.account_index,
                    api_private_keys={self.api_key_index: self._api_private_key},
                )
                # check_client() fetches /apikeys through the NATIVE signer
                # binary, which does its own HTTP and ignores proxy env vars:
                # on a proxied VM it fails at the TLS handshake even for a
                # valid key. It is advisory only. The first signed write
                # (submitted via aiohttp, which honours the proxy) is the
                # real check. Never let it block a connection.
                err = self._signer.check_client()
                if err is not None:
                    logger.warning("SignerClient check_client() advisory failure (ignored): %s", err)
                logger.info(
                    "Lighter %s connected, account %s, api key index %s",
                    "testnet" if self.testnet else "mainnet",
                    self.account_index,
                    self.api_key_index,
                )
        else:
            logger.warning(
                "No %sPRIVATE_KEY in the environment. Read-only mode. "
                "Writes will raise LighterNotSignedInError.",
                self._env_prefix,
            )

    def close(self) -> None:
        """Close the SDK clients and the owned event loop."""
        if self._signer is not None:
            self._run(self._signer.close())
            self._signer = None
        if self._api_client is not None:
            self._run(self._api_client.close())
            self._api_client = None
        if not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> "LighterClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _env_int(self, name: str, default: int | None = None) -> int | None:
        raw = os.environ.get(self._env_prefix + name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise LighterConfigError(f"{self._env_prefix}{name} must be an integer, got {raw!r}")

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _handle_api_error(self, err: Exception) -> Exception:
        """Map a venue error to the §14 handling: 401 stops, 5xx never retries."""
        status = getattr(err, "status", None)
        body = getattr(err, "body", "") or str(err)
        if status == 401:
            return LighterAuthError(
                f"401 from Lighter: stale credentials. Stop the cycle, do not retry. ({body[:300]})"
            )
        if status is not None and 500 <= status < 600:
            return LighterServerError(
                f"{status} from Lighter, NOT auto-retried. The order may already "
                f"have committed on-venue. Venue message verbatim: {body[:500]}"
            )
        return LighterError(f"Lighter API error (status {status}): {body[:500]}")

    async def _load_markets_async(self) -> None:
        """Resolve every market live; indices differ between environments."""
        try:
            resp = await self._order_api.order_book_details()
        except Exception as err:
            raise self._handle_api_error(err)
        books = getattr(resp, "order_book_details", []) or []
        self._markets = {}
        for book in books:
            if getattr(book, "market_type", "") != "perp":
                continue
            symbol = str(getattr(book, "symbol", "")).upper()
            if not symbol:
                continue
            self._markets[symbol] = MarketInfo(
                symbol=symbol,
                market_id=int(book.market_id),
                size_decimals=int(getattr(book, "supported_size_decimals", 0)),
                price_decimals=int(getattr(book, "supported_price_decimals", 0)),
                min_base_amount=float(getattr(book, "min_base_amount", 0) or 0),
                mark_price=self._to_float(getattr(book, "mark_price", None)),
            )
        if not self._markets:
            raise LighterError(f"No perp markets returned by {self.base_url}/orderBookDetails")

    def refresh_markets(self) -> None:
        """Re-resolve markets live (marks refresh with them)."""
        self._run(self._load_markets_async())

    @staticmethod
    def _to_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _market(self, symbol: str) -> MarketInfo:
        market = self._markets.get(symbol.upper())
        if market is None:
            raise LighterConfigError(
                f"Unknown perp symbol {symbol!r} on {'testnet' if self.testnet else 'mainnet'} "
                f"(known: {', '.join(sorted(self._markets))})"
            )
        return market

    def _next_client_order_index(self) -> int:
        self._client_order_index += 1
        return self._client_order_index

    def _require_signed(self) -> None:
        if self._signer is None:
            raise LighterNotSignedInError(
                f"No {self._env_prefix}PRIVATE_KEY in the environment. "
                "Set the API private key env var to enable writes."
            )

    def _guard_live(self, dry_run: bool, confirm: bool, what: str) -> bool:
        """Return True when the call is a simulation; raise otherwise."""
        if dry_run:
            return True
        if not confirm:
            raise LighterConfirmError(
                f"Live {what} refused: pass dry_run=False AND confirm=True. Nothing was sent."
            )
        self._require_signed()
        return False

    @staticmethod
    def _side_is_ask(side: str) -> bool:
        side = side.lower()
        if side in ("buy", "long"):
            return False
        if side in ("sell", "short"):
            return True
        raise LighterConfigError(f"side must be buy/long or sell/short, got {side!r}")

    @staticmethod
    def _closing_side(side: str) -> str:
        """The order side that closes a leg. Accepts both spellings
        the callers use: "buy"/"sell" is already the closing side,
        while "long"/"short" names the leg being closed (a long closes
        with a sell, a short with a buy)."""
        s = str(side).strip().lower()
        if s in ("buy", "sell"):
            return s
        if s == "long":
            return "sell"
        if s == "short":
            return "buy"
        raise LighterConfigError(
            f"side must be buy/sell (or long/short), got {side!r}"
        )

    def _base_amount(self, market: MarketInfo, size: float) -> int:
        if size <= 0:
            raise LighterConfigError(f"size must be positive, got {size}")
        if size < market.min_base_amount:
            raise LighterConfigError(
                f"size {size} {market.symbol} is below the venue minimum "
                f"{market.min_base_amount} (market {market.market_id})"
            )
        return int(round(size * (10**market.size_decimals)))

    def _price_int(self, market: MarketInfo, price: float) -> int:
        if price <= 0:
            raise LighterConfigError(f"price must be positive, got {price}")
        return int(round(price * (10**market.price_decimals)))

    def _nonce(self):
        return self._signer.nonce_manager.next_nonce(self.api_key_index)

    # ------------------------------------------------------------------
    # reads (public, except active orders which need an auth token)
    # ------------------------------------------------------------------
    def _account(self) -> dict:
        if self.account_index is None:
            raise LighterConfigError(
                f"{self._env_prefix}ACCOUNT_INDEX is not set. Balance, positions "
                "and orders need the Lighter account index."
            )
        try:
            resp = self._run(
                self._account_api.account(by="index", value=str(self.account_index))
            )
        except Exception as err:
            raise self._handle_api_error(err)
        accounts = getattr(resp, "accounts", []) or []
        if not accounts:
            raise LighterError(f"No Lighter account with index {self.account_index}")
        return accounts[0].to_dict()

    def get_balance(self) -> dict:
        """Available + collateral balances (USDC) for the account."""
        acct = self._account()
        assets = {a.get("symbol"): a for a in acct.get("assets", [])}
        usdc = assets.get("USDC", {})
        return {
            "venue": "lighter",
            "testnet": self.testnet,
            "account_index": self.account_index,
            "available_balance_usd": float(acct.get("available_balance") or 0),
            "collateral_usd": float(acct.get("collateral") or 0),
            "total_asset_value_usd": float(acct.get("total_asset_value") or 0),
            "usdc_balance": float(usdc.get("balance") or 0),
            "usdc_locked": float(usdc.get("locked_balance") or 0),
        }

    def get_positions(self) -> list[dict]:
        """Open perp positions. Empty list when flat. Funding accrued is
        per-position ``total_funding_paid_out`` (negative = received)."""
        acct = self._account()
        out = []
        for pos in acct.get("positions", []) or []:
            size = float(pos.get("position") or 0)
            if size == 0:
                continue
            sign = int(pos.get("sign") or 0)
            out.append(
                {
                    "venue": "lighter",
                    "symbol": pos.get("symbol"),
                    "market_id": int(pos.get("market_id")),
                    "side": "long" if sign > 0 else "short",
                    "size": abs(size),
                    "avg_entry_price": float(pos.get("avg_entry_price") or 0),
                    "position_value_usd": float(pos.get("position_value") or 0),
                    "unrealized_pnl_usd": float(pos.get("unrealized_pnl") or 0),
                    "realized_pnl_usd": float(pos.get("realized_pnl") or 0),
                    "liquidation_price": float(pos.get("liquidation_price") or 0),
                    "total_funding_paid_out_usd": self._to_float(
                        pos.get("total_funding_paid_out")
                    ),
                }
            )
        return out

    def _auth_token(self) -> str:
        self._require_signed()
        token, err = self._signer.create_auth_token_with_expiry()
        if err:
            raise LighterAuthError(f"Could not create auth token: {err}")
        return token

    def get_active_orders(self, symbol: str | None = None) -> list[dict]:
        """Resting orders for the account (needs the API key: the auth
        token is signed with it). Bracket legs are matched by
        (symbol, side, size). See reconcile_key()."""
        self._require_signed()
        market_id = self._market(symbol).market_id if symbol else None
        try:
            resp = self._run(
                self._order_api.account_active_orders(
                    authorization=self._auth_token(),
                    account_index=self.account_index,
                    market_id=market_id,
                )
            )
        except Exception as err:
            raise self._handle_api_error(err)
        out = []
        for order in getattr(resp, "orders", []) or []:
            d = order.to_dict()
            out.append(
                {
                    "order_index": int(d.get("order_index")),
                    "client_order_index": int(d.get("client_order_index")),
                    "symbol": self._symbol_for_market(int(d.get("market_index"))),
                    "side": "sell" if d.get("is_ask") else "buy",
                    "size": float(d.get("remaining_base_amount") or 0),
                    "filled_size": float(d.get("filled_base_amount") or 0),
                    "price": float(d.get("price") or 0),
                    "type": d.get("type"),
                    "status": d.get("status"),
                    "reduce_only": bool(d.get("reduce_only")),
                    "trigger_price": float(d.get("trigger_price") or 0),
                    "parent_order_index": int(d.get("parent_order_index") or 0),
                }
            )
        return out

    def symbol_for(self, coin: str) -> str:
        """The symbol Lighter lists for one canonical coin. Lighter
        lists the 1000-form meme coins as-is (1000PEPE), so the coin
        is returned unchanged."""
        return coin

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Outstanding orders (uniform name for the adapter interface).
        Delegates to get_active_orders, which is the original method
        name in this adapter."""
        return self.get_active_orders(symbol)

    def _symbol_for_market(self, market_id: int) -> str:
        for market in self._markets.values():
            if market.market_id == market_id:
                return market.symbol
        return f"market:{market_id}"

    @staticmethod
    def reconcile_key(order: dict) -> tuple:
        """Match key for an order. Lighter ignores clientOrderId when a
        bracket is present (§14), so reconcile on (symbol, side, size),
        never on the client order id alone."""
        return (
            str(order.get("symbol", "")).upper(),
            str(order.get("side", "")).lower(),
            float(order.get("size") or 0),
        )

    def find_active_order(self, symbol: str, side: str, size: float) -> dict | None:
        """Find a resting order by (symbol, side, size), the only
        reliable match for bracket legs. Returns None when absent."""
        want = (symbol.upper(), side.lower(), float(size))
        for order in self.get_active_orders(symbol):
            if self.reconcile_key(order) == want:
                return order
        return None

    def fetch_funding_rate(self, symbol: str, exchange: str = "lighter") -> float:
        """Current funding APR for a symbol, as a float in percent.

        Lighter's /funding-rates rows are 8h-normalized, so
        APR% = rate * 3 * 365 * 100 (the same conversion the skill's
        scanner uses). ``exchange`` defaults to Lighter itself; pass
        "hyperliquid" / "binance" / "bybit" for the cross-venue legs.
        Positive means longs pay shorts; negative means shorts pay
        longs.
        """
        market = self._market(symbol)
        try:
            resp = self._run(self._funding_api.funding_rates())
        except Exception as err:
            raise self._handle_api_error(err)
        for row in getattr(resp, "funding_rates", []) or []:
            if int(row.market_id) != market.market_id:
                continue
            if str(row.exchange).lower() != exchange.lower():
                continue
            return float(row.rate) * FUNDING_APR_FACTOR * 100
        raise LighterError(
            f"No funding row for {market.symbol} (market {market.market_id}) "
            f"on exchange {exchange!r}"
        )

    def fetch_mark_price(self, symbol: str) -> float:
        """Mark price for a perp, as a float. From orderBookDetails
        (live, keyless); falls back to the mid of the top of book."""
        market = self._market(symbol)
        if market.mark_price is not None:
            return market.mark_price
        try:
            resp = self._run(self._order_api.order_books(market_id=market.market_id))
        except Exception as err:
            raise self._handle_api_error(err)
        books = getattr(resp, "order_books", []) or []
        if not books:
            raise LighterError(f"No order book for {market.symbol}")
        book = books[0]
        bids = getattr(book, "bids", []) or []
        asks = getattr(book, "asks", []) or []
        if not bids or not asks:
            raise LighterError(f"Empty book for {market.symbol}")
        return (float(bids[0].price) + float(asks[0].price)) / 2

    # ------------------------------------------------------------------
    # writes: dry_run=True by default; live needs dry_run=False + confirm
    # ------------------------------------------------------------------
    def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        slippage_pct: float = 0.5,
        dry_run: bool = True,
        confirm: bool = False,
        reduce_only: bool = False,
    ) -> dict:
        """Market order. ``avg_execution_price`` is the worst acceptable
        fill: mark * (1 + slippage) for buys, mark * (1 - slippage) for
        sells. Set reduce_only=True only to close a position (rollback)."""
        market = self._market(symbol)
        is_ask = self._side_is_ask(side)
        base_amount = self._base_amount(market, size)
        mark = self.fetch_mark_price(symbol)
        worst = mark * (1 + slippage_pct / 100) if not is_ask else mark * (1 - slippage_pct / 100)
        worst_int = self._price_int(market, worst)
        params = {
            "venue": "lighter",
            "testnet": self.testnet,
            "symbol": market.symbol,
            "market_id": market.market_id,
            "side": "sell" if is_ask else "buy",
            "size": size,
            "base_amount": base_amount,
            "mark_price": mark,
            "worst_price": worst,
            "reduce_only": reduce_only,
        }
        if self._guard_live(dry_run, confirm, f"market {params['side']} {size} {market.symbol}"):
            logger.info("[DRY-RUN] would place market order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        client_order_index = self._next_client_order_index()
        api_key_index, nonce = self._nonce()
        try:
            tx, tx_hash, err = self._run(
                self._signer.create_market_order(
                    market_index=market.market_id,
                    client_order_index=client_order_index,
                    base_amount=base_amount,
                    avg_execution_price=worst_int,
                    is_ask=is_ask,
                    reduce_only=reduce_only,
                    nonce=nonce,
                    api_key_index=api_key_index,
                )
            )
        except Exception as exc:
            raise self._handle_api_error(exc)
        if err is not None:
            raise LighterError(f"Market order rejected by signer: {err}")
        return {
            "dry_run": False,
            "tx_hash": tx_hash,
            "client_order_index": client_order_index,
            "tx": tx.to_dict() if hasattr(tx, "to_dict") else str(tx),
            **params,
        }

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        *,
        time_in_force: str = "GTT",
        post_only: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Limit order. time_in_force: GTT (good-till-time), IOC, or PO
        (post-only)."""
        market = self._market(symbol)
        is_ask = self._side_is_ask(side)
        base_amount = self._base_amount(market, size)
        price_int = self._price_int(market, price)
        tif = {
            "GTT": lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            "IOC": lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            "PO": lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
        }.get(time_in_force.upper())
        if tif is None:
            raise LighterConfigError(f"time_in_force must be GTT, IOC or PO, got {time_in_force!r}")
        if post_only:
            tif = lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY
        # IOC orders need order_expiry 0 (DEFAULT_IOC_EXPIRY); the SDK
        # default (-1, 28-day) is rejected by the signer for IOC with
        # "OrderExpiry is invalid". GTT and PO keep the 28-day default.
        order_expiry = (
            lighter.SignerClient.DEFAULT_IOC_EXPIRY
            if tif == lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            else lighter.SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
        )
        params = {
            "venue": "lighter",
            "testnet": self.testnet,
            "symbol": market.symbol,
            "market_id": market.market_id,
            "side": "sell" if is_ask else "buy",
            "size": size,
            "base_amount": base_amount,
            "price": price,
            "time_in_force": time_in_force.upper(),
            "post_only": post_only,
        }
        if self._guard_live(dry_run, confirm, f"limit {params['side']} {size} {market.symbol} @ {price}"):
            logger.info("[DRY-RUN] would place limit order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        client_order_index = self._next_client_order_index()
        api_key_index, nonce = self._nonce()
        try:
            tx, tx_hash, err = self._run(
                self._signer.create_order(
                    market_index=market.market_id,
                    client_order_index=client_order_index,
                    base_amount=base_amount,
                    price=price_int,
                    is_ask=is_ask,
                    order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
                    time_in_force=tif,
                    reduce_only=False,
                    trigger_price=0,
                    order_expiry=order_expiry,
                    nonce=nonce,
                    api_key_index=api_key_index,
                )
            )
        except Exception as exc:
            raise self._handle_api_error(exc)
        if err is not None:
            raise LighterError(f"Limit order rejected by signer: {err}")
        return {
            "dry_run": False,
            "tx_hash": tx_hash,
            "client_order_index": client_order_index,
            "tx": tx.to_dict() if hasattr(tx, "to_dict") else str(tx),
            **params,
        }

    def place_bracket(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        take_profit: float,
        stop_loss: float,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """One-triggers-a-one-cancels-the-other bracket: an IOC limit
        entry plus a reduce-only stop-loss and take-profit for the whole
        executed size (BaseAmount=0, like the official example). The
        SL/TP limit prices sit 0.2% beyond their triggers so a trigger
        still fills. Reconcile the legs by (symbol, side, size). The
        client order ids are ignored by Lighter on bracket orders.
        Parameter order follows the adapter interface: entry, then
        take-profit, then stop-loss."""
        market = self._market(symbol)
        is_ask = self._side_is_ask(side)
        base_amount = self._base_amount(market, size)
        entry_int = self._price_int(market, entry_price)
        sl_trigger = self._price_int(market, stop_loss)
        tp_trigger = self._price_int(market, take_profit)
        # Limit beyond the trigger, in the direction that still fills.
        offset = 0.002
        if is_ask:
            # Entry is a sell (short): TP/SL are buys, limit above trigger.
            sl_limit = self._price_int(market, stop_loss * (1 + offset))
            tp_limit = self._price_int(market, take_profit * (1 + offset))
        else:
            # Entry is a buy (long): TP/SL are sells, limit below trigger.
            sl_limit = self._price_int(market, stop_loss * (1 - offset))
            tp_limit = self._price_int(market, take_profit * (1 - offset))
        params = {
            "venue": "lighter",
            "testnet": self.testnet,
            "symbol": market.symbol,
            "market_id": market.market_id,
            "side": "sell" if is_ask else "buy",
            "size": size,
            "base_amount": base_amount,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "grouping": "ONE_TRIGGERS_A_ONE_CANCELS_THE_OTHER",
        }
        if self._guard_live(
            dry_run, confirm, f"bracket {params['side']} {size} {market.symbol} @ {entry_price}"
        ):
            logger.info("[DRY-RUN] would place bracket: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        base_cid = self._next_client_order_index()
        entry = CreateOrderTxReq(
            MarketIndex=market.market_id,
            ClientOrderIndex=base_cid,
            BaseAmount=base_amount,
            Price=entry_int,
            IsAsk=int(is_ask),
            Type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            TimeInForce=lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            ReduceOnly=0,
            TriggerPrice=0,
            OrderExpiry=0,
        )
        take_profit_order = CreateOrderTxReq(
            MarketIndex=market.market_id,
            ClientOrderIndex=base_cid + 1,
            BaseAmount=0,
            Price=tp_limit,
            IsAsk=int(not is_ask),
            Type=lighter.SignerClient.ORDER_TYPE_TAKE_PROFIT_LIMIT,
            TimeInForce=lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1,
            TriggerPrice=tp_trigger,
            OrderExpiry=-1,
        )
        stop_loss_order = CreateOrderTxReq(
            MarketIndex=market.market_id,
            ClientOrderIndex=base_cid + 2,
            BaseAmount=0,
            Price=sl_limit,
            IsAsk=int(not is_ask),
            Type=lighter.SignerClient.ORDER_TYPE_STOP_LOSS_LIMIT,
            TimeInForce=lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1,
            TriggerPrice=sl_trigger,
            OrderExpiry=-1,
        )
        api_key_index, nonce = self._nonce()
        try:
            tx, tx_hash, err = self._run(
                self._signer.create_grouped_orders(
                    grouping_type=lighter.SignerClient.GROUPING_TYPE_ONE_TRIGGERS_A_ONE_CANCELS_THE_OTHER,
                    orders=[entry, take_profit_order, stop_loss_order],
                    nonce=nonce,
                    api_key_index=api_key_index,
                )
            )
        except Exception as exc:
            raise self._handle_api_error(exc)
        if err is not None:
            raise LighterError(f"Bracket rejected by signer: {err}")
        return {
            "dry_run": False,
            "tx_hash": tx_hash,
            "client_order_index": base_cid,
            "tx": tx.to_dict() if hasattr(tx, "to_dict") else str(tx),
            **params,
        }

    def cancel_order(
        self,
        symbol: str,
        order_index: int,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel one order by its CLIENT order index (the index given
        at creation. The official example cancels the same way). For
        bracket legs, whose client ids Lighter ignores, find the leg
        with find_active_order() and cancel the whole market with
        cancel_all_orders(symbol) instead."""
        market = self._market(symbol)
        params = {
            "venue": "lighter",
            "testnet": self.testnet,
            "symbol": market.symbol,
            "market_id": market.market_id,
            "order_index": int(order_index),
        }
        if self._guard_live(dry_run, confirm, f"cancel order {order_index} on {market.symbol}"):
            logger.info("[DRY-RUN] would cancel order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        api_key_index, nonce = self._nonce()
        try:
            tx, tx_hash, err = self._run(
                self._signer.cancel_order(
                    market_index=market.market_id,
                    order_index=int(order_index),
                    nonce=nonce,
                    api_key_index=api_key_index,
                )
            )
        except Exception as exc:
            raise self._handle_api_error(exc)
        if err is not None:
            raise LighterError(f"Cancel rejected by signer: {err}")
        return {
            "dry_run": False,
            "tx_hash": tx_hash,
            "tx": tx.to_dict() if hasattr(tx, "to_dict") else str(tx),
            **params,
        }

    def cancel_all_orders(
        self,
        symbol: str | None = None,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel all resting orders, optionally scoped to one market.
        This is the reliable way to clear bracket legs."""
        market_id = self._market(symbol).market_id if symbol else None
        params = {
            "venue": "lighter",
            "testnet": self.testnet,
            "symbol": symbol.upper() if symbol else "ALL",
            "market_id": market_id,
        }
        if self._guard_live(dry_run, confirm, f"cancel-all on {params['symbol']}"):
            logger.info("[DRY-RUN] would cancel all orders: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        api_key_index, nonce = self._nonce()
        try:
            tx, tx_hash, err = self._run(
                self._signer.cancel_all_orders(
                    time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE,
                    timestamp_ms=0,
                    cancel_all_market_index=(
                        market_id if market_id is not None
                        else lighter.SignerClient.NIL_MARKET_INDEX
                    ),
                    nonce=nonce,
                    api_key_index=api_key_index,
                )
            )
        except Exception as exc:
            raise self._handle_api_error(exc)
        if err is not None:
            raise LighterError(f"Cancel-all rejected by signer: {err}")
        return {
            "dry_run": False,
            "tx_hash": tx_hash,
            "tx": tx.to_dict() if hasattr(tx, "to_dict") else str(tx),
            **params,
        }

    def reduce_only_close(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        slippage_pct: float = 0.5,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Reduce-only market close, the half-leg rollback primitive
        (§14/§16 step 9). ``side`` accepts both spellings the callers
        use: "buy"/"sell" is already the closing order side, while
        "long"/"short" names the leg being closed (a long closes with
        a sell, a short with a buy). If this fails, surface it and
        STOP the cycle; never leave a one-legged trade."""
        closing_side = self._closing_side(side)
        return self.place_market_order(
            symbol,
            closing_side,
            size,
            slippage_pct=slippage_pct,
            dry_run=dry_run,
            confirm=confirm,
            reduce_only=True,
        )

    # ------------------------------------------------------------------
    # one-time setup (needs a funded account; key never stored by us)
    # ------------------------------------------------------------------
    def register_api_key(self, eth_private_key: str, api_key_index: int | None = None) -> dict:
        """Register a fresh API key for this account (official
        system_setup.py flow). The L1 ETH private key is passed
        explicitly, used only to sign the ChangePubKey transaction,
        and never stored anywhere. Returns the new API private key.
        Put it in LIGHTER_TESTNET_PRIVATE_KEY / LIGHTER_PRIVATE_KEY
        yourself. Requires a funded account (on testnet: connect a
        wallet in https://testnet.app.lighter.xyz first)."""
        if self.account_index is None:
            raise LighterConfigError(
                f"{self._env_prefix}ACCOUNT_INDEX is not set. The account must "
                "exist (funded) before an API key can be registered."
            )
        index = api_key_index if api_key_index is not None else self.api_key_index
        if index == 0:
            raise LighterConfigError("API key index 0 belongs to the web app. Use another slot (e.g. 3).")
        private_key, public_key, err = lighter.create_api_key()
        if err is not None:
            raise LighterError(f"Could not generate API key pair: {err}")
        # A throwaway signer just for the registration transaction.
        signer = lighter.SignerClient(
            url=self.base_url,
            account_index=self.account_index,
            api_private_keys={index: private_key},
        )
        try:
            _, tx_hash, reg_err = self._run(
                signer.change_api_key(
                    eth_private_key=eth_private_key,
                    new_pubkey=public_key,
                    api_key_index=index,
                )
            )
        except Exception as exc:
            raise self._handle_api_error(exc)
        finally:
            self._run(signer.close())
        if reg_err is not None:
            raise LighterError(f"API key registration rejected: {reg_err}")
        return {
            "account_index": self.account_index,
            "api_key_index": index,
            "api_private_key": private_key,
            "tx_hash": tx_hash,
            "note": "Store the api_private_key in the env var yourself. This adapter never writes keys to a file.",
        }


def connect(testnet: bool = False) -> "LighterClient":
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to LighterClient.connect. Default is mainnet
    (testnet=False), matching the class; the registry always passes
    testnet explicitly, so pass it every time."""
    return LighterClient.connect(testnet=testnet)


def _demo() -> None:
    """Read-only demo: public reads + simulated writes. Never live."""
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Lighter adapter demo (dry-run only)")
    parser.add_argument("--mainnet", action="store_true", help="Use mainnet (default is testnet)")
    parser.add_argument("--symbol", default="ETH")
    args = parser.parse_args()

    with LighterClient.connect(testnet=not args.mainnet) as client:
        print(f"Markets on {'testnet' if client.testnet else 'mainnet'}: {len(client._markets)} perps")
        print("Mark price:", client.fetch_mark_price(args.symbol))
        print("Funding APR % (lighter):", client.fetch_funding_rate(args.symbol))
        try:
            print("Balance:", json.dumps(client.get_balance(), indent=2))
            print("Positions:", json.dumps(client.get_positions(), indent=2))
        except LighterConfigError as err:
            print(f"Account reads skipped: {err}")
        print("Simulated market order:",
              json.dumps(client.place_market_order(args.symbol, "buy", 0.01), indent=2))
        print("Simulated bracket:",
              json.dumps(client.place_bracket(args.symbol, "buy", 0.01, 1800.0, 1900.0, 1700.0), indent=2))


if __name__ == "__main__":
    _demo()
