# Live execution adapters (optional, v0.9.0)

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
- `derive_client.py`: Derive, via the official `derive-py`
  (0.1.5, `derivexyz/derive-py`, MIT). Testnet verified: public
  reads (20 perps, mark price, per-hour funding converted to APR),
  dry runs, and the refusal gates. A live order on a funded account
  (market fill, bracket legs resting, real cancel, reduce-only
  close) is NOT verified yet: it needs Jesse to fund a testnet
  account in the app and mint a session key first (see
  `~/workspace/live-build/VERIFY-derive.md` for the exact steps).
  Derive quotes funding per hour, so the APR conversion is rate *
  24 * 365 * 100, not the 8h factor the other venues use.
- `dydx_client.py`: dYdX, via the official `dydx-v4-client`
  (1.1.6). Testnet verified: public reads (226 perps, mark price,
  hourly funding as APR), dry runs, and the refusal gates. A live
  signed market order was received and rejected on-chain (unfunded
  subaccount, equity tier limit 0), which proves the full write
  path up to the chain. A filled order, a resting bracket, a real
  cancel, and a reduce-only close are NOT verified yet: they need a
  funded testnet subaccount first (deposit/recharge in the app,
  300 test USDC; the faucet API returns 202 but its USDC leg did
  not land). Funding is hourly, so the APR conversion is rate *
  24 * 365 * 100. Symbols use the venue form (`PEPE-USD`, not
  `1000PEPE-USD`).
- `aster_client.py`: Aster, raw HTTP plus `eth-account` (no
  maintained official V3 Python SDK exists). Auth is EIP-712 agent
  signing, not an API key and secret: set ASTER_TESTNET_SIGNER
  (the API wallet address) and ASTER_TESTNET_SIGNER_PRIVATE_KEY
  (its private key). Testnet verified: public reads (21 symbols,
  mark price, funding rates; funding interval read live per
  symbol, 8h for BTC), dry runs, and the refusal gates. A signed
  request reached the venue and got a parsed auth error ("No agent
  found"), which proves the signature path. A filled order, a
  resting bracket, a real cancel, and a reduce-only close are NOT
  verified yet: they need a funded testnet account plus a
  registered API wallet first, both created in the browser. Watch
  for venue code -5050 (deposit gate, in force since 1 Sep 2026).

## Safety gates (both adapters, no exceptions)

- Every write defaults to `dry_run=True` and touches nothing.
- A live write needs `dry_run=False` AND `confirm=True`. One alone
  is refused before any network traffic.
- Keys come from the environment only (`HL_TESTNET_PRIVATE_KEY` /
  `HL_PRIVATE_KEY`, `LIGHTER_TESTNET_PRIVATE_KEY` /
  `LIGHTER_PRIVATE_KEY` plus the Lighter account index,
  `DYDX_TESTNET_PRIVATE_KEY` / `DYDX_PRIVATE_KEY` (or a mnemonic),
  `ASTER_TESTNET_SIGNER` + `ASTER_TESTNET_SIGNER_PRIVATE_KEY` /
  `ASTER_SIGNER` + `ASTER_SIGNER_PRIVATE_KEY`,
  `DERIVE_TESTNET_PRIVATE_KEY` + `DERIVE_TESTNET_WALLET` +
  `DERIVE_TESTNET_SUBACCOUNT_ID` and the mainnet names without
  `TESTNET_`). Never in a file, never in chat, never logged.
- 401 means stale credentials: stop the cycle, do not retry. 5xx
  means do NOT auto-retry (the original may have committed).
- If Leg 2 fails after Leg 1 fills, Leg 1 closes at once with a
  reduce-only market order. If that close fails, the cycle stops and
  says so loudly. Never leave a one-legged trade. The close
  primitive accepts both side spellings: "buy"/"sell" is already
  the closing order side, and "long"/"short" names the leg being
  closed (a long closes with a sell, a short with a buy). Every
  adapter maps both to the same closing order.

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
