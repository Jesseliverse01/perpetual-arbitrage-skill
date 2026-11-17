#!/usr/bin/env python3
"""dYdX live-trading adapter for the perpetual-arbitrage skill.

One more venue on the same shape as the Hyperliquid and Lighter
adapters, so the skill's unified execution cycle can run a Strategy E
leg on dYdX v4. Stdlib plus the official dYdX v4 Python client
(`dydx-v4-client`, https://github.com/dydxprotocol/v4-clients,
v4-client-py-v2) only. No raw signing code lives here: every write
goes through the client's `NodeClient`, and every read through its
`IndexerClient` or the same node.

Safety gates (no exceptions):
  * Every write method defaults to ``dry_run=True`` and touches
    nothing on the venue in that mode: it logs what it would do and
    returns a simulated result.
  * A live order needs BOTH ``dry_run=False`` AND ``confirm=True``.
    A live call without ``confirm=True`` raises DydxConfirmError
    and sends nothing.
  * Keys come ONLY from environment variables, never from a file and
    never hard-coded. The ETH private key is read from
    DYDX_TESTNET_PRIVATE_KEY (testnet) or DYDX_PRIVATE_KEY
    (mainnet); a mnemonic in DYDX_TESTNET_MNEMONIC /
    DYDX_MNEMONIC works instead. The dYdX address is derived from
    the key, never typed in by hand.
  * No mainnet order is ever placed by this module's demo, and the
    module never touches deposit / withdraw / transfer / leverage.

Venue quirks, verified on testnet 2026-11-17:
  * Funding settles every hour, not every 8 hours: the indexer reports
    ``nextFundingRate`` per hour (historical rows land one hour
    apart, and the market row carries ``defaultFundingRate1H``).
    So APR% = rate * 24 * 365 * 100. Using the 8h factor would
    understate the APR by 3x.
  * Orders are signed chain transactions sent over gRPC to a
    validator, not REST calls. Reads come from the indexer REST
    API. The two can disagree for a few seconds after a write; the
    adapter waits for the indexer to catch up where it matters.
  * A market order is a short-term order (good for ~20 blocks) with
    the worst price set from the oracle plus slippage. A limit order
    that must rest is a long-term order, good-til-time up to 90
    days; this adapter uses 28 days. Stop and take-profit legs are
    conditional orders, also good-til-time.
  * Tickers are ``COIN-USD`` (ETH-USD, BTC-USD, PEPE-USD). There is
    no 1000-form on dYdX, so the canonical 1000PEPE maps to
    PEPE-USD, and so on.
  * The testnet indexer answers from this VM (earlier 404s were the
    wrong path: ``/v4/markets`` does not exist; the markets live at
    ``/v4/perpetualMarkets``). The single-subaccount endpoint 404s
    until the subaccount holds something; the list endpoints return
    empty arrays instead, so reads use those.
  * gRPC through this VM's egress proxy is flaky on the proxy's IPv4
    address (the handshake sometimes fails with "Expected 'H'") and
    falls back to IPv6. The adapter uses the client's default
    channel, which tries both families, and never pins the proxy to
    one address.
  * The VM's ``no_proxy`` carries bracketed IPv6 entries (``[::1]``)
    that the client's HTTP layer cannot parse, so the adapter strips
    the brackets in-process before connecting. Nothing else about
    the proxy setup is touched.
  * The testnet faucet answers 202 to the USDC fill and then, for a
    fresh address, nothing lands: three throwaway accounts got their
    10 DV4TNT gas but no USDC after 15 to 60 minutes. The faucet is
    rate-limited per IP (1 native + 2 USDC fills a day) and per
    address. A filled order, a resting bracket, and a live cancel
    are still unverified: the write path below is real and signed,
    but the venue rejects an unfunded order for margin. Fund the
    subaccount first (the app's recharge button credits 300 test
    USDC), then re-run the checks in ``~/workspace/live-build/
    VERIFY-dydx.md``.

Environment (all optional except as noted):
  DYDX_TESTNET_PRIVATE_KEY / DYDX_PRIVATE_KEY
      ETH private key, hex with or without 0x. Trading key only.
  DYDX_TESTNET_MNEMONIC / DYDX_MNEMONIC
      Fallback when no private key is set. One of the two is needed
      for writes; public reads work without either.
  DYDX_TESTNET_SUBACCOUNT_NUMBER / DYDX_SUBACCOUNT_NUMBER
      Subaccount to trade on, default 0.
  DYDX_TESTNET_NODE_URL / DYDX_NODE_URL
      Validator gRPC host. Defaults to the client's testnet node
      (test-dydx-grpc.kingnodes.com) and, on mainnet, to
      oegs.dydx.trade:443 from the official docs.

Sources:
  * Client: https://github.com/dydxprotocol/v4-clients
    (dydx-v4-client 1.1.6, verified install + import 2026-11-17)
  * Docs: https://docs.dydx.xyz/ (endpoints, faucet, order types)
  * Testnet indexer: https://indexer.v4testnet.dydx.exchange
  * Mainnet indexer: https://indexer.dydx.trade
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass

from dydx_v4_client import MAX_CLIENT_ID, OrderFlags
from dydx_v4_client.faucet_client import FaucetClient
from dydx_v4_client.indexer.rest.constants import (
    OrderStatus,
    OrderType,
    PositionStatus,
)
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient
from dydx_v4_client.key_pair import KeyPair
from dydx_v4_client.network import (
    TESTNET,
    TESTNET_FAUCET,
    make_mainnet,
    make_testnet,
)
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.node.market import Market, since_now
from dydx_v4_client.wallet import Wallet
from v4_proto.dydxprotocol.clob.order_pb2 import Order

from venue_base import VenueAdapter, normalize_side

logger = logging.getLogger("dydx_client")

MAINNET_INDEXER_URL = "https://indexer.dydx.trade"
MAINNET_WS_URL = "wss://indexer.dydx.trade/v4/ws"
MAINNET_NODE_URL = "oegs.dydx.trade:443"

# Funding settles hourly on dYdX v4 (see module docstring).
FUNDING_APR_FACTOR = 24 * 365

# USDC is asset 0 on dYdX Chain; one USDC is 10**6 quantums.
USDC_ASSET_ID = 0
USDC_QUANTUMS_PER_USDC = 1_000_000

# Long-term (resting) orders live 28 days; the chain caps at 90.
LONG_TERM_EXPIRY_SECONDS = 28 * 24 * 3600
# Short-term orders live ~20 blocks (the chain caps at 40).
SHORT_TERM_BLOCKS_AHEAD = 20
# Bracket trigger legs sit 0.2% beyond their trigger, same as Lighter.
BRACKET_LIMIT_OFFSET = 0.002


class DydxError(Exception):
    """Base error for this adapter."""


class DydxConfigError(DydxError):
    """Missing configuration (env var, market, size below minimum)."""


class DydxAuthError(DydxError):
    """401 from the venue: stale credentials. Stop the cycle, no retry."""


class DydxServerError(DydxError):
    """5xx from the venue: never auto-retry (the order may already exist)."""


class DydxConfirmError(DydxError):
    """A live write was requested without confirm=True. Nothing sent."""


class DydxNotSignedInError(DydxError):
    """A write was requested but no key is configured in the env."""


def _sanitize_proxy_env():
    """Drop bracketed IPv6 entries from no_proxy in-process.

    This VM's no_proxy carries ``[::1]`` and ``[fd8b:...]`` entries,
    and the client's HTTP layer (httpx) raises InvalidURL on them
    while building its proxy patterns. The plain ``::1`` entries
    stay, so localhost still bypasses the proxy. Nothing else
    about the proxy setup changes.
    """
    for key in ("no_proxy", "NO_PROXY"):
        raw = os.environ.get(key)
        if not raw:
            continue
        fixed = ",".join(
            re.sub(r"^\[(.*)\]$", r"\1", part.strip())
            for part in raw.split(",")
        )
        if fixed != raw:
            os.environ[key] = fixed


@dataclass
class MarketInfo:
    ticker: str
    clob_pair_id: int
    atomic_resolution: int
    status: str
    oracle_price: float | None = None
    next_funding_rate: float | None = None


class DydxClient(VenueAdapter):
    """Sync wrapper around the official async dYdX v4 client.

    Public reads work without keys. Writes are dry-run by default and
    need dry_run=False + confirm=True to touch the venue.
    """

    venue_name = "dydx"
    testnet_url = "https://indexer.v4testnet.dydx.exchange"
    mainnet_url = MAINNET_INDEXER_URL
    env_prefix = "DYDX_"

    def __init__(self, testnet: bool = True):
        super().__init__(testnet)
        self._env_prefix = "DYDX_TESTNET_" if testnet else "DYDX_"
        self._loop = asyncio.new_event_loop()
        self._node: NodeClient | None = None
        self._indexer: IndexerClient | None = None
        self._network = None
        self._wallet: Wallet | None = None
        self._address: str | None = None
        self._markets: dict[str, MarketInfo] = {}
        self._used_client_ids: set[int] = set()
        self._write_lock = asyncio.Lock()
        self.subaccount_number = self._env_int("SUBACCOUNT_NUMBER", default=0)
        self._private_key = os.environ.get(self._env_prefix + "PRIVATE_KEY")
        self._mnemonic = os.environ.get(self._env_prefix + "MNEMONIC")
        self._node_url_override = os.environ.get(self._env_prefix + "NODE_URL")

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, testnet: bool = True) -> "DydxClient":
        """Build a client for testnet or mainnet.

        Reads keys ONLY from the environment (see module docstring).
        Without a key the client still connects and serves public
        reads; writes raise DydxNotSignedInError.
        """
        _sanitize_proxy_env()
        self = cls(testnet=testnet)
        try:
            self._run(self._setup())
        except Exception:
            if not self._loop.is_closed():
                self._loop.close()
            raise
        return self

    async def _setup(self) -> None:
        """Build the node + indexer clients and resolve markets.

        Runs on the owned loop. The wallet is built from the env key
        when one is set; a key whose account is not on-chain yet
        still connects (reads and dry runs work), and the first live
        write refuses with a plain reason until the account is
        funded.
        """
        if self.testnet:
            if self._node_url_override:
                self._network = make_testnet(node_url=self._node_url_override)
            else:
                self._network = TESTNET
        else:
            self._network = make_mainnet(
                node_url=self._node_url_override or MAINNET_NODE_URL,
                rest_indexer=MAINNET_INDEXER_URL,
                websocket_indexer=MAINNET_WS_URL,
            )
        # The node client refreshes the account sequence from the chain
        # before every send; this adapter waits for each broadcast to
        # be included before the next send (see _send_order_async),
        # so a sequence is never reused inside one block.
        self._node = await NodeClient.connect(self._network.node)
        self._indexer = IndexerClient(self._network.rest_indexer)
        await self._load_markets_async()
        if self._private_key or self._mnemonic:
            await self._build_wallet_async()
        else:
            logger.warning(
                "No %sPRIVATE_KEY (or %sMNEMONIC) in the environment, "
                "read-only mode. Writes will raise "
                "DydxNotSignedInError.",
                self._env_prefix,
                self._env_prefix,
            )

    async def _build_wallet_async(self) -> None:
        """Derive the wallet from the env key and fetch its on-chain
        account number and sequence. A key whose account does not
        exist on-chain yet keeps account_number 0, and writes refuse
        until the faucet or the app funds it."""
        if self._private_key:
            key = KeyPair.from_hex(self._private_key.replace("0x", ""))
        else:
            key = KeyPair.from_mnemonic(self._mnemonic)
        address = Wallet(key, 0, 0).address
        account_number, sequence = 0, 0
        try:
            account_number, sequence = await self._node.query_address(address)
        except Exception as err:
            logger.warning(
                "dYdX account %s is not on-chain yet (%s). Reads and dry "
                "runs still work; fund it (faucet or the app's recharge "
                "button) before any live write.",
                address,
                self._short_error(err),
            )
        self._wallet = Wallet(key, account_number, sequence)
        self._address = address
        logger.info(
            "dYdX %s connected, address %s, subaccount %s, account %s",
            "testnet" if self.testnet else "mainnet",
            address,
            self.subaccount_number,
            account_number,
        )

    def close(self) -> None:
        """Close the owned event loop."""
        if not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> "DydxClient":
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
            raise DydxConfigError(
                f"{self._env_prefix}{name} must be an integer, got {raw!r}"
            )

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    @staticmethod
    def _short_error(err: Exception) -> str:
        return f"{type(err).__name__}: {str(err)[:200]}"

    def _handle_api_error(self, err: Exception) -> Exception:
        """Map a venue error to the skill's handling: 401 stops, 5xx
        never retries. gRPC failures surface with their status."""
        import httpx

        if isinstance(err, httpx.HTTPStatusError):
            status = err.response.status_code
            body = err.response.text or str(err)
            if status in (401, 403):
                return DydxAuthError(
                    f"{status} from dYdX: stale credentials. Stop the cycle, "
                    f"do not retry. ({body[:300]})"
                )
            if 500 <= status < 600:
                return DydxServerError(
                    f"{status} from dYdX, NOT auto-retried, the order may "
                    "already have committed on-venue. Venue message "
                    f"verbatim: {body[:500]}"
                )
            return DydxError(f"dYdX HTTP error (status {status}): {body[:500]}")
        import grpc

        if isinstance(err, grpc.RpcError):
            code = err.code() if hasattr(err, "code") else None
            details = err.details() if hasattr(err, "details") else str(err)
            if code is not None and code.name == "UNAUTHENTICATED":
                return DydxAuthError(
                    f"gRPC UNAUTHENTICATED from dYdX: {details[:300]}. Stop "
                    "the cycle, do not retry."
                )
            return DydxError(f"dYdX gRPC error ({code}): {details[:300]}")
        return err if isinstance(err, DydxError) else DydxError(str(err)[:500])

    def _check_tx_response(self, resp, what: str) -> dict:
        """A broadcast that the chain accepted returns code 0. Any
        other code is the venue refusing the order (margin, size,
        sequence): surface the raw log verbatim, never retry."""
        tx_resp = getattr(resp, "tx_response", None)
        code = getattr(tx_resp, "code", None)
        txhash = getattr(tx_resp, "txhash", "") or ""
        raw_log = getattr(tx_resp, "raw_log", "") or ""
        if code is None:
            return {"tx_hash": txhash, "code": None, "raw_log": raw_log}
        if int(code) != 0:
            raise DydxError(
                f"{what} rejected on-chain (code {code}): {raw_log[:500]}"
            )
        return {"tx_hash": txhash, "code": int(code), "raw_log": raw_log}

    async def _load_markets_async(self) -> None:
        """Resolve every perp market live from the indexer."""
        try:
            resp = await self._indexer.markets.get_perpetual_markets()
        except Exception as err:
            raise self._handle_api_error(err)
        self._markets = {}
        for ticker, row in (resp.get("markets") or {}).items():
            self._markets[str(ticker).upper()] = MarketInfo(
                ticker=str(ticker).upper(),
                clob_pair_id=int(row.get("clobPairId", -1)),
                atomic_resolution=int(row.get("atomicResolution", 0)),
                status=str(row.get("status", "")),
                oracle_price=self._to_float(row.get("oraclePrice")),
                next_funding_rate=self._to_float(row.get("nextFundingRate")),
            )
        if not self._markets:
            raise DydxError(
                f"No perp markets returned by {self._network.rest_indexer}"
                "/v4/perpetualMarkets"
            )

    def refresh_markets(self) -> None:
        """Re-resolve markets live (marks and funding refresh too)."""
        self._run(self._load_markets_async())

    async def _refresh_market_async(self, ticker: str) -> MarketInfo:
        """Refresh one market row live and update the cache."""
        try:
            resp = await self._indexer.markets.get_perpetual_markets(ticker)
        except Exception as err:
            raise self._handle_api_error(err)
        row = (resp.get("markets") or {}).get(ticker)
        if not row:
            raise DydxConfigError(f"Unknown perp symbol {ticker!r} on dYdX")
        info = MarketInfo(
            ticker=ticker.upper(),
            clob_pair_id=int(row.get("clobPairId", -1)),
            atomic_resolution=int(row.get("atomicResolution", 0)),
            status=str(row.get("status", "")),
            oracle_price=self._to_float(row.get("oraclePrice")),
            next_funding_rate=self._to_float(row.get("nextFundingRate")),
        )
        self._markets[ticker.upper()] = info
        return info

    @staticmethod
    def _to_float(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _market(self, ticker: str) -> MarketInfo:
        info = self._markets.get(str(ticker).upper())
        if info is None:
            raise DydxConfigError(
                f"Unknown perp symbol {ticker!r} on "
                f"{'testnet' if self.testnet else 'mainnet'} "
                f"(known: {', '.join(sorted(self._markets)[:12])}... "
                f"{len(self._markets)} total)"
            )
        return info

    def _market_proto(self, ticker: str) -> Market:
        """The client's Market builder for one ticker, from the live
        row the indexer returned (quantums and subticks come from
        it)."""
        info = self._market(ticker)
        try:
            resp = self._run(
                self._indexer.markets.get_perpetual_markets(ticker)
            )
        except Exception as err:
            raise self._handle_api_error(err)
        row = (resp.get("markets") or {}).get(ticker)
        if not row:
            raise DydxConfigError(f"Unknown perp symbol {ticker!r} on dYdX")
        return Market(row)

    def _next_client_id(self) -> int:
        for _ in range(100):
            cid = random.randint(0, MAX_CLIENT_ID)
            if cid not in self._used_client_ids:
                self._used_client_ids.add(cid)
                return cid
        raise DydxError("could not pick an unused client order id")

    def _refresh_account(self) -> None:
        """Re-fetch the on-chain account number and sequence for the
        wallet. Needed when the account was created after connect
        (the faucet lands a few seconds after the first connect), so
        a live write is not refused for an account that exists by
        now."""
        if self._wallet is None or self._address is None:
            return
        try:
            account_number, sequence = self._run(
                self._node.query_address(self._address)
            )
        except Exception:
            return
        self._wallet.account_number = account_number
        self._wallet.sequence = sequence

    def _require_signed(self) -> None:
        if self._wallet is None:
            raise DydxNotSignedInError(
                f"No {self._env_prefix}PRIVATE_KEY (or {self._env_prefix}"
                "MNEMONIC) in the environment. Set the key env var to "
                "enable writes."
            )
        if self._wallet.account_number == 0:
            # The account may have been created since connect (faucet
            # lands after the first query); check once more before
            # refusing.
            self._refresh_account()
        if self._wallet.account_number == 0:
            raise DydxConfigError(
                f"dYdX account {self._address} is not on-chain yet. Fund "
                "it first: on testnet, the faucet API or the app's "
                "recharge button (300 test USDC). No order was sent."
            )

    def _guard_live(self, dry_run: bool, confirm: bool, what: str) -> bool:
        """Return True when the call is a simulation; raise otherwise."""
        if dry_run:
            return True
        if not confirm:
            raise DydxConfirmError(
                f"Live {what} refused: pass dry_run=False AND confirm=True. "
                "Nothing was sent."
            )
        self._require_signed()
        return False

    @staticmethod
    def _order_side(side: str) -> Order.Side:
        side = normalize_side(side)
        return Order.Side.SIDE_BUY if side == "buy" else Order.Side.SIDE_SELL

    @staticmethod
    def _closing_side(position_side: str) -> Order.Side:
        """The order side that closes a position: a long closes with
        a sell, a short with a buy (venue_base contract)."""
        side = normalize_side(position_side)
        return Order.Side.SIDE_SELL if side == "buy" else Order.Side.SIDE_BUY

    async def _wait_for_tx_async(self, tx_hash: str,
                                 timeout_s: float = 30.0) -> dict:
        """Wait until a broadcast tx is indexed, then check its
        execution result. A SYNC broadcast only proves CheckTx
        passed; the order can still fail when the block executes it
        (margin moved, state changed). Polls GetTx quietly until the
        tx appears or the timeout passes."""
        from v4_proto.cosmos.tx.v1beta1 import service_pb2_grpc
        from v4_proto.cosmos.tx.v1beta1.service_pb2 import GetTxRequest

        stub = service_pb2_grpc.ServiceStub(self._node.channel)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                resp = stub.GetTx(GetTxRequest(hash=tx_hash))
                if resp is not None and resp.tx is not None:
                    tx_resp = getattr(resp, "tx_response", None)
                    code = getattr(tx_resp, "code", None)
                    raw_log = getattr(tx_resp, "raw_log", "") or ""
                    if code is not None and int(code) != 0:
                        raise DydxError(
                            "Order included on-chain but execution failed "
                            f"(code {code}): {raw_log[:500]}"
                        )
                    return {
                        "tx_hash": tx_hash,
                        "code": int(code) if code is not None else None,
                        "raw_log": raw_log,
                    }
            except DydxError:
                raise
            except Exception:
                pass
            await asyncio.sleep(1)
        logger.warning(
            "tx %s not indexed after %ss; the next send may fail on "
            "sequence until it commits",
            tx_hash, timeout_s,
        )
        return {"tx_hash": tx_hash, "code": None, "raw_log": ""}

    async def _send_order_async(self, order) -> dict:
        """Broadcast one signed order, then wait for it to be
        included and check the execution result. Waiting matters:
        the chain only advances the account sequence once a tx is
        committed, so sending the next order before this one lands
        would reuse the sequence and be rejected. The lock keeps
        sends strictly sequential.
        """
        async with self._write_lock:
            try:
                resp = await self._node.place_order(self._wallet, order)
            except Exception as err:
                raise self._handle_api_error(err)
            checked = self._check_tx_response(resp, "Order")
            if checked.get("tx_hash"):
                return await self._wait_for_tx_async(checked["tx_hash"])
            return checked

    async def _send_cancel_async(self, order_id, good_til_block=None,
                                 good_til_block_time=None) -> dict:
        """Broadcast one signed cancel and wait for inclusion."""
        async with self._write_lock:
            try:
                resp = await self._node.cancel_order(
                    self._wallet,
                    order_id,
                    good_til_block=good_til_block,
                    good_til_block_time=good_til_block_time,
                )
            except Exception as err:
                raise self._handle_api_error(err)
            checked = self._check_tx_response(resp, "Cancel")
            if checked.get("tx_hash"):
                return await self._wait_for_tx_async(checked["tx_hash"])
            return checked

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def symbol_for(self, coin: str) -> str:
        """The symbol dYdX lists for one canonical coin. Tickers are
        COIN-USD, and there is no 1000-form on dYdX, so 1000PEPE
        maps to PEPE-USD (same for the other 1000-form memes)."""
        coin = str(coin).upper()
        if coin.startswith("1000"):
            coin = coin[4:]
        return f"{coin}-USD"

    def get_balance(self) -> dict:
        """USDC in the subaccount plus the gas balance, from the node
        (the chain is the source of truth; the indexer 404s until
        the subaccount holds something). Free collateral and equity
        are added from the indexer when it knows the subaccount."""
        if self._address is None:
            raise DydxConfigError(
                f"No {self._env_prefix}PRIVATE_KEY (or {self._env_prefix}"
                "MNEMONIC) in the environment. Balance needs the "
                "account address, which comes from the key."
            )
        try:
            sub = self._run(
                self._node.get_subaccount(self._address, self.subaccount_number)
            )
        except Exception as err:
            raise self._handle_api_error(err)
        usdc = 0.0
        for pos in sub.asset_positions:
            if pos.asset_id == USDC_ASSET_ID:
                value = pos.quantums_decoded / USDC_QUANTUMS_PER_USDC
                usdc = -value if pos.side == "SELL" else value
        try:
            bals = self._run(self._node.get_account_balances(self._address))
            gas = {
                b.denom: float(b.amount) / 1e18
                for b in bals.balances
                if b.denom in ("adv4tnt", "adydx")
            }
        except Exception:
            gas = {}
        out = {
            "venue": "dydx",
            "testnet": self.testnet,
            "address": self._address,
            "subaccount_number": self.subaccount_number,
            "available_balance_usd": usdc,
            "usdc_balance": usdc,
            "gas_balance": gas,
        }
        try:
            idx = self._run(
                self._indexer.account.get_subaccount(
                    self._address, self.subaccount_number
                )
            )
            subaccount = idx.get("subaccount") or {}
            out["free_collateral_usd"] = self._to_float(
                subaccount.get("freeCollateral")
            )
            out["equity_usd"] = self._to_float(subaccount.get("equity"))
            out["margin_enabled"] = subaccount.get("marginEnabled")
        except Exception:
            # The indexer 404s for a subaccount with no history yet;
            # the node numbers above already answer the question.
            pass
        return out

    def get_positions(self) -> list[dict]:
        """Open perp positions. The indexer rows carry the entry
        price and funding; when the indexer cannot answer, the node
        subaccount is the fallback (no entry price there)."""
        if self._address is None:
            raise DydxConfigError(
                f"No {self._env_prefix}PRIVATE_KEY (or {self._env_prefix}"
                "MNEMONIC) in the environment. Positions need the "
                "account address, which comes from the key."
            )
        try:
            resp = self._run(
                self._indexer.account.get_subaccount_perpetual_positions(
                    self._address,
                    self.subaccount_number,
                    status=PositionStatus.OPEN,
                )
            )
        except Exception as err:
            # A 404 means "no subaccount on the indexer yet": flat.
            import httpx

            if isinstance(err, httpx.HTTPStatusError) and err.response.status_code == 404:
                return []
            return self._positions_from_node()
        out = []
        for pos in resp.get("positions") or []:
            size = float(pos.get("size") or 0)
            if size == 0:
                continue
            out.append(
                {
                    "venue": "dydx",
                    "symbol": pos.get("ticker"),
                    "side": "long" if size > 0 else "short",
                    "size": abs(size),
                    "avg_entry_price": self._to_float(pos.get("entryPrice")),
                    "unrealized_pnl_usd": self._to_float(
                        pos.get("unrealisedPnl")
                    ),
                    "realized_pnl_usd": self._to_float(pos.get("realisedPnl")),
                    "net_funding_usd": self._to_float(pos.get("netFunding")),
                    "status": pos.get("status"),
                }
            )
        return out

    def _positions_from_node(self) -> list[dict]:
        """Open positions straight from the chain (fallback when the
        indexer cannot answer). Entry price is not on-chain, so it
        comes back None."""
        try:
            sub = self._run(
                self._node.get_subaccount(self._address, self.subaccount_number)
            )
        except Exception as err:
            raise self._handle_api_error(err)
        out = []
        for pos in sub.perpetual_positions:
            quantums = pos.quantums_decoded
            if quantums == 0:
                continue
            info = self._markets.get(
                self._ticker_for_perpetual(pos.perpetual_id)
            )
            scale = 10 ** (-info.atomic_resolution) if info else 1
            out.append(
                {
                    "venue": "dydx",
                    "symbol": self._ticker_for_perpetual(pos.perpetual_id),
                    "side": "long" if pos.side == "BUY" else "short",
                    "size": quantums / scale,
                    "avg_entry_price": None,
                    "unrealized_pnl_usd": None,
                    "realized_pnl_usd": None,
                    "net_funding_usd": None,
                    "status": "OPEN",
                }
            )
        return out

    def _ticker_for_perpetual(self, perpetual_id: int) -> str:
        for info in self._markets.values():
            if info.clob_pair_id == int(perpetual_id):
                return info.ticker
        return f"perpetual:{perpetual_id}"

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Resting orders from the indexer. Each row keeps the
        client order id, the order flags and the clob pair id, which
        is what a cancel needs (the venue has no single order id to
        cancel by). Empty list when flat, or when the subaccount has
        no history on the indexer yet."""
        if self._address is None:
            raise DydxConfigError(
                f"No {self._env_prefix}PRIVATE_KEY (or {self._env_prefix}"
                "MNEMONIC) in the environment. Orders need the "
                "account address, which comes from the key."
            )
        ticker = self.symbol_for(symbol) if symbol else None
        try:
            resp = self._run(
                self._indexer.account.get_subaccount_orders(
                    self._address,
                    self.subaccount_number,
                    ticker=ticker,
                    status=OrderStatus.OPEN,
                )
            )
        except Exception as err:
            import httpx

            if isinstance(err, httpx.HTTPStatusError) and err.response.status_code == 404:
                return []
            raise self._handle_api_error(err)
        orders = resp if isinstance(resp, list) else resp.get("orders", resp)
        out = []
        for row in orders or []:
            side = str(row.get("side", "")).upper()
            out.append(
                {
                    "id": row.get("id"),
                    "client_order_id": int(row.get("clientId", 0)),
                    "order_flags": int(row.get("orderFlags", 0)),
                    "clob_pair_id": int(row.get("clobPairId", -1)),
                    "symbol": row.get("ticker"),
                    "side": "buy" if side == "BUY" else "sell",
                    "size": float(row.get("size") or 0),
                    "filled_size": float(row.get("totalFilled") or 0),
                    "price": self._to_float(row.get("price")),
                    "type": row.get("type"),
                    "status": row.get("status"),
                    "reduce_only": bool(row.get("reduceOnly")),
                    "trigger_price": self._to_float(row.get("triggerPrice")),
                    "good_til_block": row.get("goodTilBlock"),
                    "good_til_block_time": row.get("goodTilBlockTime"),
                }
            )
        return out

    def fetch_funding_rate(self, symbol: str) -> float:
        """Current funding APR for a symbol, as a float in percent.

        dYdX funding settles hourly, so APR% = rate * 24 * 365 * 100
        on the indexer's ``nextFundingRate`` (the rate for the next
        hourly payment). Falls back to the latest historical funding
        row when the live field is missing. Positive means longs pay
        shorts; negative means shorts pay longs.
        """
        ticker = self.symbol_for(symbol)
        info = self._run(self._refresh_market_async(ticker))
        rate = info.next_funding_rate
        if rate is None:
            try:
                hist = self._run(
                    self._indexer.markets.get_perpetual_market_historical_funding(
                        ticker, limit=1
                    )
                )
                rows = hist.get("historicalFunding") or []
                if rows:
                    rate = self._to_float(rows[0].get("rate"))
            except Exception as err:
                raise self._handle_api_error(err)
        if rate is None:
            raise DydxError(f"No funding row for {ticker} on dYdX")
        return float(rate) * FUNDING_APR_FACTOR * 100

    def fetch_mark_price(self, symbol: str) -> float:
        """Mark price for a perp, as a float. The venue's oracle
        price, refreshed live; the top-of-book mid is the fallback
        when the oracle field is missing."""
        ticker = self.symbol_for(symbol)
        info = self._run(self._refresh_market_async(ticker))
        if info.oracle_price is not None:
            return info.oracle_price
        try:
            book = self._run(
                self._indexer.markets.get_perpetual_market_orderbook(ticker)
            )
        except Exception as err:
            raise self._handle_api_error(err)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise DydxError(f"Empty book for {ticker}")
        return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2

    # ------------------------------------------------------------------
    # writes: dry_run=True by default; live needs dry_run=False + confirm
    # ------------------------------------------------------------------
    def _build_order(self, market: Market, ticker: str, side: str,
                     size: float, price: float, order_type: str,
                     flags: int, reduce_only: bool = False,
                     post_only: bool = False,
                     trigger_price: float | None = None,
                     condition_type=None,
                     time_in_force: Order.TimeInForce | None = None):
        """Build one signed order proto. Market orders are short-term
        (good ~20 blocks); resting and conditional orders are
        long-term / conditional with a 28-day good-til-time."""
        from dydx_v4_client.indexer.rest.constants import OrderExecution

        info = self._market(ticker)
        order_id = market.order_id(
            self._address, self.subaccount_number,
            self._next_client_id(), flags,
        )
        if flags == OrderFlags.SHORT_TERM:
            current_block = self._run(self._node.latest_block_height())
            good_til_block = current_block + SHORT_TERM_BLOCKS_AHEAD
            good_til_block_time = None
        else:
            good_til_block = None
            good_til_block_time = since_now(seconds=LONG_TERM_EXPIRY_SECONDS)
        if time_in_force is None:
            if order_type == OrderType.MARKET:
                time_in_force = Order.TimeInForce.TIME_IN_FORCE_IOC
            elif post_only:
                time_in_force = Order.TimeInForce.TIME_IN_FORCE_POST_ONLY
            else:
                time_in_force = Order.TimeInForce.TIME_IN_FORCE_UNSPECIFIED
        execution = OrderExecution.DEFAULT
        return market.order(
            order_id=order_id,
            order_type=order_type,
            side=self._order_side(side),
            size=size,
            price=price,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            post_only=post_only,
            execution=execution,
            good_til_block=good_til_block,
            good_til_block_time=good_til_block_time,
            condition_type=condition_type,
            conditional_order_trigger_subticks=(
                market.calculate_subticks(trigger_price)
                if trigger_price is not None else 0
            ),
        ), order_id

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
        """Market order. The worst price is the oracle plus slippage
        for buys, minus slippage for sells. Set reduce_only=True only
        to close a position (rollback)."""
        ticker = self.symbol_for(symbol)
        market = self._market_proto(ticker)
        oracle = self.fetch_mark_price(symbol)
        if normalize_side(side) == "buy":
            worst = oracle * (1 + slippage_pct / 100)
        else:
            worst = oracle * (1 - slippage_pct / 100)
        params = {
            "venue": "dydx",
            "testnet": self.testnet,
            "symbol": ticker,
            "side": normalize_side(side),
            "size": size,
            "oracle_price": oracle,
            "worst_price": worst,
            "reduce_only": reduce_only,
        }
        if self._guard_live(dry_run, confirm, f"market {params['side']} {size} {ticker}"):
            logger.info("[DRY-RUN] would place market order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        order, order_id = self._build_order(
            market, ticker, side, size, worst, OrderType.MARKET,
            OrderFlags.SHORT_TERM, reduce_only=reduce_only,
        )
        result = self._run(self._send_order_async(order))
        return {
            "dry_run": False,
            "client_order_id": order_id.client_id,
            "order_flags": order_id.order_flags,
            "clob_pair_id": order_id.clob_pair_id,
            **result,
            **params,
        }

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
        """Limit order. time_in_force: GTC (rests up to 28 days),
        IOC or FOK (short-term, good ~20 blocks), PO (post-only,
        rests)."""
        ticker = self.symbol_for(symbol)
        market = self._market_proto(ticker)
        tif = str(time_in_force).upper()
        if tif in ("GTC", "GTT"):
            flags = OrderFlags.LONG_TERM
            order_tif = Order.TimeInForce.TIME_IN_FORCE_UNSPECIFIED
        elif tif == "IOC":
            flags = OrderFlags.SHORT_TERM
            order_tif = Order.TimeInForce.TIME_IN_FORCE_IOC
        elif tif == "FOK":
            flags = OrderFlags.SHORT_TERM
            order_tif = Order.TimeInForce.TIME_IN_FORCE_FILL_OR_KILL
        elif tif == "PO":
            flags = OrderFlags.LONG_TERM
            post_only = True
            order_tif = Order.TimeInForce.TIME_IN_FORCE_POST_ONLY
        else:
            raise DydxConfigError(
                f"time_in_force must be GTC, IOC, FOK or PO, got {time_in_force!r}"
            )
        params = {
            "venue": "dydx",
            "testnet": self.testnet,
            "symbol": ticker,
            "side": normalize_side(side),
            "size": size,
            "price": price,
            "time_in_force": tif,
            "post_only": post_only,
        }
        if self._guard_live(dry_run, confirm, f"limit {params['side']} {size} {ticker} @ {price}"):
            logger.info("[DRY-RUN] would place limit order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        order, order_id = self._build_order(
            market, ticker, side, size, price, OrderType.LIMIT, flags,
            post_only=post_only, time_in_force=order_tif,
        )
        result = self._run(self._send_order_async(order))
        return {
            "dry_run": False,
            "client_order_id": order_id.client_id,
            "order_flags": order_id.order_flags,
            "clob_pair_id": order_id.clob_pair_id,
            **result,
            **params,
        }

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
        """Entry plus take-profit and stop-loss legs.

        dYdX has no grouped bracket: the entry goes first, then the
        TP and SL as separate conditional reduce-only orders. Entry
        is a market order when entry_price is None, else a limit
        order. The TP/SL limits sit 0.2% beyond their triggers so a
        trigger still fills. Half-leg rule: when a market entry
        fills and a leg then fails, the entry is closed at once with
        a reduce-only market order; when the entry does not fill,
        no legs are placed at all.
        """
        ticker = self.symbol_for(symbol)
        market = self._market_proto(ticker)
        params = {
            "venue": "dydx",
            "testnet": self.testnet,
            "symbol": ticker,
            "side": normalize_side(side),
            "size": size,
            "entry_price": entry_price,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
        }
        if self._guard_live(dry_run, confirm, f"bracket {params['side']} {size} {ticker}"):
            logger.info("[DRY-RUN] would place bracket: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        if take_profit is None and stop_loss is None:
            raise DydxConfigError(
                "place_bracket needs at least one of take_profit / stop_loss"
            )
        # -- entry ---------------------------------------------------
        if entry_price is None:
            entry = self.place_market_order(
                symbol, side, size, dry_run=False, confirm=True
            )
            entry_filled = self._wait_for_position(ticker, size)
            if not entry_filled:
                raise DydxError(
                    f"Bracket entry on {ticker} did not fill within 10s; "
                    "no TP/SL legs placed. Check the position before "
                    "retrying."
                )
        else:
            entry = self.place_limit_order(
                symbol, side, size, entry_price,
                dry_run=False, confirm=True,
            )
        # -- legs ----------------------------------------------------
        legs = []
        try:
            if take_profit is not None:
                legs.append(
                    self._place_trigger_leg(
                        market, ticker, side, size, take_profit,
                        OrderType.TAKE_PROFIT_LIMIT,
                        Order.ConditionType.CONDITION_TYPE_TAKE_PROFIT,
                    )
                )
            if stop_loss is not None:
                legs.append(
                    self._place_trigger_leg(
                        market, ticker, side, size, stop_loss,
                        OrderType.STOP_LIMIT,
                        Order.ConditionType.CONDITION_TYPE_STOP_LOSS,
                    )
                )
        except Exception as err:
            if entry_price is None:
                # Entry filled and a leg failed: close at once.
                try:
                    self.reduce_only_close(
                        symbol, side, size, dry_run=False, confirm=True
                    )
                except Exception as close_err:
                    raise DydxError(
                        f"Bracket leg failed ({self._short_error(err)}) and "
                        "the rollback close ALSO failed "
                        f"({self._short_error(close_err)}). STOP the cycle: "
                        "a one-legged trade may be open."
                    )
            if isinstance(err, DydxError):
                raise err
            raise self._handle_api_error(err)
        return {
            "dry_run": False,
            "entry": entry,
            "legs": legs,
            **params,
        }

    def _place_trigger_leg(self, market: Market, ticker: str, side: str,
                           size: float, trigger: float, order_type: str,
                           condition_type) -> dict:
        """One conditional reduce-only leg on the opposite side of
        the entry, with its limit 0.2% beyond the trigger."""
        opposite = "sell" if normalize_side(side) == "buy" else "buy"
        if opposite == "sell":
            limit = trigger * (1 - BRACKET_LIMIT_OFFSET)
        else:
            limit = trigger * (1 + BRACKET_LIMIT_OFFSET)
        order, order_id = self._build_order(
            market, ticker, opposite, size, limit, order_type,
            OrderFlags.CONDITIONAL, reduce_only=True,
            trigger_price=trigger, condition_type=condition_type,
        )
        result = self._run(self._send_order_async(order))
        return {
            "dry_run": False,
            "client_order_id": order_id.client_id,
            "order_flags": order_id.order_flags,
            "clob_pair_id": order_id.clob_pair_id,
            "side": opposite,
            "trigger_price": trigger,
            "limit_price": limit,
            "type": order_type,
            **result,
        }

    def _wait_for_position(self, ticker: str, size: float,
                           timeout_s: float = 10.0) -> bool:
        """Poll for the position a market entry should have opened.
        The indexer lags the chain by a few seconds, so this waits
        rather than trusting the first empty read."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for pos in self.get_positions():
                if str(pos.get("symbol", "")).upper() == ticker.upper():
                    if abs(float(pos.get("size") or 0) - size) < size * 0.01 + 1e-9:
                        return True
            time.sleep(2)
        return False

    def _resolve_order_id(self, symbol: str, order_id) -> tuple:
        """Turn what cancel_order was given into (client_id, flags,
        clob_pair_id). Accepts an int client id (looked up among the
        open orders on that market) or the indexer's order id string
        (address-subaccount-clientId-clobPairId-flags)."""
        ticker = self.symbol_for(symbol)
        if isinstance(order_id, int):
            for row in self.get_open_orders(symbol):
                if int(row.get("client_order_id", -1)) == order_id:
                    return (
                        int(row["client_order_id"]),
                        int(row["order_flags"]),
                        int(row["clob_pair_id"]),
                    )
            raise DydxConfigError(
                f"No open order with client id {order_id} on {ticker}"
            )
        parts = str(order_id).split("-")
        if len(parts) == 5:
            try:
                return int(parts[2]), int(parts[4]), int(parts[3])
            except ValueError:
                pass
        raise DydxConfigError(
            f"Cannot parse order id {order_id!r}. Pass the client order "
            "id (int) or the indexer's order id string."
        )

    def cancel_order(
        self,
        symbol: str,
        order_id,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel one order by its client order id (int) or the
        indexer's order id string. Short-term orders cancel with a
        good-til-block; long-term and conditional orders with a
        good-til-time."""
        ticker = self.symbol_for(symbol)
        params = {
            "venue": "dydx",
            "testnet": self.testnet,
            "symbol": ticker,
            "order_id": order_id,
        }
        if self._guard_live(dry_run, confirm, f"cancel order {order_id} on {ticker}"):
            logger.info("[DRY-RUN] would cancel order: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        client_id, flags, clob_pair_id = self._resolve_order_id(symbol, order_id)
        market = self._market_proto(ticker)
        oid = market.order_id(
            self._address, self.subaccount_number, client_id, flags
        )
        if flags == OrderFlags.SHORT_TERM:
            current_block = self._run(self._node.latest_block_height())
            result = self._run(self._send_cancel_async(
                oid, good_til_block=current_block + SHORT_TERM_BLOCKS_AHEAD + 10,
            ))
        else:
            result = self._run(self._send_cancel_async(
                oid, good_til_block_time=since_now(seconds=120),
            ))
        return {
            "dry_run": False,
            "client_order_id": client_id,
            "order_flags": flags,
            "clob_pair_id": clob_pair_id,
            **result,
            **params,
        }

    def cancel_all_orders(
        self,
        symbol: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Cancel every resting order, optionally scoped to one
        market. Cancels one by one: the chain's batch cancel only
        covers short-term orders, and the bracket legs are
        conditional, so they need individual cancels."""
        orders = self.get_open_orders(symbol)
        params = {
            "venue": "dydx",
            "testnet": self.testnet,
            "symbol": self.symbol_for(symbol) if symbol else "ALL",
            "order_count": len(orders),
        }
        if self._guard_live(dry_run, confirm, f"cancel-all on {params['symbol']}"):
            logger.info("[DRY-RUN] would cancel all orders: %s", params)
            return {"dry_run": True, "simulated": True, **params}
        cancelled = []
        for row in orders:
            cancelled.append(
                self.cancel_order(
                    row.get("symbol") or symbol,
                    int(row["client_order_id"]),
                    dry_run=False, confirm=True,
                )
            )
        return {"dry_run": False, "cancelled": cancelled, **params}

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
        """Reduce-only market close, the half-leg rollback primitive.
        ``side`` accepts both spellings the callers use: "buy"/"sell"
        is already the closing order side, while "long"/"short" names
        the leg being closed (a long closes with a sell, a short with
        a buy). If this fails, surface it and STOP the cycle; never
        leave a one-legged trade."""
        s = str(side).strip().lower()
        if s in ("buy", "sell"):
            close_side = s
        elif s == "long":
            close_side = "sell"
        elif s == "short":
            close_side = "buy"
        else:
            raise DydxConfigError(
                f"side must be buy/sell (or long/short), got {side!r}"
            )
        return self.place_market_order(
            symbol,
            close_side,
            size,
            slippage_pct=slippage_pct,
            dry_run=dry_run,
            confirm=confirm,
            reduce_only=True,
        )

    # ------------------------------------------------------------------
    # one-time setup (testnet only; key never stored by us)
    # ------------------------------------------------------------------
    def fund_testnet_account(self, amount_usdc: float = 2000) -> dict:
        """Ask the testnet faucet for USDC + gas for this account
        (testnet only). Returns the faucet's answers. The faucet
        replies 202 and funds in its own time; check get_balance
        afterwards. A 202 is not a funded account. Rate limits: 1
        native + 2 USDC fills per IP per day, per address."""
        if not self.testnet:
            raise DydxConfigError("The faucet only exists on testnet.")
        if self._address is None:
            raise DydxConfigError(
                "No key in the environment, so there is no address to fund."
            )

        async def _fill():
            faucet = FaucetClient(TESTNET_FAUCET)
            out = {}
            for name, coro in (
                ("native_dv4tnt", faucet.fill_native(self._address)),
                ("usdc", faucet.fill(
                    self._address, self.subaccount_number, amount_usdc
                )),
            ):
                try:
                    resp = await coro
                    out[name] = {
                        "status_code": resp.status_code,
                        "body": resp.text[:200],
                    }
                except Exception as err:
                    out[name] = {"error": self._short_error(err)}
            return out

        return {
            "address": self._address,
            "subaccount_number": self.subaccount_number,
            "amount_usdc": amount_usdc,
            "faucet": self._run(_fill()),
            "note": "A 202 means the faucet accepted the request, not "
            "that the funds landed. Check get_balance() before trading.",
        }


def connect(testnet: bool = True) -> "DydxClient":
    """Module-level connect, so the registry can load every venue the
    same way. Delegates to DydxClient.connect. Default is testnet."""
    return DydxClient.connect(testnet=testnet)


def _demo() -> None:
    """Read-only demo: public reads + simulated writes. Never live."""
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="dYdX adapter demo (dry-run only)")
    parser.add_argument("--mainnet", action="store_true", help="Use mainnet (default is testnet)")
    parser.add_argument("--symbol", default="ETH")
    args = parser.parse_args()

    with DydxClient.connect(testnet=not args.mainnet) as client:
        print(f"Markets on {'testnet' if client.testnet else 'mainnet'}: {len(client._markets)} perps")
        print("Mark price:", client.fetch_mark_price(args.symbol))
        print("Funding APR %:", client.fetch_funding_rate(args.symbol))
        try:
            print("Balance:", json.dumps(client.get_balance(), indent=2))
            print("Positions:", json.dumps(client.get_positions(), indent=2))
            print("Open orders:", json.dumps(client.get_open_orders(), indent=2))
        except DydxConfigError as err:
            print(f"Account reads skipped: {err}")
        print("Simulated market order:",
              json.dumps(client.place_market_order(args.symbol, "buy", 0.01), indent=2))
        print("Simulated bracket:",
              json.dumps(client.place_bracket(args.symbol, "buy", 0.01, 1800.0, 1900.0, 1700.0), indent=2))


if __name__ == "__main__":
    _demo()
