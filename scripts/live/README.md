# Live execution adapters (optional, v0.7.1)

Two venue adapters that wire SKILL.md §14 into `scripts/execute.py
--mode live`. The core skill stays stdlib-only and paper-first; these
files are the only place that talks to a venue with a key, and they
only run when live mode is asked for explicitly.

- `hyperliquid_client.py`: Hyperliquid, via the official
  `hyperliquid-python-sdk` (0.24.0). Testnet verified: public reads,
  dry runs, refusal gates, price rounding, and a real signed order
  submission that the venue received and rejected (unfunded throwaway
  address). A filled order, a resting bracket, a real cancel, and the
  live rollback were NOT observed. The testnet faucet refuses fresh
  addresses (it wants an address that already exists on mainnet).
- `lighter_client.py`: Lighter, via the official `lighter-sdk`
  (1.1.8, `elliottech/lighter-python`). Testnet verified on a funded
  account: a market order that filled, a bracket whose entry filled
  with the stop-loss and take-profit legs resting on the venue, a
  real cancel that cleared both legs, and a reduce-only close that
  left the account flat. The two-leg live rollback is still NOT
  observed; it needs both venues funded at once. Two fixes after the
  build: the account index is enforced at the live write, not at
  connect, so dry runs and public reads work without it; and IOC
  limit orders now pass order_expiry 0, which the signer requires
  (the SDK default was rejected as invalid). Set
  `LIGHTER_TESTNET_API_KEY_INDEX` to the number the app assigned
  your key (default 3). Lighter enforces a minimum notional of 10
  dollars per order.

## Safety gates (both adapters, no exceptions)

- Every write defaults to `dry_run=True` and touches nothing.
- A live write needs `dry_run=False` AND `confirm=True`. One alone
  is refused before any network traffic.
- Keys come from the environment only (`HL_TESTNET_PRIVATE_KEY` /
  `HL_PRIVATE_KEY`, `LIGHTER_TESTNET_PRIVATE_KEY` /
  `LIGHTER_PRIVATE_KEY` plus the Lighter account index). Never in a
  file, never in chat, never logged.
- 401 means stale credentials: stop the cycle, do not retry. 5xx
  means do NOT auto-retry (the original may have committed).
- If Leg 2 fails after Leg 1 fills, Leg 1 closes at once with a
  reduce-only market order. If that close fails, the cycle stops and
  says so loudly. Never leave a one-legged trade.

## How live mode runs

`execute.py --mode live` defaults to testnet. It needs
`--live-confirm` plus a yes per ticket, and it refuses
`--auto-approve`. Mainnet needs `--mainnet` on top of that.
`--dry-run-live` rehearses the whole live path (connect + simulate
both legs) without sending anything. Only Hyperliquid + Lighter legs
run live; any other venue is refused, never half-run.

Venue quirks the adapters already handle: Hyperliquid bracket prices
are rounded to 5 sig figs; Lighter bracket legs reconcile by
(symbol, side, size) because Lighter ignores `clientOrderId` on
brackets; market indices resolve live per environment (testnet ETH is
4095 on Lighter, mainnet ETH is 0).

See `LIVE.md` at the repo root for the full setup guide.
