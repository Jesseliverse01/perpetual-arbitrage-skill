#!/usr/bin/env python3
"""Derive live-trading adapter for the perpetual-arbitrage skill.

One venue adapter with the same shape as the other live adapters, so the skill's unified execution cycle (SKILL.md §13/§14/§16)
can run a Strategy E leg on Derive. Stdlib plus the official Derive
Python SDK (`derive-py`, https://github.com/derivexyz/derive-py,
MIT, 0.1.5). No raw signing code lives here: every write goes through
the SDK's `HTTPClient`, which EIP-712-signs each order locally and
sends it to the venue.

Derive is the v3 exchange: perps and options on an off-chain book,
settled on Ethereum (testnet settles on Sepolia). The old Lyra v2 API
hosts (api.lyra.finance, api-demo.lyra.finance) are not used. The v3
hosts are https://api.derive.xyz/v3 on mainnet and
https://testnet.api.derive.xyz/v3 on testnet, and the SDK is generated
from the published v3 specs, so it covers the whole RPC surface,
perps included.

Safety gates (no exceptions):
  * Every write method defaults to ``dry_run=True`` and touches
    nothing on the venue in that mode: it logs what it would do and
    returns a simulated result.
  * A live order needs BOTH ``dry_run=False`` AND ``confirm=True``.
    A live call without ``confirm=True`` raises DeriveConfirmError
    and sends nothing.
  * Keys come ONLY from environment variables, never from a file and
    never hard-coded. The signing key is read from
    DERIVE_TESTNET_PRIVATE_KEY (testnet) or DERIVE_PRIVATE_KEY
    (mainnet). That key should be a session key minted for this bot,
    not the owner wallet key: a session key can only trade on the
    subaccounts it was given, and it expires. The owner wallet
    address comes from DERIVE_TESTNET_WALLET / DERIVE_WALLET and the
    subaccount id from DERIVE_TESTNET_SUBACCOUNT_ID /
    DERIVE_SUBACCOUNT_ID.
  * No mainnet order is ever placed by this module's demo, and the
    module never touches deposit / withdraw / transfer.

Venue quirks, verified against the live testnet API on 2026-11-17:
  * Funding is quoted PER HOUR, not per 8 hours (docs:
    https://docs.derive.xyz/perp-funding.md: "The funding rate
    quoted above and returned by the API is a per-hour rate").
    APR% = rate * 24 * 365 * 100. The baseline rate is 0.00125% per
    hour (0.01% per 8h), so a flat book reads 10.95% APR.
  * The ticker carries the current funding rate and the mark price
    in one call (`public/get_ticker`), so both reads are cheap.
  * A market order still needs a limit price: it acts as the
    worst-acceptable bound and any unfilled remainder is cancelled.
  * `reduce_only` only works on market orders and non-resting limit
    orders (IOC/FOK), so the rollback close is a reduce-only market
    order.
  * Derive has no bracket primitive. A bracket here is an entry plus
    two separate trigger orders (take-profit and stop-loss, both
    reduce-only, watching the mark price). The entry is placed
    first; the legs only go on once the entry has filled. If a leg
    fails after the entry filled, the entry is closed at once with a
    reduce-only market order; if that close fails, the error is
    raised loudly and the cycle stops.
  * `cancel_all` on the subaccount clears resting orders, and with
    the trigger flag it clears the pending trigger legs too. The
    SDK's `cancel_all()` wrapper does not pass that flag, so this
    adapter calls the generated RPC method directly.
  * A 401 means stale credentials: the cycle stops, no retry
    (DeriveAuthError). A 5xx is NEVER auto-retried: the original may
    already have committed on-venue, so a retry could double-open
    (DeriveServerError carries the venue message verbatim).
  * Testnet lists 20 perps (ETH-PERP, BTC-PERP, SOL-PERP, HYPE-PERP
    and 16 more). Instruments are always resolved live from
    `get_all_instruments`, never hard-coded. Sizes are quantized by
    the SDK to each instrument's `amount_step` and `tick_size`, and
    each instrument has a minimum amount (ETH-PERP: 0.1, BTC-PERP:
    0.01).

Environment (all optional except as noted):
  DERIVE_TESTNET_PRIVATE_KEY / DERIVE_PRIVATE_KEY
      Private key of the signer: a session key if one exists, else
      the owner key. Trading key only; the adapter never withdraws.
  DERIVE_TESTNET_WALLET / DERIVE_WALLET
      Owner wallet address (0x...). Needed together with the key.
  DERIVE_TESTNET_SUBACCOUNT_ID / DERIVE_SUBACCOUNT_ID
      Subaccount id to trade on. Needed for balance, positions,
      orders, and every write.
  DERIVE_TESTNET_MAX_FEE / DERIVE_MAX_FEE
      Fee cap signed into each order, default 10 (USDC per unit of
      volume, the official example's value).

Without keys the client still connects and serves public reads
(instruments, mark price, funding). Writes without keys raise
DeriveNotSignedInError. To fund a testnet account, Jesse needs a
browser wallet: get Sepolia ETH from a Sepolia faucet, open
https://testnet.app.derive.xyz, connect the wallet, click Deposit
then Mint to mint test USDC, and deposit. The first deposit creates
the account and its first subaccount (the id shows up about two
minutes later). Then mint a session key for the bot in the app
(Settings, Session Keys) with the trade scope on that subaccount,
and put its private key in DERIVE_TESTNET_PRIVATE_KEY.

Sources:
  * SDK: https://github.com/derivexyz/derive-py (0.1.5 verified)
  * Docs: https://docs.derive.xyz (quickstart, perp funding,
    order types, session keys)
  * API hosts: https://testnet.api.derive.xyz/v3,
    https://api.derive.xyz/v3
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from msgspec import UNSET

from derive_py import HTTPClient
from derive_py.data_types import Chain, ClientConfig
from derive_py.data_types.generated_models import (
    AssetType,
    CancelAllRequest,
    CancelTriggerOrderRequest,
    Direction,
    GetFundingRateHistoryRequest,
    GetTriggerOrdersRequest,
    OrderType,
    TimeInForce,
    TriggerPriceType,
    TriggerType,
)
from derive_py.exceptions import (
    CredentialsRequired,
    DeriveJSONRPCError,
    NoActiveSubaccount,
)

from venue_base import VenueAdapter

logger = logging.getLogger("derive_client")

MAINNET_URL = "https://api.derive.xyz/v3"
TESTNET_URL = "https://testnet.api.derive.xyz/v3"

# Derive quotes funding per hour (docs/perp-funding.md), so the
# annualisation is 24 hours a day, not 3 eight-hour windows.
FUNDING_APR_FACTOR = 24 * 365

# Fee cap signed into every order (USDC per unit of volume). The
# official SDK example uses 10; the SDK's own default of 1000 is far
# looser than any real fee, so this adapter stays at 10 unless the
# env var says otherwise.
DEFAULT_MAX_FEE = Decimal("10")

# Trigger legs sit this far past their trigger price so a trigger
# still fills instead of resting unfilled.
TRIGGER_LIMIT_OFFSET = Decimal("0.002")


class DeriveError(Exception):
    """Base error for this adapter."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class DeriveConfigError(DeriveError):
    """Missing configuration (env var, instrument, size below minimum)."""


