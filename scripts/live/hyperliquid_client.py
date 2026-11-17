#!/usr/bin/env python3
"""Hyperliquid live-trading adapter for the perpetual-arbitrage skill.

Implements the Hyperliquid half of the unified execution API the skill
describes in SKILL.md section 14, under the rules of sections 13 and 16.

What it is
----------
A thin, explicit adapter over hyperliquid-python-sdk 0.24.0. One venue,
one account, one signing flow. It exposes the capabilities section 14
requires for the cycle (section 16): balances, open positions, open
orders (SL/TP audit), funding rate, mark price, single-order placement,
bracket placement (entry + take-profit + stop-loss), and cancel.

What it is NOT
--------------
- It is not a trading bot. It places one order when told to, and
  nothing on its own. There is no loop, no scheduler, no auto-retry.
- It never touches deposit / withdraw / transfer / leverage /
  margin-mode. Those are explicit user-initiated operations, surfaced
  separately (SKILL.md section 14).
- It never places a mainnet order unless the caller explicitly asks
  for mainnet AND passes confirm=True. Testnet is the default.

Safety rules (SKILL.md section 14, enforced in code)
----------------------------------------------------
1. Credentials come ONLY from environment variables:
   HL_TESTNET_PRIVATE_KEY (testnet) / HL_PRIVATE_KEY (mainnet).
   Nothing is hardcoded, nothing is written to any file, nothing is
   logged. The key lives in memory for the life of the process.
2. Every write method defaults to dry_run=True. With dry_run on, the
   method logs what it WOULD do and returns a simulated result. It
   makes no venue call and moves no funds.
3. A live order requires dry_run=False AND confirm=True. Either one
   missing and the call is refused with a ValueError before any
   network traffic.
4. A write failing with 401 means stale credentials: stop the cycle,
   surface it, do not retry. A write failing with 5xx: do NOT retry
   automatically (the original may have committed on-venue, which
   would double-open). Both surface verbatim as exceptions.
5. Half-leg risk: if one leg of a bracket fills and another fails,
   roll back immediately with a reduce-only market close on the
   filled leg. If the rollback itself fails, raise
   EmergencyUnwindRequired and STOP. Never leave a one-legged trade.

Venue quirks (SKILL.md section 14)
-----------------------------------
- Bracket prices need max 5 significant figures (and max
  6 - baseDecimals decimal places). Prices are rounded before
  sending; sizes are rounded DOWN to the asset's szDecimals.
- Hyperliquid is USDC-margined. Funding APR = hourly rate x 24 x 365.

Strategy E defaults used here (SKILL.md section 13)
----------------------------------------------------
- Leg collateral $10, leg notional $50, effective leverage 5x.
- Stop-loss -5% of (leg collateral x 2) = -$1.00 net.
- Entry band 70-120% net APR, hard cap 200%.

Usage
-----
    export HL_TESTNET_PRIVATE_KEY=0x...   # testnet-only key
    python3 -c "
    from hyperliquid_client import connect
    c = connect(testnet=True)
    print(c.get_balance())
    print(c.fetch_mark_price('HYPE'))
    print(c.place_market_order('HYPE', is_buy=True, sz=1.0))  # dry run
    "

Dependencies: hyperliquid-python-sdk 0.24.0 (and its deps) only.
"""

import logging
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import OrderType

logger = logging.getLogger(__name__)

# Strategy E defaults (SKILL.md section 13).
DEFAULT_LEG_COLLATERAL_USD = 10.0
DEFAULT_LEG_NOTIONAL_USD = 50.0
DEFAULT_LEVERAGE = 5
DEFAULT_SLIPPAGE = 0.05  # 5% worst-case slippage on market orders

# Hyperliquid price precision (SKILL.md section 14).
MAX_SIG_FIGS = 5


class HyperliquidError(Exception):
    """Base error for this adapter. Carries the venue error verbatim."""


class StaleCredentialsError(HyperliquidError):
    """A write failed with 401: credentials are stale. Stop, do not retry."""


class VenueServerError(HyperliquidError):
    """A write failed with 5xx. Do NOT auto-retry: the original order
    may already have committed on-venue."""


class EmergencyUnwindRequired(HyperliquidError):
    """A leg filled and the rollback close failed. The position is live
    and one-legged. Surface this and STOP the cycle immediately."""


