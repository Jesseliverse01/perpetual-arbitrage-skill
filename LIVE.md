# Live version (v0.9.0)

This is the live version of the skill: the same paper-first research
tool, plus a real execution path for the venues it backtests. All
five venues have working adapters now: Hyperliquid, Lighter, dYdX,
Aster and Derive. Any venue can still be added with the template in
`scripts/live/custom_venue.py`. Nothing here trades on its own.
Every live order needs a human yes for that exact ticket, and the
default is testnet, not mainnet.

## What live can and cannot do

Live mode places the two market legs of a Strategy E ticket (long one
venue, short the other), records the position in the state file, and
rolls the first leg back at once if the second leg fails. It does not
place per-leg stop triggers yet; the hard stop, the safe floor, and
the max hold live in the ticket and in the state file, and the cycle
scores closes on the next run. Only legs on a venue with a working
adapter run live. A ticket with any other venue is refused, never
half-run.

What was verified, honestly: on a funded Lighter testnet account,
the adapter placed a market order that filled, a bracket whose entry
filled with the stop-loss and take-profit legs resting on the venue,
a real cancel that cleared both legs, and a reduce-only close that
left the account flat. On Hyperliquid testnet, the adapter did public
reads, dry runs, refusal gates, and a real signed order submission
that the venue received and rejected (the throwaway address was
unfunded). On dYdX testnet, the adapter did public reads and dry
runs, and a live signed market order was received and rejected
on-chain (unfunded subaccount, equity tier limit 0), which proves
the full write path up to the chain. On Aster testnet, the adapter
did public reads and dry runs, and a signed request reached the
venue and got a parsed auth error, which proves the signature path.
On Derive testnet, the adapter did public reads and dry runs. What
was NOT verified: a filled order on Hyperliquid, dYdX, Aster or
Derive; a resting bracket, a real cancel, or a reduce-only close on
those four; and the two-leg live rollback, which needs both venues
funded at once. Mainnet is wired for all five venues but NOT
verified: no mainnet order was placed, no mainnet key was used.
Run the funded testnet steps yourself before you trust live mode
with real money.

## Custom venues

Live mode is not tied to Hyperliquid and Lighter. Pick the venue you
want to test on, add its adapter, and run the same testnet-first
cycle there. The execution engine connects only the venues in the
ticket it is running, through the registry in
`scripts/live/registry.py`.

The adapter interface is one class, `VenueAdapter` in
`scripts/live/venue_base.py`: connect, balance, positions, open
orders, funding rate (as APR in percent), mark price, market order,
limit order, bracket, cancel, cancel-all, and a reduce-only close
for the half-leg rollback. Every write defaults to dry run, and a
live write needs dry run off AND confirm on. Credentials come from
environment variables only, with the venue name as the prefix:
`HL_TESTNET_PRIVATE_KEY` for Hyperliquid testnet,
`LIGHTER_TESTNET_PRIVATE_KEY` for Lighter testnet, and the same
shape for any venue you add.

To add a venue, copy `scripts/live/custom_venue.py` to
`scripts/live/<venue>_client.py`, fill in each method (every one
carries a comment saying what it needs), register the module name
and env prefix in `scripts/live/registry.py`, and add its SDK to
`requirements-live.txt` when it has one. Then test on testnet with
throwaway keys and a tiny size: connect, read the mark price and
the funding rate, dry run, one small live order, a bracket, a
cancel, a reduce-only close.

The five adapters that ship with the skill, and what is verified
about each:

- Hyperliquid (`scripts/live/hyperliquid_client.py`): official
  `hyperliquid-python-sdk` (0.24.0). Testnet verified: public
  reads, dry runs, refusal gates, and a real signed order
  submission the venue received and rejected (unfunded throwaway
  address). A filled order, a resting bracket, a real cancel, and
  the live rollback were NOT observed.
- Lighter (`scripts/live/lighter_client.py`): official
  `lighter-sdk` (1.1.8). Testnet verified on a funded account: a
  market order that filled, a bracket whose entry filled with the
  stop-loss and take-profit legs resting on the venue, a real
  cancel that cleared both legs, and a reduce-only close that left
  the account flat. The two-leg live rollback is still NOT
  observed.
