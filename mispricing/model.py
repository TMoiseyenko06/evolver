"""Pure fair-value model, ported verbatim from the evolved strategy ``stale_quote_sweeper``.

The idea: estimate recent realized volatility, scale it to how much time is left
(square-root-of-time, the standard random-walk rule), and ask "given how far price
has already moved from the window's open, what's the fair probability we finish
above it, treating the rest of the path as a driftless random walk?" That's a
neutral baseline — it isn't a momentum or reversion opinion, just "what should the
market be charging right now."

Every function here takes plain primitives (candles/prices), never ``evolver.context.Ctx``
directly, so the exact same math runs identically whether driven by a live poll or by
an archived ``PollSnapshot`` read back from the evolver's own SQLite — there is only
ONE implementation of the model, used by both the live paper loop and the backtest.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List

from .config import MispricingParams


def one_min_returns(candles: List[dict], lookback_candles: int) -> List[float]:
    """Fractional close-to-close returns over the last ``lookback_candles`` candles.

    Yields up to ``lookback_candles - 1`` returns. A pair is skipped if the earlier
    close is <= 0 (guards a divide-by-zero on bad data, matching the ported original).
    """
    if len(candles) < lookback_candles:
        return []
    window = candles[-lookback_candles:]
    rets = []
    for a, b in zip(window[:-1], window[1:]):
        if a["close"] > 0:
            rets.append((b["close"] - a["close"]) / a["close"])
    return rets


def realized_sd(returns: List[float], fallback_sd: float) -> float:
    """Population stdev of ``returns``, or ``fallback_sd`` if too few points or the
    computed value is <= 0 (a flat/degenerate sample shouldn't collapse sigma to 0)."""
    sd = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    return sd if sd > 0 else fallback_sd


def sqrt_time_sigma(sd: float, seconds_remaining: float) -> float:
    """Scale per-minute volatility ``sd`` to the time left, via the square-root-of-time
    rule: uncertainty over ``t`` minutes grows with ``sqrt(t)``, not ``t``, under the
    standard assumption of independent per-minute returns."""
    t_frac = max(seconds_remaining, 1) / 60.0
    return sd * math.sqrt(t_frac)


def fair_value_probs(
    candles: List[dict],
    window_open_price: float,
    spot: float,
    seconds_remaining: float,
    params: MispricingParams,
) -> Dict[str, float]:
    """The model's fair probability of {"Up": p, "Down": 1-p}.

    ``p`` is the probability, under a driftless random walk with volatility
    ``sqrt_time_sigma``, that the path finishes above the window's open given how
    far it has already moved (``m = (spot - open) / open``). This is a neutral
    baseline — no momentum or reversion opinion, just "what should this be worth
    right now given the move already made and the volatility/time left."
    """
    rets = one_min_returns(candles, params.lookback_candles)
    sd = realized_sd(rets, params.fallback_sd)
    sigma = sqrt_time_sigma(sd, seconds_remaining)
    m = (spot - window_open_price) / max(window_open_price, 1.0)
    p_up = 0.5 * (1.0 + math.erf(m / (sigma * math.sqrt(2.0) + 1e-12)))
    p_up = min(1.0, max(0.0, p_up))
    return {"Up": p_up, "Down": 1.0 - p_up}
