# Live version (v0.7.0)

This is the live version of the skill: the same paper-first research
tool, plus a real execution path for the two venues it backtests
(Hyperliquid and Lighter). Nothing here trades on its own. Every live
order needs a human yes for that exact ticket, and the default is
testnet, not mainnet.

## What live can and cannot do

Live mode places the two market legs of a Strategy E ticket (long one
venue, short the other), records the position in the state file, and
rolls the first leg back at once if the second leg fails. It does not
place per-leg stop triggers yet; the hard stop, the safe floor, and
the max hold live in the ticket and in the state file, and the cycle
scores closes on the next run. Only Hyperliquid and Lighter legs run
live. A ticket with any other venue is refused, never half-run.

What was verified, honestly: on testnet, both adapters did public
reads, dry runs, refusal gates, and a real signed order submission
that the venue received and rejected (the throwaway addresses were
unfunded). What was NOT verified: a market order that actually
fills, a bracket resting on the venue, a real cancel, and the live
rollback. Those need a funded testnet account, and funding one is a
human step (below). Run the funded testnet steps yourself before you
trust live mode with real money.

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
```

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
500 test USDC; there is no faucet API.

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
live path has not yet seen a filled order, and a venue can reject,
delay, or partially fill a leg. The rollback guard covers the
half-leg case, but it cannot cover a venue outage or a market that
moves past the stop while both legs are open. Never trade money you
cannot afford to lose, and never paste your main key anywhere; use
an API wallet or API key with no withdrawal rights.
