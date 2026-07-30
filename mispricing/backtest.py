"""Backtest against ALREADY-COLLECTED evolver windows — read-only, zero risk.

The highest-value, zero-risk way to answer "would early exit have helped?": run the
SAME entry logic twice over real historical order-book data the user's evolver run
has already collected (via ``evolver.store.Store``) — once with exits enabled, once
with them disabled (the evolved-strategy baseline: always hold to resolution).
Because entries are entirely deterministic given the same archived polls, both
passes make identical entries, so any P&L difference isolates the effect of adding
exits, not different entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List

from polybot import fees as pb_fees

from evolver.config import Config
from evolver.engine import score_trade
from evolver.models import Stats, TradeResult, WindowData

from .config import MispricingParams
from .runner import ExitTradeResult, run_window


@dataclass
class ExitReasonCounts:
    gap_closed: int = 0
    time_expired: int = 0
    adverse_move: int = 0
    held_to_resolution: int = 0
    never_entered: int = 0

    def bump(self, reason: str) -> None:
        setattr(self, reason, getattr(self, reason) + 1)


@dataclass
class BacktestReport:
    with_exits: Stats = field(default_factory=Stats)
    baseline_no_exits: Stats = field(default_factory=Stats)
    exit_counts: ExitReasonCounts = field(default_factory=ExitReasonCounts)
    per_window: List[dict] = field(default_factory=list)

    @property
    def pnl_delta(self) -> float:
        """with_exits total P&L minus the hold-to-resolution baseline — the headline
        "did exits help" number."""
        return self.with_exits.net_pnl - self.baseline_no_exits.net_pnl


def _as_trade_result(strategy_name: str, window_id: str, exit_trade: ExitTradeResult) -> TradeResult:
    """Adapt an ExitTradeResult so evolver.models.Stats can accumulate it uniformly
    alongside hold-to-resolution trades.

    An early exit has no resolved side to win/lose against, so ``net_pnl > 0``
    counts as a win for hit-rate bookkeeping. The attached Fill carries the
    COMBINED entry+exit fee (not just the entry leg) so Stats.fees_paid reflects
    the whole round trip.
    """
    combined_fee = exit_trade.entry_fill.fee + exit_trade.exit_fill.fee
    fill_for_stats = replace(exit_trade.entry_fill, fee=combined_fee)
    return TradeResult(
        strategy_name=strategy_name,
        window_id=window_id,
        fill=fill_for_stats,
        won=exit_trade.net_pnl > 0,
        payout=exit_trade.exit_fill.proceeds,
        net_pnl=exit_trade.net_pnl,
        breakeven=pb_fees.breakeven(exit_trade.entry_fill.avg_price),
    )


def run_backtest(
    windows: List[WindowData],
    config: Config,
    params: MispricingParams,
    strategy_name: str = "mispricing",
) -> BacktestReport:
    """Run both passes (with exits, and the hold-to-resolution baseline) over every
    RESOLVED window, and aggregate the results."""
    report = BacktestReport()
    baseline_params = replace(params, exits_enabled=False)

    for w in windows:
        if not w.resolved_side:
            continue

        # Baseline pass: identical entries (deterministic given the same archived
        # polls), exits disabled -> always hold to resolution.
        base_outcome = run_window(w.polls, strategy_name, w.window_id, config, baseline_params)
        if base_outcome.entered:
            base_trade = score_trade(strategy_name, w.window_id, base_outcome.entry_fill, w.resolved_side)
            report.baseline_no_exits.record(base_trade)

        # With-exits pass.
        outcome = run_window(w.polls, strategy_name, w.window_id, config, params)
        if not outcome.entered:
            report.exit_counts.bump("never_entered")
            report.per_window.append({"window_id": w.window_id, "action": None})
            continue
        if outcome.exit_trade is not None:
            report.exit_counts.bump(outcome.exit_reason)
            report.with_exits.record(_as_trade_result(strategy_name, w.window_id, outcome.exit_trade))
            report.per_window.append({
                "window_id": w.window_id, "action": outcome.entry_signal.side,
                "exit_reason": outcome.exit_reason, "net_pnl": outcome.exit_trade.net_pnl,
            })
        else:
            # pending_resolution: no exit fired before the window ended.
            trade = score_trade(strategy_name, w.window_id, outcome.entry_fill, w.resolved_side)
            report.exit_counts.bump("held_to_resolution")
            report.with_exits.record(trade)
            report.per_window.append({
                "window_id": w.window_id, "action": outcome.entry_signal.side,
                "exit_reason": None, "net_pnl": trade.net_pnl,
            })

    return report
