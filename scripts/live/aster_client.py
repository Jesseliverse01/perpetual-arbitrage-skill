#!/usr/bin/env python3
"""Aster live-trading adapter for the perpetual-arbitrage skill.

One venue adapter with the same shape as the Hyperliquid and Lighter
adapters, so the skill's unified execution cycle (SKILL.md section 14
and 16) can run a Strategy E leg on Aster. Raw HTTP only, with the
standard library plus ``requests`` for transport and ``eth-account``
for signing. No other dependencies.

There is no maintained official V3 Python SDK. The organisation's
``aster-connector-python`` only speaks the legacy V1 API, and V1 key
creation has been closed since 25 March 2026, so a new account cannot
use it at all. The official docs ship Python examples that use
``requests`` plus ``eth-account`` directly, and this adapter follows
those examples. Install with::

    pip install -r requirements-live.txt

which now lists ``requests`` and ``eth-account`` for this adapter.

How auth works (V3, from the official testnet API doc):
  * Every signed request carries ``signer`` (the API wallet, also
    called the agent), a microsecond ``nonce`` and an EIP-712
    signature. There is no API key and no HMAC secret in V3.
  * The signed message is the URL-encoded parameter string, signed
    as EIP-712 typed data: domain name ``AsterSignTransaction``,
    version ``1``, chain id 714 on testnet and 1666 on mainnet,
    verifying contract the zero address.
  * The main wallet address (``user``) is not part of the signed
    request. It is only needed once, in the app, to approve the API
    wallet. The adapter reads it from the environment when set and
    records it, but no endpoint here needs it.

Safety gates (no exceptions):
  * Every write method defaults to ``dry_run=True`` and touches
    nothing on the venue in that mode: it logs what it would do and
    returns a simulated result.
  * A live order needs BOTH ``dry_run=False`` AND ``confirm=True``.
    A live call without ``confirm=True`` raises AsterConfirmError
    and sends nothing.
  * Keys come ONLY from environment variables, never from a file and
    never hard-coded. The API wallet private key is read from
    ASTER_TESTNET_SIGNER_PRIVATE_KEY (testnet) or
    ASTER_SIGNER_PRIVATE_KEY (mainnet). The main wallet private key
    is never needed and never read.
  * No mainnet order is ever placed by this module's demo, and the
    module never touches deposit / withdraw / transfer / leverage.

Venue quirks, from SKILL.md section 14 (checked against the
official V3 doc):
  * Aster has no bracket stopLoss/takeProfit. ``place_bracket()``
    places the entry, then a TAKE_PROFIT_MARKET and a STOP_MARKET
    as separate reduce-only trigger orders. If a leg fails after
    the entry fills, the entry is closed at once.
  * The positions endpoint returns null funding fields, so
    ``get_positions()`` reports ``funding_accrued_usd`` as None.
    Estimate funding from spread history times held hours.
  * Funding does not settle every 8h on every symbol. The adapter
    reads ``fundingIntervalHours`` per symbol from the fundingInfo
    endpoint and converts with APR% = rate * (24 / hours) * 365 *
    100, falling back to 8h only when that read fails.
  * A write failing with 401, or a venue auth code such as -2015
    or -1022, means stale credentials: the cycle stops, no retry
    (AsterAuthError).
  * A write failing with 5xx is NEVER auto-retried: the original
    may already have committed on-venue, so a retry could
    double-open (AsterServerError carries the venue message
    verbatim).
  * Half-leg risk: if one leg of a two-venue arb fills and the
    other fails, roll back immediately with a reduce-only market
    close of the filled leg -- ``reduce_only_close()`` is that
    primitive. If the rollback itself fails, surface it and STOP
    the cycle.
  * Testnet lists only 21 symbols (mainnet lists about 600), and
    the testnet minimum notional is 5 USDT. Sizes and prices are
    always rounded to the symbol's stepSize and tickSize, read live
    from exchangeInfo, never hard-coded.
  * Since 1 September 2026 the venue only serves authenticated V3
    endpoints after the main wallet has completed a deposit, and
    answers -5050 otherwise. On testnet deposits are disabled, so
    if testnet ever returns -5050 the account cannot be used for
    signed calls until the venue lifts that gate.

Environment (all optional except as noted):
  ASTER_TESTNET_USER / ASTER_USER
      Main wallet address (0x...). Optional; recorded, not signed.
  ASTER_TESTNET_SIGNER / ASTER_SIGNER
      API wallet (agent) address (0x...). Needed for signed calls.
  ASTER_TESTNET_SIGNER_PRIVATE_KEY / ASTER_SIGNER_PRIVATE_KEY
      API wallet private key (hex, with or without 0x). Needed for
      signed calls. Never logged, never written to a file.

Without keys the client still connects and serves public reads
(exchange info, mark price, funding). Writes without keys raise
AsterNotSignedInError. One-time setup for a fresh account (needs a
funded account first -- on testnet that means connecting a wallet
in https://www.asterdex-testnet.com and claiming the faucet, which
credits 1,000 test USDT and 1,000 test ASTER once a day): open
https://www.asterdex-testnet.com/en/api-wallet, switch to Pro API
at the top, create an API wallet, approve it from the main wallet,
and put its address and private key in the env vars yourself.

Sources:
  * Docs: https://github.com/asterdex/api-docs (V3 futures API,
    testnet doc; chain id 714 testnet, 1666 mainnet)
  * API hosts: https://fapi.asterdex.com,
    https://fapi.asterdex-testnet.com
  * Testnet app: https://www.asterdex-testnet.com
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from venue_base import VenueAdapter, normalize_side

logger = logging.getLogger("aster_client")

try:
    import requests
except ImportError:  # pragma: no cover - missing dependency path
    requests = None

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
except ImportError:  # pragma: no cover - older eth-account path
    try:
        from eth_account import Account
        from eth_account.messages import encode_structured_data as encode_typed_data
    except ImportError:
        Account = None
        encode_typed_data = None

TESTNET_URL = "https://fapi.asterdex-testnet.com"
MAINNET_URL = "https://fapi.asterdex.com"
TESTNET_CHAIN_ID = 714
MAINNET_CHAIN_ID = 1666

# EIP-712 domain for V3 signing (official testnet doc, order example).
EIP712_DOMAIN_NAME = "AsterSignTransaction"
EIP712_DOMAIN_VERSION = "1"
EIP712_VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"

# Funding APR factor for one interval: APR% = rate * (24 / hours) * 365 * 100.
FUNDING_HOURS_FALLBACK = 8

# Small recvWindow, as the official doc recommends (5000 or less).
RECV_WINDOW_MS = 5000

# Canonical coin aliases the scanner uses (same map as multivenue_scan).
SCALE_ALIAS_MAP = {
    "KPEPE": "1000PEPE",
    "KBONK": "1000BONK",
    "KSHIB": "1000SHIB",
    "KFLOKI": "1000FLOKI",
    "KLUNC": "1000LUNC",
    "KDOGS": "1000DOGS",
    "KNEIRO": "1000NEIRO",
    "KNOT": "1000NOT",
    "KTOSHI": "1000TOSHI",
}


class AsterError(Exception):
    """Base error for this adapter."""


class AsterConfigError(AsterError):
    """Missing configuration (env var, market, size below minimum)."""


class AsterAuthError(AsterError):
    """401 (or a venue auth code) from the venue: stale credentials.
    Stop the cycle, no retry."""


class AsterServerError(AsterError):
    """5xx from the venue: never auto-retry (the order may already exist)."""


class AsterConfirmError(AsterError):
    """A live write was requested without confirm=True. Nothing sent."""


class AsterNotSignedInError(AsterError):
    """A write was requested but no API wallet is configured in the env."""


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_down_step(value, step):
    """Round value down to a multiple of step (Decimal, exact)."""
    step = Decimal(str(step))
    return float((Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN) * step)


def _round_to_tick(value, tick):
    """Round value to the nearest multiple of tick (Decimal, exact)."""
    tick = Decimal(str(tick))
    return float((Decimal(str(value)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)


class AsterClient(VenueAdapter):
    """Raw-HTTP adapter for Aster V3 (EIP-712 agent signing).

    Public reads work without keys. Writes are dry-run by default and
    need dry_run=False + confirm=True to touch the venue.
    """

    venue_name = "aster"
    testnet_url = TESTNET_URL
    mainnet_url = MAINNET_URL
    env_prefix = "ASTER_"

    def __init__(self, testnet: bool = True):
        super().__init__(testnet)
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self.chain_id = TESTNET_CHAIN_ID if testnet else MAINNET_CHAIN_ID
        self._env_prefix = "ASTER_TESTNET_" if testnet else "ASTER_"
        self._session = None
        self._symbols: dict[str, dict] = {}
        self._time_offset_ms = 0
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0
        self.user = os.environ.get(self._env_prefix + "USER") or None
        self.signer = os.environ.get(self._env_prefix + "SIGNER") or None
        self._signer_private_key = os.environ.get(
            self._env_prefix + "SIGNER_PRIVATE_KEY"
        )

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, testnet: bool = True) -> "AsterClient":
        """Build a client for testnet or mainnet.

        Reads keys ONLY from the environment (see module docstring).
        Without an API wallet the client still connects and serves
        public reads; writes raise AsterNotSignedInError.
        """
        self = cls(testnet=testnet)
        if requests is None:
            raise AsterConfigError(
                "The 'requests' package is missing. Install the live "
                "dependencies first: pip install -r requirements-live.txt"
            )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "perpetual-arb-aster/0.8"})
        try:
            self._sync_clock()
            self._load_symbols()
        except Exception:
            try:
                self._session.close()
            except Exception:
                pass
            raise
        if self._signer_private_key:
            if Account is None:
                raise AsterConfigError(
                    "The 'eth-account' package is missing, so V3 requests "
                    "cannot be signed. Install it first: "
                    "pip install -r requirements-live.txt"
                )
            if not self.signer:
                raise AsterConfigError(
                    f"{self._env_prefix}SIGNER is not set. The API wallet "
                    "address must accompany its private key."
                )
            derived = Account.from_key(self._signer_private_key).address
            if derived.lower() != self.signer.lower():
                raise AsterConfigError(
                    f"{self._env_prefix}SIGNER does not match "
                    f"{self._env_prefix}SIGNER_PRIVATE_KEY: the key derives "
                    f"to {derived}, not {self.signer}. Check both values."
                )
            logger.info(
                "Aster %s connected, signer %s",
                "testnet" if self.testnet else "mainnet",
                self.signer,
            )
        else:
            logger.warning(
                "No %sSIGNER_PRIVATE_KEY in the environment -- read-only "
                "mode. Writes will raise AsterNotSignedInError.",
                self._env_prefix,
            )
        return self

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "AsterClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _sync_clock(self) -> None:
        """Align request timestamps to the venue clock. Nonces must sit
        within 60 seconds of server time, so a skewed VM clock would
        get every signed call rejected."""
        try:
            data = self._public_get("/fapi/v3/time", {})
            server_ms = int(data["serverTime"])
            self._time_offset_ms = server_ms - int(time.time() * 1000)
            if abs(self._time_offset_ms) > 1000:
                logger.info("Aster clock offset: %d ms", self._time_offset_ms)
        except Exception as err:
            logger.warning("Could not read Aster server time (%s); using local clock.", err)
            self._time_offset_ms = 0

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _nonce(self) -> int:
        """Microsecond nonce, strictly increasing so two requests in
        the same microsecond never collide."""
        with self._nonce_lock:
            nonce = self._now_ms() * 1000
            if nonce <= self._last_nonce:
                nonce = self._last_nonce + 1
            self._last_nonce = nonce
            return nonce

    def _require_signed(self) -> None:
        if not self._signer_private_key or not self.signer:
            raise AsterNotSignedInError(
                f"No {self._env_prefix}SIGNER / "
                f"{self._env_prefix}SIGNER_PRIVATE_KEY in the environment. "
                "Create an API wallet in the Aster app and set the env "
                "vars to enable writes."
            )
        if Account is None or encode_typed_data is None:
            raise AsterConfigError(
                "The 'eth-account' package is missing, so V3 requests "
                "cannot be signed. Install it first: "
                "pip install -r requirements-live.txt"
            )

    def _guard_live(self, dry_run: bool, confirm: bool, what: str) -> bool:
        """Return True when the call is a simulation; raise otherwise."""
        if dry_run:
            return True
        if not confirm:
            raise AsterConfirmError(
                f"Live {what} refused: pass dry_run=False AND confirm=True. Nothing was sent."
            )
        self._require_signed()
        return False

    def _handle_http_error(self, resp) -> Exception:
        """Map a venue failure to the section 14 handling: 401 stops,
        5xx never retries. The venue message is carried verbatim."""
        status = resp.status_code
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        msg = payload.get("msg") if isinstance(payload, dict) else None
        detail = f"venue message verbatim: {msg} (code {code})" if msg else resp.text[:500]
        if status == 401 or code in (-2015, -2014, -1022, -1021):
            return AsterAuthError(
                f"{status} from Aster: stale credentials. Stop the cycle, "
                f"do not retry. ({detail[:300]})"
            )
        if code == -1000 and "agent" in str(msg).lower():
            return AsterAuthError(
                "Aster refused the call (-1000 'No agent found'): the "
                "signer is not a registered API wallet for any account. "
                "Stop the cycle, do not retry. Create and approve an API "
                "wallet in the Aster app first, then set its address and "
                f"private key in the env vars. ({detail[:300]})"
            )
        if code == -5050:
            return AsterConfigError(
                "Aster refused the call (-5050): authenticated V3 "
                "endpoints need a completed deposit on the main wallet "
                "first. On testnet deposits are disabled, so if testnet "
                "returns this the account cannot be used for signed "
                f"calls yet. ({detail[:300]})"
            )
        if status is not None and 500 <= status < 600:
            return AsterServerError(
                f"{status} from Aster, NOT auto-retried -- the order may "
                f"already have committed on-venue. {detail[:500]}"
            )
        return AsterError(f"Aster API error (HTTP {status}, code {code}): {detail[:500]}")

    def _public_get(self, path: str, params: dict):
        url = self.base_url + path
        try:
            resp = self._session.get(url, params=params, timeout=30)
        except Exception as err:
            raise AsterError(f"Aster request to {path} failed: {err}")
        if resp.status_code != 200:
            raise self._handle_http_error(resp)
        try:
            return resp.json()
        except Exception as err:
            raise AsterError(f"Aster returned unreadable JSON from {path}: {err}")

    def _typed_data(self, param_str: str) -> dict:
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Message": [{"name": "msg", "type": "string"}],
            },
            "primaryType": "Message",
            "domain": {
                "name": EIP712_DOMAIN_NAME,
                "version": EIP712_DOMAIN_VERSION,
                "chainId": self.chain_id,
                "verifyingContract": EIP712_VERIFYING_CONTRACT,
            },
            "message": {"msg": param_str},
        }

    def _sign(self, params: dict) -> str:
        """Sign the URL-encoded parameter string (EIP-712) and return
        the full query string with the signature appended, following
        the official V3 example."""
        param_str = urllib.parse.urlencode(params)
        try:
            # eth-account 0.14 takes the full message as a keyword.
            message = encode_typed_data(full_message=self._typed_data(param_str))
        except TypeError:
            # Older eth-account took it positionally.
            message = encode_typed_data(self._typed_data(param_str))
        signed = Account.sign_message(message, private_key=self._signer_private_key)
        return param_str + "&signature=" + signed.signature.hex()

    def _signed_request(self, method: str, path: str, params: dict):
        """One signed V3 call. Business params go in first, then the
        timestamp, recvWindow, nonce and signer, all covered by the
        signature. Sent as a query string, as in the official example."""
        self._require_signed()
        full = dict(params)
        full["timestamp"] = str(self._now_ms())
        full["recvWindow"] = str(RECV_WINDOW_MS)
        full["nonce"] = str(self._nonce())
        full["signer"] = self.signer
        query = self._sign(full)
        url = self.base_url + path + "?" + query
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            if method == "GET":
                resp = self._session.get(url, headers=headers, timeout=30)
            elif method == "POST":
                resp = self._session.post(url, headers=headers, timeout=30)
            elif method == "DELETE":
                resp = self._session.delete(url, headers=headers, timeout=30)
            else:
                raise AsterConfigError(f"unsupported HTTP method {method!r}")
        except AsterError:
            raise
        except Exception as err:
            raise AsterError(f"Aster request to {path} failed: {err}")
        if resp.status_code != 200:
            raise self._handle_http_error(resp)
        try:
            return resp.json()
        except Exception as err:
            raise AsterError(f"Aster returned unreadable JSON from {path}: {err}")

    def _load_symbols(self) -> None:
        """Read every listed symbol and its filters live from
        exchangeInfo. Testnet lists 21 symbols; mainnet about 600.
        Nothing about a symbol is hard-coded."""
        data = self._public_get("/fapi/v3/exchangeInfo", {})
        symbols = data.get("symbols", []) or []
        self._symbols = {}
        for sym in symbols:
            if sym.get("status") != "TRADING":
                continue
            name = str(sym.get("symbol", "")).upper()
            if not name:
                continue
            filters = {f.get("filterType"): f for f in sym.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            min_notional = filters.get("MIN_NOTIONAL", {})
            self._symbols[name] = {
                "symbol": name,
                "tickSize": price_filter.get("tickSize", "0.01"),
                "stepSize": lot_filter.get("stepSize", "0.001"),
                "minQty": _to_float(lot_filter.get("minQty")) or 0.0,
                "maxQty": _to_float(lot_filter.get("maxQty")),
                "minNotional": _to_float(min_notional.get("notional")) or 0.0,
                "pricePrecision": int(sym.get("pricePrecision") or 0),
                "quantityPrecision": int(sym.get("quantityPrecision") or 0),
                "orderTypes": list(sym.get("orderTypes") or []),
                "timeInForce": list(sym.get("timeInForce") or []),
            }
        if not self._symbols:
            raise AsterError(f"No TRADING symbols returned by {self.base_url}/fapi/v3/exchangeInfo")

    def refresh_symbols(self) -> None:
        """Re-read exchangeInfo live."""
        self._load_symbols()

    def _symbol_info(self, symbol: str) -> dict:
        info = self._symbols.get(symbol.upper())
        if info is None:
            raise AsterConfigError(
                f"Unknown symbol {symbol!r} on "
                f"{'testnet' if self.testnet else 'mainnet'} "
                f"(known: {', '.join(sorted(self._symbols))})"
            )
        return info

    def _round_qty(self, info: dict, size: float) -> float:
        if size <= 0:
            raise AsterConfigError(f"size must be positive, got {size}")
        qty = _round_down_step(size, info["stepSize"])
        if qty < info["minQty"]:
            raise AsterConfigError(
                f"size {size} {info['symbol']} rounds to {qty}, below the "
                f"venue minimum {info['minQty']} (step {info['stepSize']})"
            )
        if info["maxQty"] and qty > info["maxQty"]:
            raise AsterConfigError(
                f"size {size} {info['symbol']} is above the venue maximum {info['maxQty']}"
            )
        return qty

    def _round_price(self, info: dict, price: float) -> float:
        if price <= 0:
            raise AsterConfigError(f"price must be positive, got {price}")
        return _round_to_tick(price, info["tickSize"])

    def _check_notional(self, info: dict, qty: float, price: float) -> None:
        if info["minNotional"] and qty * price < info["minNotional"]:
            raise AsterConfigError(
                f"order notional {qty * price:.2f} USDT on {info['symbol']} "
                f"is below the venue minimum {info['minNotional']} USDT"
            )

    # ------------------------------------------------------------------
    # symbols
    # ------------------------------------------------------------------
    def symbol_for(self, coin: str) -> str:
        """The symbol Aster lists for one canonical coin. Aster lists
        USDT pairs, so BTC becomes BTCUSDT. The scanner's 1000-form
        meme coins list as-is (1000PEPE -> 1000PEPEUSDT); a k-form
        coin (kPEPE) maps to its 1000-form first."""
        coin = str(coin).strip().upper()
        for suffix in ("USDT", "USD"):
            if coin.endswith(suffix) and len(coin) > len(suffix):
                coin = coin[: -len(suffix)]
                break
        coin = SCALE_ALIAS_MAP.get(coin, coin)
        return coin + "USDT"

    # ------------------------------------------------------------------
    # reads (public, except balance / positions / orders)
    # ------------------------------------------------------------------
    def get_balance(self) -> dict:
        """Wallet and available balances (USDT) for the account."""
        rows = self._signed_request("GET", "/fapi/v3/balance", {})
        if isinstance(rows, dict):
            raise AsterError(f"Unexpected balance payload: {str(rows)[:300]}")
        out = {
            "venue": "aster",
            "testnet": self.testnet,
            "user": self.user,
            "signer": self.signer,
            "available_balance_usd": 0.0,
            "wallet_balance_usd": 0.0,
            "assets": {},
        }
        for row in rows:
            asset = row.get("asset")
            out["assets"][asset] = {
                "balance": _to_float(row.get("balance")) or 0.0,
                "available_balance": _to_float(row.get("availableBalance")) or 0.0,
                "cross_wallet_balance": _to_float(row.get("crossWalletBalance")) or 0.0,
                "cross_unrealized_pnl": _to_float(row.get("crossUnPnl")) or 0.0,
            }
            if asset == "USDT":
                out["available_balance_usd"] = _to_float(row.get("availableBalance")) or 0.0
                out["wallet_balance_usd"] = _to_float(row.get("balance")) or 0.0
        return out

    def get_positions(self) -> list[dict]:
        """Open perp positions. Empty list when flat. The venue
        returns null funding fields, so funding_accrued_usd is None:
        estimate funding from spread history times held hours."""
        rows = self._signed_request("GET", "/fapi/v3/positionRisk", {})
        out = []
        for pos in rows or []:
            amt = _to_float(pos.get("positionAmt")) or 0.0
            if amt == 0:
                continue
            out.append(
                {
                    "venue": "aster",
                    "symbol": pos.get("symbol"),
                    "coin": str(pos.get("symbol", "")).replace("USDT", ""),
                    "side": "long" if amt > 0 else "short",
                    "size": abs(amt),
                    "avg_entry_price": _to_float(pos.get("entryPrice")) or 0.0,
                    "mark_price": _to_float(pos.get("markPrice")),
                    "unrealized_pnl_usd": _to_float(pos.get("unRealizedProfit")) or 0.0,
                    "liquidation_price": _to_float(pos.get("liquidationPrice")) or 0.0,
                    "leverage": _to_float(pos.get("leverage")),
                    "notional_usd": _to_float(pos.get("notional")) or 0.0,
                    "funding_accrued_usd": None,
                }
            )
        return out

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Outstanding orders, optionally scoped to one symbol."""
        params = {}
        if symbol:
            params["symbol"] = self.symbol_for(symbol)
        rows = self._signed_request("GET", "/fapi/v3/openOrders", params)
        out = []
        for order in rows or []:
            sym = order.get("symbol")
            out.append(
                {
                    "oid": int(order.get("orderId")),
                    "orderId": int(order.get("orderId")),
                    "id": int(order.get("orderId")),
                    "client_order_id": order.get("clientOrderId"),
                    "symbol": sym,
                    "coin": str(sym or "").replace("USDT", ""),
                    "side": str(order.get("side", "")).lower(),
                    "size": _to_float(order.get("origQty")) or 0.0,
                    "filled_size": _to_float(order.get("executedQty")) or 0.0,
                    "price": _to_float(order.get("price")) or 0.0,
                    "stop_price": _to_float(order.get("stopPrice")),
                    "type": order.get("type"),
                    "orig_type": order.get("origType"),
                    "status": order.get("status"),
                    "reduce_only": bool(order.get("reduceOnly")),
                    "time_in_force": order.get("timeInForce"),
                }
            )
        return out

    def fetch_funding_rate(self, symbol: str) -> float:
        """Current funding APR for a symbol, as a float in percent.

        premiumIndex gives lastFundingRate per settlement interval;
        fundingInfo gives that interval in hours for the symbol, so
        APR% = rate * (24 / hours) * 365 * 100. BTC settles every 8h,
        which is rate * 3 * 365 * 100, the same conversion the skill's
        scanner uses. Positive means longs pay shorts; negative means
        shorts pay longs.
        """
        venue_symbol = self.symbol_for(symbol)
        row = self._public_get("/fapi/v3/premiumIndex", {"symbol": venue_symbol})
        if isinstance(row, dict) and "code" in row:
            raise AsterError(
                f"No premiumIndex row for {venue_symbol}: {row.get('msg')} (code {row.get('code')})"
            )
        rate = _to_float(row.get("lastFundingRate"))
        if rate is None:
            raise AsterError(f"No funding rate in premiumIndex for {venue_symbol}")
        hours = FUNDING_HOURS_FALLBACK
        try:
            info = self._public_get("/fapi/v3/fundingInfo", {"symbol": venue_symbol})
            rows = info if isinstance(info, list) else [info]
            for entry in rows:
                if str(entry.get("symbol", "")).upper() == venue_symbol:
                    hours = float(entry.get("fundingIntervalHours") or hours)
                    break
        except AsterError as err:
            logger.warning(
                "Could not read funding interval for %s (%s); assuming %dh.",
                venue_symbol, err, hours,
            )
        if hours <= 0:
            hours = FUNDING_HOURS_FALLBACK
        return rate * (24.0 / hours) * 365 * 100

    def fetch_mark_price(self, symbol: str) -> float:
        """Mark price for a perp, as a float. From premiumIndex (live,
        keyless)."""
        venue_symbol = self.symbol_for(symbol)
        row = self._public_get("/fapi/v3/premiumIndex", {"symbol": venue_symbol})
        if isinstance(row, dict) and "code" in row:
            raise AsterError(
                f"No premiumIndex row for {venue_symbol}: {row.get('msg')} (code {row.get('code')})"
            )
        price = _to_float(row.get("markPrice"))
        if price is None:
            raise AsterError(f"No mark price in premiumIndex for {venue_symbol}")
        return price

    # ------------------------------------------------------------------
    # writes -- dry_run=True by default; live needs dry_run=False + confirm
    # ------------------------------------------------------------------
    def _place_order(self, params: dict, what: str) -> dict:
        """Send one POST /fapi/v3/order. Returns the venue payload."""
        try:
            return self._signed_request("POST", "/fapi/v3/order", params)
        except AsterError:
            raise
        except Exception as err:
            raise AsterError(f"{what} failed: {err}")

    def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        reduce_only: bool = False,
    ) -> dict:
        """Market order for ``size`` base units. ``side`` is "buy" or
        "sell". Sent with newOrderRespType RESULT, so the reply is the
        final fill, not just the acknowledgement. Set reduce_only=True
        only to close a position (rollback)."""
        venue_symbol = self.symbol_for(symbol)
        info = self._symbol_info(venue_symbol)
        order_side = normalize_side(side).upper()
        qty = self._round_qty(info, size)
        mark = self.fetch_mark_price(symbol)
        self._check_notional(info, qty, mark)
        params = {
            "venue": "aster",
            "testnet": self.testnet,
            "symbol": venue_symbol,
            "side": order_side.lower(),
            "size": qty,
            "mark_price": mark,
            "reduce_only": reduce_only,
        }
        if self._guard_live(dry_run, confirm, f"market {params['side']} {qty} {venue_symbol}"):
            logger.info("[DRY-RUN] would place market order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        body = {
            "symbol": venue_symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": str(qty),
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            body["reduceOnly"] = "true"
        result = self._place_order(body, f"market {order_side} {qty} {venue_symbol}")
        return {"dry_run": False, "venue_result": result, **params}

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        time_in_force: str = "GTC",
    ) -> dict:
        """Limit order at ``price``. time_in_force: GTC (good till
        cancel), IOC (immediate or cancel), or GTX (post-only)."""
        venue_symbol = self.symbol_for(symbol)
        info = self._symbol_info(venue_symbol)
        order_side = normalize_side(side).upper()
        tif = str(time_in_force).upper()
        if tif in ("PO", "POST_ONLY", "POSTONLY"):
            tif = "GTX"
        if tif not in ("GTC", "IOC", "GTX"):
            raise AsterConfigError(
                f"time_in_force must be GTC, IOC or GTX (post-only), got {time_in_force!r}"
            )
        qty = self._round_qty(info, size)
        rounded_price = self._round_price(info, price)
        self._check_notional(info, qty, rounded_price)
        params = {
            "venue": "aster",
            "testnet": self.testnet,
            "symbol": venue_symbol,
            "side": order_side.lower(),
            "size": qty,
            "price": rounded_price,
            "time_in_force": tif,
        }
        if self._guard_live(
            dry_run, confirm, f"limit {params['side']} {qty} {venue_symbol} @ {rounded_price}"
        ):
            logger.info("[DRY-RUN] would place limit order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        body = {
            "symbol": venue_symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": tif,
            "quantity": str(qty),
            "price": str(rounded_price),
        }
        result = self._place_order(
            body, f"limit {order_side} {qty} {venue_symbol} @ {rounded_price}"
        )
        return {"dry_run": False, "venue_result": result, **params}

    def _place_trigger(self, venue_symbol: str, info: dict, side: str,
                       qty: float, stop_price: float, order_type: str) -> dict:
        """One reduce-only trigger order (STOP_MARKET or
        TAKE_PROFIT_MARKET). Triggers on the mark price, so a wick in
        the last price alone cannot fire it."""
        body = {
            "symbol": venue_symbol,
            "side": side,
            "type": order_type,
            "quantity": str(qty),
            "stopPrice": str(self._round_price(info, stop_price)),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
            "timeInForce": "GTC",
        }
        return self._place_order(body, f"{order_type} {side} {qty} {venue_symbol}")

    def place_bracket(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float | None = None,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Bracket without a bracket primitive: the entry goes in
        first (market when entry_price is None, else a limit), then
        the take-profit and stop-loss sit as separate reduce-only
        trigger orders on the opposite side, same size. Half-leg
        rule: if the entry fills and a trigger leg fails, the entry
        is closed at once with a reduce-only market order; if that
        close fails, the error is raised loudly and the cycle stops."""
        venue_symbol = self.symbol_for(symbol)
        info = self._symbol_info(venue_symbol)
        entry_side = normalize_side(side)
        close_side = "sell" if entry_side == "buy" else "buy"
        qty = self._round_qty(info, size)
        params = {
            "venue": "aster",
            "testnet": self.testnet,
            "symbol": venue_symbol,
            "side": entry_side,
            "size": qty,
            "entry_price": entry_price,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
        }
        if self._guard_live(dry_run, confirm, f"bracket {entry_side} {qty} {venue_symbol}"):
            logger.info("[DRY-RUN] would place bracket: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        if entry_price is None:
            entry = self.place_market_order(
                symbol, entry_side, qty, dry_run=False, confirm=True
            )
        else:
            entry = self.place_limit_order(
                symbol, entry_side, qty, entry_price, dry_run=False, confirm=True
            )
        entry_result = entry.get("venue_result", {}) or {}
        entry_status = str(entry_result.get("status", "")).upper()
        if entry_price is None and entry_status not in ("FILLED", "PARTIALLY_FILLED"):
            raise AsterError(
                f"Bracket entry did not fill on {venue_symbol}: "
                f"status {entry_status or 'unknown'} ({str(entry_result)[:300]}). "
                "No trigger legs were placed."
            )
        legs = {"entry": entry}
        try:
            if take_profit is not None:
                legs["take_profit"] = self._place_trigger(
                    venue_symbol, info, close_side.upper(), qty,
                    take_profit, "TAKE_PROFIT_MARKET",
                )
            if stop_loss is not None:
                legs["stop_loss"] = self._place_trigger(
                    venue_symbol, info, close_side.upper(), qty,
                    stop_loss, "STOP_MARKET",
                )
        except AsterError as err:
            logger.error("Bracket leg failed after entry filled: %s", err)
            try:
                close = self.reduce_only_close(
                    symbol, entry_side, qty, dry_run=False, confirm=True
                )
                legs["rollback_close"] = close
            except AsterError as rollback_err:
                raise AsterError(
                    f"Bracket leg failed ({err}) AND the rollback close "
                    f"failed ({rollback_err}). STOP the cycle: a one-legged "
                    f"trade may be open on {venue_symbol}."
                )
            raise AsterError(
                f"Bracket leg failed ({err}); entry was closed at once "
                "with a reduce-only market order."
            )
        return {"dry_run": False, "legs": legs, **params}

    def cancel_order(
        self,
        symbol: str,
        order_id,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel one order by its venue order id."""
        venue_symbol = self.symbol_for(symbol)
        self._symbol_info(venue_symbol)
        params = {
            "venue": "aster",
            "testnet": self.testnet,
            "symbol": venue_symbol,
            "order_id": int(order_id),
        }
        if self._guard_live(dry_run, confirm, f"cancel order {order_id} on {venue_symbol}"):
            logger.info("[DRY-RUN] would cancel order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        result = self._signed_request(
            "DELETE", "/fapi/v3/order",
            {"symbol": venue_symbol, "orderId": str(int(order_id))},
        )
        return {"dry_run": False, "venue_result": result, **params}

    def cancel_all_orders(
        self,
        symbol: str | None = None,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel every resting order, optionally scoped to one symbol.
        Uses the native cancel-all per symbol (DELETE
        /fapi/v3/allOpenOrders needs a symbol); with no symbol it
        clears each symbol that has open orders."""
        if symbol:
            symbols = [self.symbol_for(symbol)]
        else:
            symbols = sorted({o["symbol"] for o in self.get_open_orders()})
        params = {
            "venue": "aster",
            "testnet": self.testnet,
            "symbol": symbols[0] if len(symbols) == 1 else "ALL",
            "symbols": symbols,
        }
        if self._guard_live(dry_run, confirm, f"cancel-all on {params['symbol']}"):
            logger.info("[DRY-RUN] would cancel all orders: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        results = []
        for venue_symbol in symbols:
            results.append(
                self._signed_request(
                    "DELETE", "/fapi/v3/allOpenOrders", {"symbol": venue_symbol}
                )
            )
        return {"dry_run": False, "venue_results": results, **params}

    def reduce_only_close(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Reduce-only market close -- the half-leg rollback primitive.
        ``side`` is the ORDER side: pass "sell" to close a long, "buy"
        to close a short (the same convention the execution engine
        uses). If this fails, surface it and STOP the cycle; never
        leave a one-legged trade."""
        return self.place_market_order(
            symbol, side, size, dry_run=dry_run, confirm=confirm, reduce_only=True
        )

    def rollback_close(
        self,
        symbol: str,
        size: float | None = None,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Emergency unwind: close the open position on ``symbol``.
        Finds the position, flips its side (a long closes with a
        sell), and reduce-only closes it. ``size`` closes part of a
        position when given; None closes the whole thing."""
        venue_symbol = self.symbol_for(symbol)
        match = None
        for pos in self.get_positions():
            if str(pos.get("symbol", "")).upper() == venue_symbol:
                match = pos
                break
        if match is None:
            raise AsterError(
                f"Aster has no open position on {venue_symbol} to roll back."
            )
        close_size = size if size is not None else abs(float(match.get("size", 0)))
        if close_size <= 0:
            raise AsterError(
                f"Aster position on {venue_symbol} has no size to close."
            )
        close_side = "sell" if match.get("side") == "long" else "buy"
        return self.reduce_only_close(
            symbol, close_side, close_size, dry_run=dry_run, confirm=confirm
        )


def connect(testnet: bool = True) -> "AsterClient":
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to the class. Default is testnet, matching
    the class; the registry always passes testnet explicitly, so pass
    it every time."""
    return AsterClient.connect(testnet=testnet)


def _demo() -> None:
    """Read-only demo: public reads + simulated writes. Never live."""
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Aster adapter demo (dry-run only)")
    parser.add_argument("--mainnet", action="store_true", help="Use mainnet (default is testnet)")
    parser.add_argument("--symbol", default="BTC")
    args = parser.parse_args()

    with AsterClient.connect(testnet=not args.mainnet) as client:
        print(f"Symbols on {'testnet' if client.testnet else 'mainnet'}: {len(client._symbols)} TRADING")
        print("Mark price:", client.fetch_mark_price(args.symbol))
        print("Funding APR %:", client.fetch_funding_rate(args.symbol))
        try:
            print("Balance:", json.dumps(client.get_balance(), indent=2))
            print("Positions:", json.dumps(client.get_positions(), indent=2))
            print("Open orders:", json.dumps(client.get_open_orders(args.symbol), indent=2))
        except AsterConfigError as err:
            print(f"Account reads skipped: {err}")
        except AsterNotSignedInError as err:
            print(f"Account reads skipped: {err}")
        print("Simulated market order:",
              json.dumps(client.place_market_order(args.symbol, "buy", 0.01), indent=2))
        print("Simulated bracket:",
              json.dumps(client.place_bracket(args.symbol, "buy", 0.01, 60000.0, 70000.0, 55000.0), indent=2))


if __name__ == "__main__":
    _demo()
