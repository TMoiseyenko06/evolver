"""evolver — an evolutionary strategy-search system for Polymarket 5-minute
Bitcoin Up/Down markets.

See README.md for the full design. The public surface most code touches:

    from evolver.config import Config
    from evolver.store import Store
    from evolver.runner import run_loop

The market plumbing (discovery, candles, fees, resolution) lives in the sibling
``polybot`` package and is imported, not reimplemented, here.
"""

__version__ = "0.1.0"
