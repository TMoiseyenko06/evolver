"""Per-venue taker-fee models, in $ per share/contract at a given price.

Fees decide whether a spread is a real arb. Both venues charge more near 0.50 and
~0 at the extremes, but with different coefficients:

- **Polymarket**: ``0.0312 * min(p, 1-p)`` per share (the evolver's fee curve).
- **Kalshi**: general schedule ``ceil(0.07 * C * p * (1-p))`` — ~``0.07 * p*(1-p)``
  per contract, rounded up to the cent per order. We use the smooth per-contract
  form; the coefficient is configurable because Kalshi's schedule varies by market.

These are defaults to make the scanner honest, not gospel — verify against real
fills (the same discipline as the evolver's fill model) and tune the coefficients.
"""

from __future__ import annotations

from typing import Callable, Dict

from polybot import fees as _pm

# per-share fee = coeff * min(p, 1-p)
POLYMARKET_COEFF = 0.0312
# per-contract fee ≈ coeff * p * (1-p)
KALSHI_COEFF = 0.07


def polymarket_fee_per_share(price: float) -> float:
    return POLYMARKET_COEFF * min(price, 1.0 - price)


def kalshi_fee_per_share(price: float) -> float:
    return KALSHI_COEFF * price * (1.0 - price)


FEE_MODELS: Dict[str, Callable[[float], float]] = {
    "polymarket": polymarket_fee_per_share,
    "kalshi": kalshi_fee_per_share,
}


def fee_per_share(venue: str, price: float) -> float:
    """Per-share/contract taker fee for ``venue`` at ``price`` (0 if venue unknown)."""
    fn = FEE_MODELS.get((venue or "").lower())
    return fn(price) if fn else 0.0


# Sanity: the Polymarket helper matches polybot.fees for a single share.
def _polymarket_matches_polybot(price: float) -> bool:  # pragma: no cover - doc check
    return abs(polymarket_fee_per_share(price) - _pm.fee(1.0, price)) < 1e-12
