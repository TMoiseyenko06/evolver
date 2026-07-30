"""Entry gate + the three early-exit triggers.

``entry_signal`` is a direct port of ``stale_quote_sweeper``'s decide(): compare the
model's fair value to each side's actual ask, and only enter when the model
disagrees by more than a fee-adjusted margin.

The three exit triggers all reuse ``fair_value_probs`` — recomputed fresh each poll
after entry, never a new/different signal:

  1. ``gap_closed``   -- the ask has caught up to (or past) fair value: take profit.
  2. ``adverse_move`` -- the model itself now prices the held side BELOW what was
                         paid: a self-consistent stop-loss, no new signal needed.
  3. ``time_expired``  -- neither has happened within ``time_expired_seconds`` of
                         entry: the "stale quote" thesis was probably wrong.

Checked in that priority order (take the win first, then real bad news, then a
stale thesis) — if more than one fires on the same poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from polybot import fees

from evolver.models import Book, Fill

from . import model as fv
from .config import MispricingParams


@dataclass(frozen=True)
class EntrySignal:
    side: str
    ask: float
    model_p: float  # the model's fair-value probability for `side` at entry


def entry_signal(
    candles: List[dict],
    window_open_price: float,
    spot: float,
    seconds_remaining: float,
    books: Dict[str, Book],
    params: MispricingParams,
) -> Optional[EntrySignal]:
    """Direct port of stale_quote_sweeper's decide(): for each side, if the model's
    fair value clears the ask by more than ``entry_gap`` AND still clears the
    fee-adjusted breakeven by more than ``fee_margin``, enter that side.
    """
    if len(candles) < params.lookback_candles:
        return None
    probs = fv.fair_value_probs(candles, window_open_price, spot, seconds_remaining, params)
    for side, p in probs.items():
        asks = books.get(side, {}).get("asks", [])
        if not asks:
            continue
        ask = asks[0][0]
        if ask <= 0:
            continue
        if p - ask > params.entry_gap and p - fees.breakeven(ask) > params.fee_margin:
            return EntrySignal(side=side, ask=ask, model_p=p)
    return None


@dataclass
class OpenPosition:
    side: str
    fill: Fill
    entry_model_p: float
    entry_poll_index: int
    entry_seconds_remaining: int  # basis for time_expired — measured in market time,
    # not wall-clock, so backtest (instant replay) and live behave identically.


@dataclass(frozen=True)
class ExitDecision:
    reason: str  # "gap_closed" | "adverse_move" | "time_expired"
    model_p_now: float
    ask_now: Optional[float]


def check_exit(
    position: OpenPosition,
    candles: List[dict],
    window_open_price: float,
    spot: float,
    seconds_remaining: float,
    books: Dict[str, Book],
    params: MispricingParams,
) -> Optional[ExitDecision]:
    """Recompute the SAME fair-value model for this poll and check the three exit
    triggers in priority order. Returns the first that fires, or None (keep holding).
    """
    if len(candles) < params.lookback_candles:
        return None
    probs = fv.fair_value_probs(candles, window_open_price, spot, seconds_remaining, params)
    model_p_now = probs[position.side]
    asks = books.get(position.side, {}).get("asks", [])
    ask_now = asks[0][0] if asks else None

    if ask_now is not None and model_p_now - ask_now <= params.gap_closed_min_gain:
        return ExitDecision("gap_closed", model_p_now, ask_now)
    if model_p_now < position.fill.avg_price:
        return ExitDecision("adverse_move", model_p_now, ask_now)
    elapsed = position.entry_seconds_remaining - seconds_remaining
    if elapsed >= params.time_expired_seconds:
        return ExitDecision("time_expired", model_p_now, ask_now)
    return None
