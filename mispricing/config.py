"""Model/exit thresholds for the mispricing strategy.

Kept OUT of ``evolver.config.Config`` on purpose: ``Config`` is shared infra used by
the population loop, the tuner, and calibrate — every field there is either
accounting/execution infra (stake, bankroll, slippage/participation/price-guard,
synthesis creds) or population-management (survivors, diversity, risk ranking).
None of it is "this one strategy's numeric thresholds," and adding them there would
be exactly the kind of one-strategy-specific bloat this project has avoided
elsewhere. Infra knobs still come from the existing ``evolver.config.Config``,
passed in alongside a ``MispricingParams`` wherever both are needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MispricingParams:
    # --- fair-value model (ported verbatim from stale_quote_sweeper) ---
    lookback_candles: int = 5  # needs len(candles) >= this; yields (this - 1) returns
    fallback_sd: float = 0.0003  # used when <2 returns, or the computed sd is <= 0

    # --- entry gate (ported verbatim) ---
    entry_gap: float = 0.08  # model_p - ask must exceed this
    fee_margin: float = 0.05  # model_p - breakeven(ask) must exceed this

    # --- exit triggers (new) ---
    # Cut the trade if the mispricing hasn't closed within this many seconds of
    # entry. A reasoned default, not derived from the original strategy: roughly a
    # fifth of the 300s window — long enough for a genuine quote-lag to catch up
    # (normally seconds, not minutes), short enough to still protect a stalled/wrong
    # thesis. Tune once real backtest/paper data justifies a different value.
    time_expired_seconds: float = 60.0
    # Take profit as soon as the model no longer sees a gap (model_p_now - ask_now
    # <= this), i.e. the instant the mispricing has closed — don't hold out for a
    # bigger locked gain and risk the market moving back against the position.
    gap_closed_min_gain: float = 0.0
    # Master switch: False = pure hold-to-resolution (the evolved-strategy contract's
    # behavior), used as the baseline to isolate the effect of adding exits.
    exits_enabled: bool = True