- dYdX (`scripts/live/dydx_client.py`): official `dydx-v4-client`
  (1.1.6). Testnet app https://v4.testnet.dydx.exchange, indexer
  at /v4/perpetualMarkets (226 markets). Funding is hourly, so APR
  is rate times 24 times 365 times 100. Public reads and dry runs
  verified live; a live signed market order was received and
  rejected on-chain (unfunded subaccount). A filled order, a
  resting bracket, a real cancel, and a reduce-only close were NOT
  observed. The faucet API returns 202 but its USDC leg did not
  land for fresh addresses; fund a testnet subaccount in the
  browser first (deposit/recharge, 300 test USDC). Auth is an
  Ethereum wallet signature; testnet chain id dydx-testnet-4; gas
  token DV4TNT from the same faucet.
- Aster (`scripts/live/aster_client.py`): raw HTTP plus
  `eth-account` (no maintained official V3 Python SDK exists).
  Auth is EIP-712 agent signing, not an API key and secret: set
  ASTER_TESTNET_SIGNER (the API wallet address) and
  ASTER_TESTNET_SIGNER_PRIVATE_KEY (its private key). Testnet app
  https://www.asterdex-testnet.com. Public reads verified live
  (21 symbols, mark price, funding rates; funding interval read
  live per symbol, 8h for BTC). Dry runs simulate, and a signed
  request reached the venue and got a parsed auth error, which
  proves the signature path. A filled order, a resting bracket, a
  real cancel, and a reduce-only close were NOT observed. A funded
  testnet account plus a registered API wallet can only be created
  in the browser. Watch for venue code -5050 (deposit gate, in
  force since 1 Sep 2026): if testnet returns it on the first
  signed call, signed calls are blocked until the venue lifts it.
  Chain id 714 testnet, 1666 mainnet.
- Derive (`scripts/live/derive_client.py`): implemented on the
  official `derive-py` SDK (0.1.5, MIT), v3 API
  https://testnet.api.derive.xyz/v3. Public reads verified live on
  testnet (20 perps, mark price, per-hour funding as APR); writes
  are code-complete with the usual dry-run and confirm gates but NOT
  yet verified on a funded account. That needs Jesse to fund a
  testnet account in the app and mint a session key first (exact
  steps in `~/workspace/live-build/VERIFY-derive.md`). Funding is
  quoted per hour, so APR = rate * 24 * 365 * 100. The old v2 demo
  host api-demo.lyra.finance is not used.

## Install the skill in your agent

Clone the repo, then copy it into your agent's skills directory (the
folder your agent loads skills from; check your agent's docs for the
path). Any agent that reads `SKILL.md` can load it; the file tells
the agent what to do, and `SOUL.md` sets the mindset. The core scripts
need nothing beyond Python 3.10+ and the standard library.

