#!/usr/bin/env python3
"""Venue registry for the perpetual-arbitrage skill.

Maps a venue name to its adapter module and its credential env
prefix, and loads adapters on demand. The execution engine never
imports a venue module directly; it asks here, and this module
connects only the venues a ticket actually needs.

Env prefix convention: testnet credentials live in
<PREFIX>TESTNET_<KEY>, mainnet credentials in <PREFIX><KEY>. For
example Hyperliquid uses HL_TESTNET_PRIVATE_KEY on testnet and
HL_PRIVATE_KEY on mainnet. A new venue follows the same shape.

Status means: "live" is a working adapter, "stub" raises
NotImplementedError at connect (the venue is named but nobody has
filled in its adapter yet), "template" is the file to copy when
adding a venue.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REGISTRY = {
    "hyperliquid": {
        "module": "hyperliquid_client",
        "env_prefix": "HL_",
        "status": "live",
    },
    "lighter": {
        "module": "lighter_client",
        "env_prefix": "LIGHTER_",
        "status": "live",
    },
    "dydx": {
        "module": "dydx_client",
        "env_prefix": "DYDX_",
        "status": "live",
    },
    "aster": {
        "module": "aster_client",
        "env_prefix": "ASTER_",
        "status": "live",
    },
    "derive": {
        "module": "derive_client",
        "env_prefix": "DERIVE_",
        "status": "live",
    },
    "custom": {
        "module": "custom_venue",
        "env_prefix": "CUSTOM_",
        "status": "template",
    },
}

_CLIENTS = {}


class UnknownVenueError(Exception):
    """No adapter module is registered for this venue name."""


def known_venues():
    """Registered venue names, in registry order."""
    return list(REGISTRY)


def venue_status(venue):
    """Status string for a venue: live, stub, or template. None when
    the venue is not registered at all."""
    entry = REGISTRY.get(str(venue).lower())
    return entry["status"] if entry else None


def get_adapter(venue, testnet=True):
    """Connect a venue adapter, cached per (venue, network).

    Only the requested venue is connected, and only once per run.
    A venue with no registered module raises UnknownVenueError. A
    stub or template venue raises NotImplementedError from its
    connect(), before any order is placed. Any other connect failure
    (missing env key, unreachable host) raises as-is, so the caller
    can stop the cycle with the real reason.
    """
    name = str(venue).lower()
    key = (name, bool(testnet))
    if key in _CLIENTS:
        return _CLIENTS[key]
    entry = REGISTRY.get(name)
    if entry is None:
        raise UnknownVenueError(
            f"no adapter registered for venue {venue!r}. Registered "
            f"venues: {', '.join(known_venues())}. To add one, copy "
            "scripts/live/custom_venue.py, fill it in, and register it here."
        )
    module = importlib.import_module(entry["module"])
    client = module.connect(testnet=testnet)
    _CLIENTS[key] = client
    return client


def clear_cache():
    """Drop cached clients (tests and long-running runs)."""
    _CLIENTS.clear()