class RefusedError(HyperliquidError):
    """The call was refused before any venue traffic: dry-run off
    without an explicit confirm, or a missing credential."""


def round_to_sig_figs(px, sig_figs=MAX_SIG_FIGS):
    """Round a price to at most `sig_figs` significant figures.

    Hyperliquid rejects bracket (and limit) prices with more than 5
    significant figures. Decimal quantize keeps this exact.
    """
    px = float(px)
    if px == 0:
        return 0.0
    d = Decimal(repr(px))
    quantum = Decimal(1).scaleb(d.adjusted() - sig_figs + 1)
    return float(d.quantize(quantum, rounding=ROUND_HALF_UP))


def round_price_for_asset(px, sz_decimals):
    """Round a price for one asset: max 5 sig figs AND at most
    (6 - szDecimals) decimal places (SKILL.md section 14)."""
    px = round_to_sig_figs(px)
    max_decimals = 6 - int(sz_decimals)
    if max_decimals < 0:
        max_decimals = 0
    quantum = Decimal(1).scaleb(-max_decimals)
    return float(Decimal(repr(px)).quantize(quantum, rounding=ROUND_HALF_UP))


def round_size_down(sz, sz_decimals):
    """Round a size DOWN to the asset's szDecimals. Never round up:
    rounding up could exceed the intended notional or the margin."""
    quantum = Decimal(1).scaleb(-int(sz_decimals))
    return float(Decimal(repr(sz)).quantize(quantum, rounding=ROUND_DOWN))


