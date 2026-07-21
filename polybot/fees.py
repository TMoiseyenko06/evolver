"""Polymarket taker-fee curve and breakeven helpers.

The fee for taking liquidity on a binary outcome token is::

    fee = 0.0312 * shares * min(price, 1 - price)

This is a *per-share* cost of ``0.0312 * min(price, 1 - price)`` dollars, so it
peaks at price 0.50 (~1.56c/share, i.e. ~3.1% of a 50c share) and decays to ~0
at the extremes (a 5c or 95c share pays almost nothing). That shape is the whole
reason "buy the obvious side" loses money: the obvious side trades near 55c and
pays close to the maximum fee while offering a thin edge.
"""

from __future__ import annotations

# Polymarket taker fee coefficient.
TAKER_FEE_RATE = 0.0312


def fee(shares: float, price: float) -> float:
    """Dollar taker fee for ``shares`` filled at ``price``."""
    return TAKER_FEE_RATE * shares * min(price, 1.0 - price)


def fee_per_share(price: float) -> float:
    """Per-share dollar fee at ``price`` (fee for one share)."""
    return TAKER_FEE_RATE * min(price, 1.0 - price)


def breakeven(ask: float) -> float:
    """Breakeven *probability* for buying at ``ask``.

    A share bought at ``ask`` costs ``ask + fee_per_share(ask)`` all-in and pays
    $1 if it wins, so the position is EV-positive only if the true win
    probability exceeds this value.
    """
    return ask + fee_per_share(ask)


def expected_value(p: float, ask: float) -> float:
    """EV per share = p - ask - fee, the core edge equation.

    ``p`` is the strategy's estimated win probability, ``ask`` the price paid.
    """
    return p - ask - fee_per_share(ask)
