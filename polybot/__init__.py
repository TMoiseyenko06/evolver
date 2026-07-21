"""polybot — Polymarket / crypto market plumbing.

This is the shared market-data layer that ``evolver`` builds on top of.
It provides four concerns, each in its own module, mirroring the module
layout the task asked us to reuse:

- ``polybot.fees``      — the Polymarket taker-fee curve and breakeven math.
- ``polybot.candles``   — Coinbase Exchange candle/spot access (Binance blocks US IPs).
- ``polybot.polymarket``— Gamma + CLOB market discovery, token mapping, resolution.
- ``polybot.db``        — small SQLite connection/schema helpers.

NOTE ON PROVENANCE: this repository did not contain a pre-existing ``polybot``
package to import, so these modules were written fresh to the *exact behaviors*
the task specified (see evolver/README.md → "Where is polybot?"). Everything
that touches the network is isolated here so that ``evolver`` can be driven with
a mock market provider in tests without any HTTP.
"""

from . import candles, db, fees, polymarket  # noqa: F401

__all__ = ["candles", "db", "fees", "polymarket"]