class HyperliquidClient:
    """One Hyperliquid account on one network (testnet or mainnet)."""

    def __init__(self, testnet, address, info, exchange):
        self.testnet = testnet
        self.network = "testnet" if testnet else "mainnet"
        self.address = address
        self._info = info
        self._exchange = exchange
        self._meta = None  # cached {coin: {"szDecimals": int, "maxLeverage": int}}

    # -- setup -----------------------------------------------------

    def _load_meta(self):
        if self._meta is None:
            meta = self._info.meta()
            self._meta = {
                u["name"]: {
                    "szDecimals": int(u["szDecimals"]),
                    "maxLeverage": int(u["maxLeverage"]),
                }
                for u in meta["universe"]
            }
        return self._meta

    def asset_meta(self, coin):
        """szDecimals / maxLeverage for one asset. Raises KeyError if
        the asset does not exist on this network."""
        meta = self._load_meta()
        if coin not in meta:
            raise HyperliquidError(f"unknown asset on {self.network}: {coin}")
        return meta[coin]

    # -- reads (public or account state; no key needed for public) --

    def get_balance(self):
        """Withdrawable USDC, account value and margin used.

        Returns dict: withdrawable_usd, account_value_usd,
        margin_used_usd, total_position_value_usd.
        """
        state = self._info.user_state(self.address)
        summary = state.get("marginSummary", {})
        return {
            "withdrawable_usd": float(state.get("withdrawable", 0.0)),
            "account_value_usd": float(summary.get("accountValue", 0.0)),
            "margin_used_usd": float(summary.get("totalMarginUsed", 0.0)),
            "total_position_value_usd": float(summary.get("totalNtlPos", 0.0)),
        }

    def get_positions(self):
        """Open positions, with funding accrued and liquidation price.

        Returns a list of dicts: coin, size, entry_px, mark_px,
        leverage, liquidation_px, margin_used_usd, unrealized_pnl_usd,
        funding_since_open_usd, funding_all_time_usd.
        """
        state = self._info.user_state(self.address)
        out = []
        for entry in state.get("assetPositions", []):
            p = entry["position"]
            lev = p.get("leverage", {})
            cum = p.get("cumFunding", {})
            out.append({
                "coin": p["coin"],
                "size": float(p["szi"]),
                "entry_px": float(p["entryPx"]),
                "leverage": int(lev.get("value", 0)),
                "leverage_type": lev.get("type"),
                "liquidation_px": (
                    float(p["liquidationPx"]) if p.get("liquidationPx") else None
                ),
                "margin_used_usd": float(p.get("marginUsed", 0.0)),
                "position_value_usd": float(p.get("positionValue", 0.0)),
                "unrealized_pnl_usd": float(p.get("unrealizedPnl", 0.0)),
                "funding_since_open_usd": float(cum.get("sinceOpen", 0.0)),
                "funding_all_time_usd": float(cum.get("allTime", 0.0)),
            })
        return out

    def get_open_orders(self):
        """Outstanding orders, for the SL/TP audit (SKILL.md section 14).

        Returns a list of dicts: oid, coin, side, size, limit_px,
        order_type, trigger_px, trigger_condition, is_trigger,
        reduce_only.
        """
        orders = self._info.open_orders(self.address)
        out = []
        for o in orders:
            otype = o.get("orderType", {})
            trigger = otype.get("Trigger", {})
            out.append({
                "oid": int(o["oid"]),
                "coin": o["coin"],
                "side": o["side"],
                "size": float(o["sz"]),
                "limit_px": float(o["limitPx"]),
                "order_type": "trigger" if trigger else "limit",
                "trigger_px": float(trigger["triggerPx"]) if trigger else None,
                "trigger_condition": trigger.get("tpsl") if trigger else None,
                "is_trigger": bool(trigger),
                "reduce_only": bool(o.get("reduceOnly", False)),
            })
        return out

    def fetch_mark_price(self, coin):
        """Current mark price for one asset (float)."""
        _meta, ctxs = self._info.meta_and_asset_ctxs()
        universe = self._load_meta()
        if coin not in universe:
            raise HyperliquidError(f"unknown asset on {self.network}: {coin}")
        idx = list(universe.keys()).index(coin)
        ctx = ctxs[idx]
        if ctx.get("markPx"):
            return float(ctx["markPx"])
        mids = self._info.all_mids()
        return float(mids[coin])

    def fetch_funding_rate(self, coin):
        """Current hourly funding rate and its APR for one asset.

        Returns dict: coin, funding_rate_hourly, funding_apr_pct.
        APR = hourly rate x 24 x 365 (Hyperliquid funds hourly).
        """
        _meta, ctxs = self._info.meta_and_asset_ctxs()
        universe = self._load_meta()
        if coin not in universe:
            raise HyperliquidError(f"unknown asset on {self.network}: {coin}")
        idx = list(universe.keys()).index(coin)
        rate = float(ctxs[idx].get("funding", 0.0))
        return {
            "coin": coin,
            "funding_rate_hourly": rate,
            "funding_apr_pct": rate * 24 * 365 * 100,
        }

    # -- writes ----------------------------------------------------
    # Every write defaults to dry_run=True and requires confirm=True
    # to go live. A live call that fails with 401 raises
    # StaleCredentialsError (stop, no retry); 5xx raises
    # VenueServerError (do NOT auto-retry).

    def _check_live(self, dry_run, confirm, action):
        if dry_run:
            return False
        if not confirm:
            raise RefusedError(
                f"live {action} refused: confirm=True is required "
                "(dry_run=False alone is not enough)"
            )
        return True

    def _call(self, fn, action):
        """Run one venue write, classifying failures per SKILL.md 14.

        The venue answers a rejected write in two ways: an HTTP error
        (401 / 5xx, classified below) or a 200 with a top-level
        {"status": "err", "response": "<reason>"} body. The second
        case is just as much a refusal — raise it with the venue's
        reason verbatim instead of returning an "unknown" result.
        """
        try:
            result = fn()
        except Exception as e:
            msg = str(e)
            status = getattr(e, "status_code", None)
            if status is None and hasattr(e, "response"):
                status = getattr(e.response, "status_code", None)
            if status == 401 or "401" in msg[:80]:
                raise StaleCredentialsError(
                    f"{action} failed with 401 (stale credentials). "
                    "Stop the cycle, do not retry. Venue error: " + msg
                ) from e
            if status is not None and 500 <= int(status) < 600:
                raise VenueServerError(
                    f"{action} failed with {status}. Do NOT auto-retry: "
                    "the original may have committed on-venue. "
                    "Venue error: " + msg
                ) from e
            if "500" in msg[:80] or "502" in msg[:80] or "503" in msg[:80]:
                raise VenueServerError(
                    f"{action} failed with a 5xx. Do NOT auto-retry: "
                    "the original may have committed on-venue. "
                    "Venue error: " + msg
                ) from e
            raise HyperliquidError(f"{action} failed. Venue error: {msg}") from e
        if isinstance(result, dict) and result.get("status") == "err":
            raise HyperliquidError(
                f"{action} rejected by venue: {result.get('response')}"
            )
        return result

    def place_market_order(self, coin, is_buy, sz, dry_run=True, confirm=False,
                           slippage=DEFAULT_SLIPPAGE):
        """Market order, filled at the current book with `slippage`
        worst-case tolerance. `sz` is in base units (e.g. 1.0 HYPE).

        Dry run: logs the would-be order, returns a simulated result.
        Live: requires confirm=True.
        """
        meta = self.asset_meta(coin)
        sz = round_size_down(sz, meta["szDecimals"])
        if sz <= 0:
            raise HyperliquidError(f"size rounds to zero for {coin}")
        side = "BUY" if is_buy else "SELL"
        live = self._check_live(dry_run, confirm, f"market {side} {sz} {coin}")
        if not live:
            mark = self.fetch_mark_price(coin)
            logger.info("[DRY RUN] market %s %s %s @ ~%s (%s)",
                        side, sz, coin, mark, self.network)
            return {
                "dry_run": True, "simulated": True, "network": self.network,
                "action": "market_order", "coin": coin, "side": side,
                "size": sz, "approx_px": mark,
            }
        result = self._call(
            lambda: self._exchange.market_open(coin, is_buy, sz, slippage=slippage),
            f"market {side} {sz} {coin}",
        )
        return self._order_result(result, coin, side, sz, "market")

    def place_limit_order(self, coin, is_buy, sz, price, dry_run=True,
                          confirm=False, tif="Gtc"):
        """Limit order at `price` (rounded to 5 sig figs for the asset).

        Dry run: logs the would-be order, returns a simulated result.
        Live: requires confirm=True.
        """
        meta = self.asset_meta(coin)
        sz = round_size_down(sz, meta["szDecimals"])
        price = round_price_for_asset(price, meta["szDecimals"])
        if sz <= 0:
            raise HyperliquidError(f"size rounds to zero for {coin}")
        side = "BUY" if is_buy else "SELL"
        live = self._check_live(dry_run, confirm, f"limit {side} {sz} {coin} @ {price}")
        if not live:
            logger.info("[DRY RUN] limit %s %s %s @ %s (%s)",
                        side, sz, coin, price, self.network)
            return {
                "dry_run": True, "simulated": True, "network": self.network,
                "action": "limit_order", "coin": coin, "side": side,
                "size": sz, "price": price, "tif": tif,
            }
        order_type: OrderType = {"limit": {"tif": tif}}
        result = self._call(
            lambda: self._exchange.order(coin, is_buy, sz, price, order_type),
            f"limit {side} {sz} {coin} @ {price}",
        )
        return self._order_result(result, coin, side, sz, "limit", price)

    def place_bracket(self, coin, is_buy, sz, entry_px=None,
                      take_profit_px=None, stop_loss_px=None,
                      dry_run=True, confirm=False,
                      slippage=DEFAULT_SLIPPAGE):
        """Bracket: entry + take-profit trigger + stop-loss trigger.

        - Entry: market order when entry_px is None, else a limit
          order at entry_px.
        - TP: trigger order, isMarket, tpsl="tp". SL: trigger order,
          isMarket, tpsl="sl". Both on the opposite side of the entry,
          reduce-only, same size.
        - All prices rounded to 5 sig figs for the asset (§14).

        Half-leg rule (§14): if the entry fills and a bracket leg
        fails, the entry is closed at once with a reduce-only market
        close. If that close fails, EmergencyUnwindRequired is raised
        and the caller must STOP the cycle.

        Dry run: returns the would-be bracket (no venue calls).
        Live: requires confirm=True.
        """
        meta = self.asset_meta(coin)
        sz = round_size_down(sz, meta["szDecimals"])
        if sz <= 0:
            raise HyperliquidError(f"size rounds to zero for {coin}")
        if take_profit_px is None and stop_loss_px is None:
            raise HyperliquidError("place_bracket needs a TP and/or SL price")
        tp = (round_price_for_asset(take_profit_px, meta["szDecimals"])
              if take_profit_px is not None else None)
        sl = (round_price_for_asset(stop_loss_px, meta["szDecimals"])
              if stop_loss_px is not None else None)
        entry = (round_price_for_asset(entry_px, meta["szDecimals"])
                 if entry_px is not None else None)
        side = "BUY" if is_buy else "SELL"
        live = self._check_live(dry_run, confirm, f"bracket {side} {sz} {coin}")
        if not live:
            logger.info("[DRY RUN] bracket %s %s %s entry=%s tp=%s sl=%s (%s)",
                        side, sz, coin, entry, tp, sl, self.network)
            return {
                "dry_run": True, "simulated": True, "network": self.network,
                "action": "bracket", "coin": coin, "side": side, "size": sz,
                "entry_px": entry, "take_profit_px": tp, "stop_loss_px": sl,
            }

        # 1. Entry (market, or limit when a price was given).
        if entry is None:
            entry_result = self._call(
                lambda: self._exchange.market_open(coin, is_buy, sz,
                                                   slippage=slippage),
                f"bracket entry market {side} {sz} {coin}",
            )
        else:
            order_type: OrderType = {"limit": {"tif": "Gtc"}}
            entry_result = self._call(
                lambda: self._exchange.order(coin, is_buy, sz, entry, order_type),
                f"bracket entry limit {side} {sz} {coin} @ {entry}",
            )
        entry_out = self._order_result(entry_result, coin, side, sz,
                                       "market" if entry is None else "limit",
                                       entry)
        if entry_out.get("status") == "error":
            raise HyperliquidError(
                f"bracket entry rejected by venue: {entry_out.get('error')}"
            )
        if entry_out.get("status") != "filled":
            # Entry did not fill (a limit entry now resting): there is
            # no position for the triggers, and nothing to roll back.
            # Return the entry as-is; the caller can bracket it if and
            # when it fills.
            return {"entry": entry_out, "take_profit": None, "stop_loss": None,
                    "rolled_back": False, "network": self.network}

        # 2. Bracket legs, on the opposite side, reduce-only.
        tp_out = sl_out = None
        try:
            if tp is not None:
                tp_type: OrderType = {"trigger": {"triggerPx": tp,
                                                  "isMarket": True,
                                                  "tpsl": "tp"}}
                tp_result = self._call(
                    lambda: self._exchange.order(coin, not is_buy, sz, tp,
                                                 tp_type, reduce_only=True),
                    f"bracket TP {coin} @ {tp}",
                )
                tp_out = self._order_result(tp_result, coin,
                                            "SELL" if is_buy else "BUY",
                                            sz, "trigger_tp", tp)
            if sl is not None:
                sl_type: OrderType = {"trigger": {"triggerPx": sl,
                                                  "isMarket": True,
                                                  "tpsl": "sl"}}
                sl_result = self._call(
                    lambda: self._exchange.order(coin, not is_buy, sz, sl,
                                                 sl_type, reduce_only=True),
                    f"bracket SL {coin} @ {sl}",
                )
                sl_out = self._order_result(sl_result, coin,
                                            "SELL" if is_buy else "BUY",
                                            sz, "trigger_sl", sl)
        except Exception:
            # A bracket leg failed after the entry filled: roll back
            # at once with a reduce-only market close (§14 half-leg rule).
            logger.error("bracket leg failed for %s; rolling back entry", coin)
            try:
                self._call(
                    lambda: self._exchange.market_close(coin, sz,
                                                        slippage=slippage),
                    f"rollback close {coin}",
                )
                rolled_back = True
            except Exception as rb:
                raise EmergencyUnwindRequired(
                    f"HALF-LEG: {coin} entry filled but a bracket leg "
                    f"failed, and the rollback close ALSO failed ({rb}). "
                    "The position is live and one-legged. STOP the cycle "
                    "and unwind manually."
                ) from rb
            return {"entry": entry_out, "take_profit": tp_out,
                    "stop_loss": sl_out, "rolled_back": rolled_back,
                    "network": self.network}

        return {"entry": entry_out, "take_profit": tp_out,
                "stop_loss": sl_out, "rolled_back": False,
                "network": self.network}

    def cancel_order(self, coin, oid, dry_run=True, confirm=False):
        """Cancel one open order by its oid.

        Dry run: logs the would-be cancel, returns a simulated result.
        Live: requires confirm=True.
        """
        live = self._check_live(dry_run, confirm, f"cancel {coin} oid={oid}")
        if not live:
            logger.info("[DRY RUN] cancel %s oid=%s (%s)", coin, oid, self.network)
            return {
                "dry_run": True, "simulated": True, "network": self.network,
                "action": "cancel_order", "coin": coin, "oid": oid,
            }
        result = self._call(
            lambda: self._exchange.cancel(coin, int(oid)),
            f"cancel {coin} oid={oid}",
        )
        return {"network": self.network, "action": "cancel_order",
                "coin": coin, "oid": oid, "status": "ok", "venue": result}

    def rollback_close(self, coin, sz=None, dry_run=True, confirm=False,
                       slippage=DEFAULT_SLIPPAGE):
        """Emergency unwind: reduce-only market close of a position.

        This is the §14 half-leg rollback. Same gates as every write:
        dry_run=True by default, live needs confirm=True.
        """
        live = self._check_live(dry_run, confirm, f"rollback close {coin}")
        if not live:
            logger.info("[DRY RUN] rollback close %s sz=%s (%s)",
                        coin, sz, self.network)
            return {
                "dry_run": True, "simulated": True, "network": self.network,
                "action": "rollback_close", "coin": coin, "size": sz,
            }
        result = self._call(
            lambda: self._exchange.market_close(coin, sz, slippage=slippage),
            f"rollback close {coin}",
        )
        return {"network": self.network, "action": "rollback_close",
                "coin": coin, "size": sz, "status": "ok", "venue": result}

    # -- helpers ---------------------------------------------------

    def _order_result(self, result, coin, side, sz, kind, px=None):
        """Normalise the SDK's order response into one shape.

        Hyperliquid returns {"status": "ok", "response": {"type":
        "order", "data": {"statuses": [...]}}} with per-order entries
        like {"filled": {"totalSz": ..., "avgPx": ..., "oid": ...}} or
        {"resting": {"oid": ...}} or {"error": ...}. A top-level
        {"status": "err"} never reaches here: _call raises it first.
        """
        out = {"network": self.network, "action": kind, "coin": coin,
               "side": side, "size": sz, "price": px, "status": "unknown",
               "oid": None, "filled_px": None, "venue": result}
        try:
            data = result.get("response", {}).get("data", {})
            statuses = data.get("statuses", [])
            if not statuses:
                return out
            first = statuses[0]
            if "filled" in first:
                f = first["filled"]
                out["status"] = "filled"
                out["oid"] = int(f["oid"])
                out["filled_px"] = float(f["avgPx"])
            elif "resting" in first:
                out["status"] = "resting"
                out["oid"] = int(first["resting"]["oid"])
            elif "error" in first:
                out["status"] = "error"
                out["error"] = first["error"]
        except Exception:
            pass
        return out


