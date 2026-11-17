# Live version (v0.8.0)

This is the live version of the skill: the same paper-first research
tool, plus a real execution path for the venues it backtests.
Hyperliquid and Lighter are the two working adapters; dYdX, Aster
and Derive ship as stubs you can fill in, and any venue can be added
with the template in `scripts/live/custom_venue.py`. Nothing here
trades on its own. Every live order needs a human yes for that exact
ticket, and the default is testnet, not mainnet.

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
unfunded). What was NOT verified: a filled order on Hyperliquid, and
the two-leg live rollback, which needs both venues funded at once.
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

Three stubs ship with the skill, each naming only what is verified
about its venue and refusing to connect until filled in:

- dYdX (`scripts/live/dydx_client.py`): testnet app
  https://v4.testnet.dydx.exchange, faucet via the app (300 test
  USDC) and the faucet API https://faucet.v4testnet.dydx.exchange.
  Testnet indexer returned 404 from this VM, so the funding
  endpoint is not yet verified. All perps USDC-settled; auth is an
  Ethereum wallet signature; testnet chain id dydx-testnet-4; gas
  token DV4TNT from the same faucet.
- Aster (`scripts/live/aster_client.py`): the scanner already reads
  Aster funding via premiumIndex. Testnet futures REST
  https://fapi.asterdex-testnet.com verified HTTP 200 (ping,
  exchangeInfo, premiumIndex). The testnet app URL and the faucet
  are NOT verified. Chain id 714 comes from the official demo
  script via a third-party guide; re-check it before using it.
- Derive (`scripts/live/derive_client.py`): testnet app
  https://testnet.derive.xyz, USDC faucet in the dashboard, trading
  is gasless, deposit/withdraw needs gas (ETH, OP Stack L2). Demo
  API host api-demo.lyra.finance; individual API paths are not yet
  verified. Auth via EIP-712 signed messages / session keys.

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

That installs `hyperliquid-python-sdk` 0.24.0 and `lighter-sdk`
1.1.8, the official SDKs. Run the scripts with the venv Python
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
```

The Lighter API key index is the number the app assigned your key;
the default is 3, set it if yours differs (the app shows it on the
API Keys page). Lighter also enforces a minimum notional of 10
dollars per order, so a tiny test size can be refused or silently
not fill; the Strategy E default of 50 dollars per leg is safely
above it.

For mainnet the names drop the `TESTNET_` part
(`HL_PRIVATE_KEY`, `LIGHTER_PRIVATE_KEY`,
`LIGHTER_ACCOUNT_INDEX`).

How to get them: on Hyperliquid, create an API wallet in the app
(Settings, API Wallet) and use its private key; the API wallet can
only trade, it cannot withdraw. On Lighter, register an API key with
the adapter's `register_api_key()` or the official `system_setup.py`
from the Lighter SDK examples; it needs a funded account first.

## Testnet first

Fund a testnet account before anything else. On Hyperliquid the
faucet only serves addresses that already exist on mainnet, so a
brand new address gets refused; use an address that has deposited on
mainnet before, or fund it from mainnet first. On Lighter, connect a
wallet in the testnet app at testnet.app.lighter.xyz and it credits
test USDC; there is no faucet API.

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
live path has seen filled orders on Lighter testnet but not yet on
Hyperliquid, and a venue can reject, delay, or partially fill a leg.
The rollback guard covers the half-leg case, but it cannot cover a
venue outage or a market that moves past the stop while both legs are
open. Never trade money you cannot afford to lose, and never paste
your main key anywhere; use an API wallet or API key with no
withdrawal rights.