class DeriveAuthError(DeriveError):
    """401 / unauthorized from the venue: stale credentials. Stop the
    cycle, do not retry."""


class DeriveServerError(DeriveError):
    """5xx from the venue: never auto-retry (the order may already exist)."""


class DeriveConfirmError(DeriveError):
    """A live write was requested without confirm=True. Nothing sent."""


class DeriveNotSignedInError(DeriveError):
    """A write was requested but no signing key is configured."""


class _Instrument:
    """One perp as listed by the venue, with its size rules."""

    def __init__(self, raw):
        self.name = raw.instrument_name
        self.tick_size = Decimal(str(raw.tick_size))
        self.amount_step = Decimal(str(raw.amount_step))
        self.minimum_amount = Decimal(str(raw.minimum_amount))
        self.base_currency = getattr(raw, "base_currency", None)


class DeriveClient(VenueAdapter):
    """Sync wrapper around the official Derive Python SDK.

    Public reads work without keys. Writes are dry-run by default and
    need dry_run=False + confirm=True to touch the venue.
    """

    venue_name = "derive"
    testnet_url = TESTNET_URL
    mainnet_url = MAINNET_URL
    env_prefix = "DERIVE_"

    def __init__(self, testnet: bool = True):
        super().__init__(testnet)
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self._env_prefix = "DERIVE_TESTNET_" if testnet else "DERIVE_"
        self._chain = Chain.SEPOLIA if testnet else Chain.ETHEREUM
        self._sdk: HTTPClient | None = None
        self._instruments: dict[str, _Instrument] = {}
        self.wallet = os.environ.get(self._env_prefix + "WALLET")
        self.subaccount_id = self._env_int("SUBACCOUNT_ID")
        self._session_key = os.environ.get(self._env_prefix + "PRIVATE_KEY")
        self.max_fee = self._env_decimal("MAX_FEE", DEFAULT_MAX_FEE)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, testnet: bool = True) -> "DeriveClient":
        """Build a client for testnet or mainnet.

        Reads keys ONLY from the environment (see module docstring).
        Without a signing key the client still connects and serves
        public reads; writes raise DeriveNotSignedInError.
        """
        self = cls(testnet=testnet)
        try:
            self._setup()
        except Exception:
            self.close()
            raise
        return self

    def _setup(self) -> None:
        """Build the SDK client and resolve the perp list live."""
        if self._session_key and not self.wallet:
            raise DeriveConfigError(
                f"{self._env_prefix}PRIVATE_KEY is set but "
                f"{self._env_prefix}WALLET is not. The key and the "
                "owner wallet address go together."
            )
        if self._session_key and self.wallet:
            config = ClientConfig(
                chain=self._chain,
                session_key=self._session_key,
                wallet=self.wallet,
                subaccount_id=self.subaccount_id,
            )
        else:
            if self.wallet or self.subaccount_id is not None:
                logger.warning(
                    "No %sPRIVATE_KEY in the environment. Read-only mode. "
                    "Writes will raise DeriveNotSignedInError.",
                    self._env_prefix,
                )
            config = ClientConfig(chain=self._chain)
        self._sdk = HTTPClient(config)
        try:
            self._sdk.connect()
        except Exception as err:
            raise self._handle_error(err)
        self._load_instruments()
        if self._session_key:
            logger.info(
                "Derive %s connected, wallet %s, subaccount %s",
                "testnet" if self.testnet else "mainnet",
                self.wallet,
                self.subaccount_id,
            )
        else:
            logger.warning(
                "No %sPRIVATE_KEY in the environment. Read-only mode. "
                "Writes will raise DeriveNotSignedInError.",
                self._env_prefix,
            )

    def close(self) -> None:
        """Close the SDK session. Idempotent."""
        if self._sdk is not None:
            try:
                self._sdk.disconnect()
            except Exception:
                pass
            self._sdk = None

    def __enter__(self) -> "DeriveClient":
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
            raise DeriveConfigError(
                f"{self._env_prefix}{name} must be an integer, got {raw!r}"
            )

    def _env_decimal(self, name: str, default: Decimal) -> Decimal:
        raw = os.environ.get(self._env_prefix + name)
        if raw is None or raw == "":
            return default
        try:
            return Decimal(raw)
        except Exception:
            raise DeriveConfigError(
                f"{self._env_prefix}{name} must be a decimal, got {raw!r}"
            )

    def _handle_error(self, err: Exception) -> Exception:
        """Map a venue error to the §14 handling: 401 stops, 5xx never
        retries, anything else surfaces verbatim."""
        if isinstance(err, DeriveError):
            return err
        if isinstance(err, CredentialsRequired):
            return DeriveNotSignedInError(
                f"No {self._env_prefix}PRIVATE_KEY in the environment (or "
                "the key has no session for this wallet). Set the signing "
                "key env var to enable writes."
            )
        if isinstance(err, NoActiveSubaccount):
            return DeriveConfigError(
                f"{self._env_prefix}SUBACCOUNT_ID is not set. Balance, "
                "positions, orders and writes need the subaccount id."
            )
        if isinstance(err, DeriveJSONRPCError):
            code = err.rpc_error.code
            message = err.rpc_error.message
            data = err.rpc_error.data
            text = f"Derive RPC {code}: {message}"
            if data is not None:
                text += f"  [data={data!r}]"
            if code == 14014:
                # Wrong chain for the credentials (testnet values on
                # mainnet or the other way round), per the docs.
                return DeriveConfigError(text)
            if code == 14031 or "unauthor" in str(message).lower():
                return DeriveAuthError(
                    f"{text}. Stale or under-scoped credentials. Stop "
                    "the cycle, do not retry."
                )
            return DeriveError(text, code=code)
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status is None:
            status = getattr(err, "status", None)
        if status == 401:
            return DeriveAuthError(
                "401 from Derive: stale credentials. Stop the cycle, do "
                f"not retry. ({str(err)[:300]})"
            )
        if status is not None and 500 <= status < 600:
            return DeriveServerError(
                f"{status} from Derive, NOT auto-retried. The order may "
                "already have committed on-venue. Venue message verbatim: "
                f"{str(err)[:500]}"
            )
        return DeriveError(f"Derive error: {str(err)[:500]}")

    def _load_instruments(self) -> None:
        """Resolve every perp live; the list differs per network."""
        try:
            page = 1
            found: dict[str, _Instrument] = {}
            while True:
                resp = self._sdk.markets.get_all_instruments(
                    expired=False,
                    instrument_type=AssetType.perp,
                    page=page,
                    page_size=1000,
                )
                for raw in resp.instruments:
                    found[raw.instrument_name] = _Instrument(raw)
                pagination = resp.pagination
                if pagination is None or page >= pagination.num_pages:
                    break
                page += 1
        except Exception as err:
            raise self._handle_error(err)
        if not found:
            raise DeriveError(f"No perp instruments returned by {self.base_url}")
        self._instruments = found

    def refresh_instruments(self) -> None:
        """Re-resolve the perp list live."""
        self._load_instruments()

    def _instrument(self, symbol: str) -> _Instrument:
        name = self.symbol_for(symbol)
        inst = self._instruments.get(name)
        if inst is None:
            raise DeriveConfigError(
                f"Unknown perp symbol {symbol!r} on "
                f"{'testnet' if self.testnet else 'mainnet'} (known: "
                f"{', '.join(sorted(self._instruments))})"
            )
        return inst

    def _require_signed(self) -> None:
        if self._sdk is None or self._session_key is None:
            raise DeriveNotSignedInError(
                f"No {self._env_prefix}PRIVATE_KEY in the environment. "
                "Set the signing key env var to enable writes."
            )

    def _require_subaccount(self) -> int:
        self._require_signed()
        if self.subaccount_id is None:
            raise DeriveConfigError(
                f"{self._env_prefix}SUBACCOUNT_ID is not set. Balance, "
                "positions, orders and writes need the subaccount id."
            )
        return self.subaccount_id

    def _guard_live(self, dry_run: bool, confirm: bool, what: str) -> bool:
        """Return True when the call is a simulation; raise otherwise."""
        if dry_run:
            return True
        if not confirm:
            raise DeriveConfirmError(
                f"Live {what} refused: pass dry_run=False AND confirm=True. Nothing was sent."
            )
        self._require_signed()
        self._require_subaccount()
        return False

    @staticmethod
    def _to_decimal(value) -> Decimal | None:
        if value is None or value is UNSET:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def _ticker(self, symbol: str):
        inst = self._instrument(symbol)
        try:
            return self._sdk.markets.get_ticker(instrument_name=inst.name)
        except Exception as err:
            raise self._handle_error(err)

    # ------------------------------------------------------------------
    # reads (public, except balance / positions / orders)
    # ------------------------------------------------------------------
    def symbol_for(self, coin: str) -> str:
        """The symbol Derive lists for one canonical coin. Derive
        names its perps `<COIN>-PERP` (ETH-PERP, BTC-PERP), so the
        coin gets that suffix. A coin that already carries it is
        returned unchanged."""
        coin = str(coin).strip().upper()
        if coin.endswith("-PERP"):
            return coin
        return f"{coin}-PERP"

    def get_balance(self) -> dict:
        """USDC collateral on the subaccount, in USD. `amount` is the
        collateral held; `available_balance_usd` is the mark value
        less the margin in use and the margin reserved for open
        orders."""
        subaccount_id = self._require_subaccount()
        try:
            resp = self._sdk.active_subaccount.collateral.get()
        except Exception as err:
            raise self._handle_error(err)
        usdc = None
        total_mark_value = Decimal("0")
        for col in resp.collaterals:
            mark_value = self._to_decimal(col.mark_value) or Decimal("0")
            total_mark_value += mark_value
            if str(col.asset_name).upper() == "USDC":
                usdc = col
        if usdc is None:
            raise DeriveError(
                f"No USDC collateral on subaccount {subaccount_id} "
                f"({'testnet' if self.testnet else 'mainnet'})"
            )
        amount = self._to_decimal(usdc.amount) or Decimal("0")
        mark_value = self._to_decimal(usdc.mark_value) or Decimal("0")
        initial_margin = self._to_decimal(usdc.initial_margin) or Decimal("0")
        open_orders_margin = (
            self._to_decimal(usdc.open_orders_margin) or Decimal("0")
        )
        return {
            "venue": "derive",
            "testnet": self.testnet,
            "subaccount_id": subaccount_id,
            "available_balance_usd": float(
                mark_value - initial_margin - open_orders_margin
            ),
            "collateral_usd": float(mark_value),
            "total_asset_value_usd": float(total_mark_value),
            "usdc_balance": float(amount),
            "usdc_locked": float(initial_margin + open_orders_margin),
            "initial_margin_usd": float(initial_margin),
            "open_orders_margin_usd": float(open_orders_margin),
        }

    def get_positions(self) -> list[dict]:
        """Open perp positions. Empty list when flat. Funding accrued
        is `cumulative_funding_usd` (negative = received); funding
        still owed on the open position is `pending_funding_usd`."""
        self._require_subaccount()
        try:
            positions = self._sdk.active_subaccount.positions.list()
        except Exception as err:
            raise self._handle_error(err)
        out = []
        for pos in positions:
            if str(pos.instrument_type).lower() != "perp":
                continue
            size = self._to_decimal(pos.amount) or Decimal("0")
            if size == 0:
                continue
            out.append(
                {
                    "venue": "derive",
                    "symbol": pos.instrument_name,
                    "side": "long" if size > 0 else "short",
                    "size": float(abs(size)),
                    "avg_entry_price": float(
                        self._to_decimal(pos.average_price) or Decimal("0")
                    ),
                    "mark_price": float(
                        self._to_decimal(pos.mark_price) or Decimal("0")
                    ),
                    "position_value_usd": float(
                        self._to_decimal(pos.mark_value) or Decimal("0")
                    ),
                    "unrealized_pnl_usd": float(
                        self._to_decimal(pos.unrealized_pnl) or Decimal("0")
                    ),
                    "realized_pnl_usd": float(
                        self._to_decimal(pos.realized_pnl) or Decimal("0")
                    ),
                    "initial_margin_usd": float(
                        self._to_decimal(pos.initial_margin) or Decimal("0")
                    ),
                    "cumulative_funding_usd": float(
                        self._to_decimal(pos.cumulative_funding) or Decimal("0")
                    ),
                    "pending_funding_usd": float(
                        self._to_decimal(pos.pending_funding) or Decimal("0")
                    ),
                }
            )
        return out

    def _trigger_orders(self) -> list:
        """Pending trigger orders (stop-loss / take-profit legs that
        have not fired yet) for the subaccount."""
        subaccount_id = self._require_subaccount()
        try:
            result = self._sdk.private_api.rpc.get_trigger_orders(
                GetTriggerOrdersRequest(subaccount_id=subaccount_id)
            )
        except Exception as err:
            raise self._handle_error(err)
        return list(result.orders)

    @staticmethod
    def _order_dict(order, is_trigger: bool) -> dict:
        return {
            "id": order.order_id,
            "order_id": order.order_id,
            "symbol": order.instrument_name,
            "coin": order.instrument_name,
            "side": str(order.direction).lower(),
            "size": float(order.amount),
            "filled_size": float(order.filled_amount),
            "price": float(order.limit_price),
            "average_price": float(order.average_price),
            "type": str(order.order_type),
            "status": str(order.order_status),
            "reduce_only": bool(
                getattr(order, "reduce_only", False)
                if hasattr(order, "reduce_only")
                else False
            ),
            "trigger_price": (
                float(order.trigger_price)
                if order.trigger_price is not None
                else None
            ),
            "is_trigger": is_trigger,
        }

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Outstanding orders, resting and pending-trigger, optionally
        scoped to one symbol. Each dict carries the venue order id,
        the symbol, the side, the size and the price, plus
        `is_trigger` so the caller can tell a resting order from a
        stop-loss / take-profit leg that has not fired yet."""
        self._require_subaccount()
        try:
            resting = self._sdk.active_subaccount.orders.list_open()
        except Exception as err:
            raise self._handle_error(err)
        triggers = self._trigger_orders()
        want = symbol.upper() if symbol else None
        out = []
        for order in resting:
            if want and order.instrument_name.upper() != want:
                continue
            out.append(self._order_dict(order, is_trigger=False))
        for order in triggers:
            if want and order.instrument_name.upper() != want:
                continue
            out.append(self._order_dict(order, is_trigger=True))
        return out

    def fetch_funding_rate(self, symbol: str) -> float:
        """Current funding APR for a symbol, as a float in percent.

        Derive quotes funding PER HOUR, so APR% = rate * 24 * 365
        * 100 (docs/perp-funding.md). Positive means longs pay
        shorts; negative means shorts pay longs. The ticker carries
        the live rate; when it is missing, the latest funding-rate
        candle is used instead.
        """
        inst = self._instrument(symbol)
        ticker = self._ticker(inst.name)
        rate = self._to_decimal(ticker.funding_rate)
        if rate is None:
            try:
                result = self._sdk.public_api.rpc.get_funding_rate_history(
                    GetFundingRateHistoryRequest(instrument_name=inst.name)
                )
            except Exception as err:
                raise self._handle_error(err)
            candles = list(result.funding_rate_history)
            if not candles:
                raise DeriveError(
                    f"No funding rate for {inst.name} on "
                    f"{'testnet' if self.testnet else 'mainnet'}"
                )
            rate = self._to_decimal(candles[-1].funding_rate)
        if rate is None:
            raise DeriveError(f"No funding rate for {inst.name}")
        return float(rate) * FUNDING_APR_FACTOR * 100

    def fetch_mark_price(self, symbol: str) -> float:
        """Mark price for a perp, as a float. From the ticker (live,
        keyless)."""
        ticker = self._ticker(symbol)
        mark = self._to_decimal(ticker.mark_price)
        if mark is None:
            raise DeriveError(f"No mark price for {symbol!r}")
        return float(mark)

    # ------------------------------------------------------------------
    # writes: dry_run=True by default; live needs dry_run=False + confirm
    # ------------------------------------------------------------------
    def _check_size(self, inst: _Instrument, size: float) -> Decimal:
        if size <= 0:
            raise DeriveConfigError(f"size must be positive, got {size}")
        amount = Decimal(str(size))
        if amount < inst.minimum_amount:
            raise DeriveConfigError(
                f"size {size} {inst.name} is below the venue minimum "
                f"{inst.minimum_amount} (tick {inst.tick_size}, step "
                f"{inst.amount_step})"
            )
        return amount

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
        raise DeriveConfigError(
            f"side must be buy/sell (or long/short), got {side!r}"
        )

    @staticmethod
    def _direction(side: str) -> Direction:
        s = str(side).strip().lower()
        if s in ("buy", "long"):
            return Direction.buy
        if s in ("sell", "short"):
            return Direction.sell
        raise DeriveConfigError(
            f"side must be buy/sell (or long/short), got {side!r}"
        )

    def _create_order(self, *, instrument_name, direction, amount,
                      limit_price, order_type, time_in_force,
                      reduce_only=False, trigger_price=None,
                      trigger_type=None, label=""):
        """One signed order through the SDK. Raises the mapped venue
        error on any failure; returns the created order."""
        kwargs = dict(
            amount=amount,
            direction=direction,
            instrument_name=instrument_name,
            limit_price=limit_price,
            max_fee=self.max_fee,
            order_type=order_type,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            label=label,
        )
        if trigger_price is not None:
            kwargs["trigger_price"] = trigger_price
            kwargs["trigger_price_type"] = TriggerPriceType.mark
            kwargs["trigger_type"] = trigger_type
        try:
            response = self._sdk.active_subaccount.orders.create(**kwargs)
        except Exception as err:
            raise self._handle_error(err)
        return response.order

    @staticmethod
    def _result(order, params):
        return {
            "dry_run": False,
            "order_id": order.order_id,
            "status": str(order.order_status),
            "filled_amount": float(order.filled_amount),
            "average_price": float(order.average_price),
            **params,
        }

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
        """Market order. The limit price is the worst acceptable fill:
        mark * (1 + slippage) for buys, mark * (1 - slippage) for
        sells. Derive requires a limit price even on market orders,
        and any unfilled remainder is cancelled rather than resting.
        Set reduce_only=True only to close a position (rollback)."""
        inst = self._instrument(symbol)
        direction = self._direction(side)
        amount = self._check_size(inst, size)
        # Refuse an unconfirmed live call before any network traffic.
        if not dry_run and not confirm:
            raise DeriveConfirmError(
                "Live market order refused: pass dry_run=False AND "
                "confirm=True. Nothing was sent."
            )
        mark = Decimal(str(self.fetch_mark_price(inst.name)))
        worst = (
            mark * (Decimal("1") + Decimal(str(slippage_pct)) / Decimal("100"))
            if direction == Direction.buy
            else mark * (Decimal("1") - Decimal(str(slippage_pct)) / Decimal("100"))
        )
        params = {
            "venue": "derive",
            "testnet": self.testnet,
            "symbol": inst.name,
            "side": "buy" if direction == Direction.buy else "sell",
            "size": float(amount),
            "mark_price": float(mark),
            "worst_price": float(worst),
            "reduce_only": reduce_only,
        }
        if self._guard_live(
            dry_run, confirm,
            f"market {params['side']} {size} {inst.name}",
        ):
            logger.info("[DRY-RUN] would place market order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        order = self._create_order(
            instrument_name=inst.name,
            direction=direction,
            amount=amount,
            limit_price=worst,
            order_type=OrderType.market,
            time_in_force=TimeInForce.ioc,
            reduce_only=reduce_only,
        )
        return self._result(order, params)

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        *,
        time_in_force: str = "GTC",
        post_only: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Limit order. time_in_force: GTC (good-til-cancelled), IOC,
        FOK, or PO (post-only, maker only)."""
        inst = self._instrument(symbol)
        direction = self._direction(side)
        amount = self._check_size(inst, size)
        if price <= 0:
            raise DeriveConfigError(f"price must be positive, got {price}")
        tif = {
            "GTC": TimeInForce.gtc,
            "IOC": TimeInForce.ioc,
            "FOK": TimeInForce.fok,
            "PO": TimeInForce.post_only,
            "POST_ONLY": TimeInForce.post_only,
        }.get(str(time_in_force).upper())
        if tif is None:
            raise DeriveConfigError(
                f"time_in_force must be GTC, IOC, FOK or PO, got {time_in_force!r}"
            )
        if post_only:
            tif = TimeInForce.post_only
        params = {
            "venue": "derive",
            "testnet": self.testnet,
            "symbol": inst.name,
            "side": "buy" if direction == Direction.buy else "sell",
            "size": float(amount),
            "price": float(price),
            "time_in_force": str(time_in_force).upper(),
            "post_only": post_only,
        }
        if self._guard_live(
            dry_run, confirm,
            f"limit {params['side']} {size} {inst.name} @ {price}",
        ):
            logger.info("[DRY-RUN] would place limit order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        order = self._create_order(
            instrument_name=inst.name,
            direction=direction,
            amount=amount,
            limit_price=Decimal(str(price)),
            order_type=OrderType.limit,
            time_in_force=tif,
        )
        return self._result(order, params)

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
        """Bracket: entry plus take-profit and stop-loss triggers.

        Derive has no bracket primitive, so the entry goes on first
        (a market order when entry_price is None, else an IOC limit
        order), and the TP/SL legs follow as separate reduce-only
        trigger orders watching the mark price, only once the entry
        has actually filled. If a leg fails after the entry filled,
        the entry is closed at once with a reduce-only market order;
        if that close fails, the error is raised loudly and the cycle
        stops. When entry_price is given and the IOC entry does not
        fill, no legs are placed at all.
        """
        inst = self._instrument(symbol)
        direction = self._direction(side)
        amount = self._check_size(inst, size)
        params = {
            "venue": "derive",
            "testnet": self.testnet,
            "symbol": inst.name,
            "side": "buy" if direction == Direction.buy else "sell",
            "size": float(amount),
            "entry_price": entry_price,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
        }
        if self._guard_live(
            dry_run, confirm,
            f"bracket {params['side']} {size} {inst.name}",
        ):
            logger.info("[DRY-RUN] would place bracket: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        if entry_price is None:
            entry = self.place_market_order(
                inst.name, params["side"], float(amount),
                dry_run=False, confirm=True,
            )
            filled = float(entry.get("filled_amount") or 0)
            entry_order_id = entry.get("order_id")
        else:
            entry = self.place_limit_order(
                inst.name, params["side"], float(amount), entry_price,
                time_in_force="IOC", dry_run=False, confirm=True,
            )
            filled = float(entry.get("filled_amount") or 0)
            entry_order_id = entry.get("order_id")
        if filled <= 0:
            raise DeriveError(
                f"Bracket entry on {inst.name} did not fill "
                f"(order {entry_order_id}, status {entry.get('status')}); "
                "no TP/SL legs were placed."
            )
        filled_amount = Decimal(str(filled))
        legs = []
        try:
            for trigger, trigger_type in (
                (take_profit, TriggerType.takeprofit),
                (stop_loss, TriggerType.stoploss),
            ):
                if trigger is None:
                    continue
                trigger_dec = Decimal(str(trigger))
                # The leg's limit sits just past its trigger so it
                # still fills when the trigger fires.
                if direction == Direction.buy:
                    limit = trigger_dec * (Decimal("1") - TRIGGER_LIMIT_OFFSET)
                else:
                    limit = trigger_dec * (Decimal("1") + TRIGGER_LIMIT_OFFSET)
                leg = self._create_order(
                    instrument_name=inst.name,
                    direction=(
                        Direction.sell
                        if direction == Direction.buy
                        else Direction.buy
                    ),
                    amount=filled_amount,
                    limit_price=limit,
                    order_type=OrderType.limit,
                    time_in_force=TimeInForce.gtc,
                    reduce_only=True,
                    trigger_price=trigger_dec,
                    trigger_type=trigger_type,
                    label=f"br-{str(entry_order_id)[:8]}",
                )
                legs.append(
                    {
                        "order_id": leg.order_id,
                        "trigger_type": str(trigger_type),
                        "trigger_price": float(trigger_dec),
                        "limit_price": float(limit),
                        "status": str(leg.order_status),
                    }
                )
        except Exception:
            # Half-leg rule: a leg failed after the entry filled, so
            # close the entry at once. If the close fails, that error
            # replaces the leg error. The cycle must stop loudly.
            self.reduce_only_close(
                inst.name, params["side"], float(filled_amount),
                dry_run=False, confirm=True,
            )
            raise
        return {
            "dry_run": False,
            "entry": entry,
            "legs": legs,
            **params,
        }

    def cancel_order(
        self,
        symbol: str,
        order_id: str,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel one open order by its venue order id. Trigger legs
        (stop-loss / take-profit) are cancelled through the trigger
        endpoint; resting orders through the regular one. The adapter
        looks the order up first so it picks the right endpoint."""
        inst = self._instrument(symbol)
        params = {
            "venue": "derive",
            "testnet": self.testnet,
            "symbol": inst.name,
            "order_id": str(order_id),
        }
        if self._guard_live(
            dry_run, confirm, f"cancel order {order_id} on {inst.name}"
        ):
            logger.info("[DRY-RUN] would cancel order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        subaccount_id = self._require_subaccount()
        is_trigger = None
        for order in self.get_open_orders(inst.name):
            if str(order.get("order_id")) == str(order_id):
                is_trigger = bool(order.get("is_trigger"))
                break
        try:
            if is_trigger:
                result = self._sdk.private_api.rpc.cancel_trigger_order(
                    CancelTriggerOrderRequest(
                        order_id=str(order_id),
                        subaccount_id=subaccount_id,
                    )
                )
                cancelled = self._order_dict(result, is_trigger=True)
            else:
                result = self._sdk.active_subaccount.orders.cancel(
                    instrument_name=inst.name,
                    order_id=str(order_id),
                )
                cancelled = self._order_dict(result, is_trigger=False)
        except Exception as err:
            mapped = self._handle_error(err)
            # The order was not where the lookup said: a trigger leg
            # the lookup missed reports 11006 (order_does_not_exist)
            # on the regular endpoint, so try the trigger endpoint.
            if not is_trigger and getattr(mapped, "code", None) == 11006:
                try:
                    result = self._sdk.private_api.rpc.cancel_trigger_order(
                        CancelTriggerOrderRequest(
                            order_id=str(order_id),
                            subaccount_id=subaccount_id,
                        )
                    )
                except Exception as err2:
                    raise self._handle_error(err2)
                cancelled = self._order_dict(result, is_trigger=True)
            else:
                raise mapped
        return {"dry_run": False, "cancelled": cancelled, **params}

    def cancel_all_orders(
        self,
        symbol: str | None = None,
        *,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel every resting order, optionally scoped to one
        symbol. The venue-wide call also clears the pending trigger
        legs (take-profit / stop-loss); the symbol-scoped call clears
        that symbol's resting orders plus its trigger legs one by
        one."""
        params = {
            "venue": "derive",
            "testnet": self.testnet,
            "symbol": symbol.upper() if symbol else "ALL",
        }
        if self._guard_live(
            dry_run, confirm, f"cancel-all on {params['symbol']}"
        ):
            logger.info("[DRY-RUN] would cancel all orders: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        subaccount_id = self._require_subaccount()
        try:
            if symbol is None:
                result = self._sdk.private_api.rpc.cancel_all(
                    CancelAllRequest(
                        subaccount_id=subaccount_id,
                        cancel_trigger_orders=True,
                        cancel_algo_orders=True,
                    )
                )
                return {"dry_run": False, "result": result, **params}
            inst = self._instrument(symbol)
            self._sdk.active_subaccount.orders.cancel_by_instrument(
                instrument_name=inst.name
            )
            cancelled_triggers = []
            for order in self._trigger_orders():
                if order.instrument_name.upper() != inst.name.upper():
                    continue
                self._sdk.private_api.rpc.cancel_trigger_order(
                    CancelTriggerOrderRequest(
                        order_id=order.order_id,
                        subaccount_id=subaccount_id,
                    )
                )
                cancelled_triggers.append(order.order_id)
            return {
                "dry_run": False,
                "result": "ok",
                "cancelled_trigger_orders": cancelled_triggers,
                **params,
            }
        except Exception as err:
            raise self._handle_error(err)

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
        (§14/§16 step 9). `side` is the side of the leg being closed:
        a long closes with a sell, a short with a buy; "buy"/"sell"
        are also accepted as the closing side itself. If this fails,
        surface it and STOP the cycle; never leave a one-legged
        trade."""
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


def connect(testnet: bool = True) -> "DeriveClient":
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to DeriveClient.connect. Default is testnet,
    matching the class; the registry always passes testnet explicitly,
    so pass it every time."""
    return DeriveClient.connect(testnet=testnet)


def _demo() -> None:
    """Read-only demo: public reads + simulated writes. Never live."""
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Derive adapter demo (dry-run only)")
    parser.add_argument("--mainnet", action="store_true", help="Use mainnet (default is testnet)")
    parser.add_argument("--symbol", default="ETH-PERP")
    args = parser.parse_args()

    with DeriveClient.connect(testnet=not args.mainnet) as client:
        print(f"Perps on {'testnet' if client.testnet else 'mainnet'}: {len(client._instruments)}")
        print("Mark price:", client.fetch_mark_price(args.symbol))
        print("Funding APR %:", client.fetch_funding_rate(args.symbol))
        try:
            print("Balance:", json.dumps(client.get_balance(), indent=2))
            print("Positions:", json.dumps(client.get_positions(), indent=2))
        except DeriveError as err:
            print(f"Account reads skipped: {err}")
        print("Simulated market order:",
              json.dumps(client.place_market_order(args.symbol, "buy", 0.1), indent=2))
        print("Simulated bracket:",
              json.dumps(client.place_bracket(args.symbol, "buy", 0.1, None, 2000.0, 1700.0), indent=2))


if __name__ == "__main__":
    _demo()