def connect(testnet=True):
    """Connect to Hyperliquid and return a HyperliquidClient.

    testnet=True (default) reads HL_TESTNET_PRIVATE_KEY.
    testnet=False reads HL_PRIVATE_KEY (mainnet) — only use with a
    funded mainnet key and explicit user approval per action.

    The key is read from the environment only. It is never written to
    any file, never printed, never logged. The account address is
    derived from the key locally.
    """
    env_var = "HL_TESTNET_PRIVATE_KEY" if testnet else "HL_PRIVATE_KEY"
    key = os.environ.get(env_var)
    if not key:
        raise RefusedError(
            f"{env_var} is not set. Set it in the environment; never "
            "paste a key into chat and never store one in a file."
        )
    key = key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    from eth_account import Account
    account = Account.from_key(key)
    address = account.address
    exchange = Exchange(account, base_url, account_address=address)
    client = HyperliquidClient(testnet, address, info, exchange)
    logger.info("connected to Hyperliquid %s as %s",
                client.network, address)
    return client


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys
    testnet = "--mainnet" not in sys.argv
    client = connect(testnet=testnet)
    print("network :", client.network)
    print("address :", client.address)
    print("balance :", client.get_balance())
    print("positions:", client.get_positions())
    print("HYPE mark :", client.fetch_mark_price("HYPE"))
    print("HYPE funding:", client.fetch_funding_rate("HYPE"))
