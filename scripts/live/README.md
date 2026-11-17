# Live execution adapters (optional, v0.8.0)

Venue adapters that wire SKILL.md §14 into `scripts/execute.py
--mode live`. The core skill stays stdlib-only and paper-first; these
files are the only place that talks to a venue with a key, and they
only run when live mode is asked for explicitly.

- `venue_base.py`: the `VenueAdapter` interface every adapter
  implements, plus the shared defaults (dry-run default, cancel-all
  by looping open orders, rollback by finding the position and
  reduce-only closing it).
- `registry.py`: venue name to adapter module and credential env
  prefix. `get_adapter(venue, testnet)` connects only the venue a
  ticket needs, cached per venue and network.
- `custom_venue.py`: the template to copy when adding a venue.
  Every method raises `NotImplementedError` with a comment saying
  what to fill in.
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
- `dydx_client.py`, `aster_client.py`, `derive_client.py`: stubs.
  Importable, but every method raises `NotImplementedError` and
  `connect()` fails loudly before any order is placed. Each header
  names only what is verified about its venue. Fill them in from
  the template, then register them (they are already listed in the
  registry as stubs).

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
both legs) without sending anything. Only legs on a venue with a
working adapter run live; a ticket on a stub or unregistered venue
is refused with a clear message, never half-run. To add a venue,
copy `custom_venue.py`, fill it in, and register it in
`registry.py`; see SKILL.md §14 for the full steps.

Venue quirks the adapters already handle: Hyperliquid bracket prices
are rounded to 5 sig figs; Lighter bracket legs reconcile by
(symbol, side, size) because Lighter ignores `clientOrderId` on
brackets; market indices resolve live per environment (testnet ETH is
4095 on Lighter, mainnet ETH is 0).

See `LIVE.md` at the repo root for the full setup guide.