Live mode needs two extra packages, installed in a venv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-live.txt
```

That installs `hyperliquid-python-sdk` 0.24.0, `lighter-sdk`
1.1.8, `dydx-v4-client` 1.1.6, and `derive-py` 0.1.5, the official
SDKs, plus `requests` and `eth-account` for the Aster adapter.
Run the scripts with the venv Python
(`.venv/bin/python3 scripts/execute.py ...`).

## Keys (environment only)

Live mode reads keys from environment variables and nowhere else. Do
not paste a key into chat, do not put one in a file, do not commit
one. The adapters never log a key.

```bash
export HL_TESTNET_PRIVATE_KEY=0x...
export LIGHTER_TESTNET_PRIVATE_KEY=0x...
export LIGHTER_TESTNET_ACCOUNT_INDEX=1
export LIGHTER_TESTNET_API_KEY_INDEX=3
export DYDX_TESTNET_PRIVATE_KEY=0x...
export ASTER_TESTNET_SIGNER=0x...
export ASTER_TESTNET_SIGNER_PRIVATE_KEY=0x...
export DERIVE_TESTNET_PRIVATE_KEY=0x...
export DERIVE_TESTNET_WALLET=0x...
export DERIVE_TESTNET_SUBACCOUNT_ID=1234
```

The Lighter API key index is the number the app assigned your key;
the default is 3, set it if yours differs (the app shows it on the
API Keys page). Lighter also enforces a minimum notional of 10
dollars per order, so a tiny test size can be refused or silently
not fill; the Strategy E default of 50 dollars per leg is safely
above it.

For mainnet the names drop the `TESTNET_` part
(`HL_PRIVATE_KEY`, `LIGHTER_PRIVATE_KEY`,
`LIGHTER_ACCOUNT_INDEX`, `DYDX_PRIVATE_KEY`, `ASTER_SIGNER`,
`ASTER_SIGNER_PRIVATE_KEY`, `DERIVE_PRIVATE_KEY`,
`DERIVE_WALLET`, `DERIVE_SUBACCOUNT_ID`). dYdX also accepts a
mnemonic instead of a private key
(`DYDX_TESTNET_MNEMONIC` / `DYDX_MNEMONIC`); a private key wins
when both are set. Aster has no API key or secret: the signer is
an API wallet you register in the app, and its address goes in
ASTER_TESTNET_SIGNER while its private key goes in
ASTER_TESTNET_SIGNER_PRIVATE_KEY.

How to get them: on Hyperliquid, create an API wallet in the app
(Settings, API Wallet) and use its private key; the API wallet can
only trade, it cannot withdraw. On Lighter, register an API key with
the adapter's `register_api_key()` or the official `system_setup.py`
from the Lighter SDK examples; it needs a funded account first. On
dYdX, use a wallet private key (or its mnemonic) for the account
that holds the testnet subaccount; the key signs orders, it cannot
withdraw. On Aster, connect a wallet in the testnet app, create an
API wallet on the API wallet page (trade permission, no withdraw),
and use its address and private key as the signer. On Derive, fund
a testnet account in the app first (Deposit, then Mint test USDC),
then mint a session key in the app (Settings, Session Keys) with the
trade scope on your subaccount and use its private key; the session
key can only trade on that subaccount, and it expires. The wallet
address and subaccount id go in DERIVE_TESTNET_WALLET and
DERIVE_TESTNET_SUBACCOUNT_ID.

## Testnet first

Fund a testnet account before anything else. On Hyperliquid the
faucet only serves addresses that already exist on mainnet, so a
brand new address gets refused; use an address that has deposited on
mainnet before, or fund it from mainnet first. On Lighter, connect a
wallet in the testnet app at testnet.app.lighter.xyz and it credits
test USDC; there is no faucet API. On dYdX, connect a wallet in the
testnet app at https://v4.testnet.dydx.exchange and press
deposit/recharge; it credits 300 test USDC automatically. The
faucet API (POST https://faucet.v4testnet.dydx.exchange/faucet/tokens)
returns 202 but its USDC leg did not land for fresh addresses, so
use the app button. On Aster, connect a wallet at
https://www.asterdex-testnet.com and claim the faucet, then create
an API wallet on the API wallet page. On Derive, get Sepolia ETH for
gas (any Sepolia faucet), open https://testnet.app.derive.xyz,
connect a wallet, click Deposit then Mint to mint test USDC, and
deposit. The first deposit creates the account and its first
subaccount, and the subaccount id shows up about two minutes later.
There is no faucet API for the deposit step; it needs the browser.

Then rehearse the whole live path without sending anything:

```bash
.venv/bin/python3 scripts/multivenue_scan.py
.venv/bin/python3 scripts/execute.py --mode live --dry-run-live --live-confirm
```

That connects, reads marks and balances, and simulates both legs for
each ticket. When the rehearsal looks right, run one small live
testnet trade (default notional is 100 dollars total, 50 per leg;
start smaller with `--notional 20`):

```bash
.venv/bin/python3 scripts/execute.py --mode live --live-confirm --notional 20
```

Say yes to one ticket only, watch both legs fill, then check the
position in `results/live-watch-state.json` and on the venue.

## Mainnet

Only after testnet. Mainnet needs the `--mainnet` flag on top of
`--live-confirm`, plus a yes per ticket, and it refuses
`--auto-approve` outright. Start at the smallest size the venues
allow, keep the default 100 dollar notional until you have seen a
full cycle (enter, hold, close) work, and never run live mode
unattended. The state file is the record of what is open; if it is
missing or corrupt, live mode stops rather than trading from memory.

## Risks, plainly

Funding spreads are thin and move fast. The backtests are paper, the
live path has seen filled orders on Lighter testnet only, and a venue
can reject, delay, or partially fill a leg. The rollback guard covers
the half-leg case, but it cannot cover a venue outage or a market
that moves past the stop while both legs are open. Never trade money
you cannot afford to lose, and never paste your main key anywhere;
use an API wallet or API key with no withdrawal rights.
