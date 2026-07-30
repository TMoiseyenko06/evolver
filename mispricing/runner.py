"""The one symmetric entry+exit loop — identical whether fed by a live market, the
test harness's mock market, or a plain list of archived polls.

``run_window`` takes only an ``Iterable[PollSnapshot]``: no ``Store``, no
``LiveMarket``, no ``WindowData`` coupling. That's what lets the SAME decision logic
run in ``backtest.py`` (against already-collected history) and ``paper.py`` (live)
without two implementations that could quietly drift apart — a backtest result is
only a true prediction of live behavior if it's produced by the exact code that
would run live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from evolver.config import Config
from evolver.context import Ctx
from evolver.engine import score_exit_trade, simulate_exit_fill, simulate_fill
from evolver.models import ExitFill, ExitTradeResult, Fill, PollSnapshot

from .config import MispricingParams
from .signals import EntrySignal, ExitDecision, OpenPosition, check_exit, entry_signal


@dataclass
class WindowOutcome:
    window_id: str
    entered: bool = False
    entry_signal: Optional[EntrySignal] = None
    entry_fill: Optional[Fill] = None
    exit_reason: Optional[str] = None
    exit_fill: Optional[ExitFill] = None
    exit_trade: Optional[ExitTradeResult] = None
    # True when a position is open but the polls ran out before any exit fired —
    # the caller resolves it (mechanics differ: an already-known resolved_side for
    # archived windows vs. a live, blocking market.resolve(handle) call).
    pending_resolution: bool = False


def run_window(
    polls: Iterable[PollSnapshot],
    strategy_name: str,
    window_id: str,
    config: Config,
    params: MispricingParams,
) -> WindowOutcome:
    """Run entry+exit logic over one window's polls. Returns a WindowOutcome.

    While flat: check ``entry_signal`` each poll. If it fires but the book can't
    fill (thin book), stay flat and keep scanning LATER polls in the same window —
    deliberately looser than ``evolver.replay``'s hard-stop-at-first-signal contract,
    since a signal that can't fill yet may fill on a later poll.

    While holding: check ``check_exit`` each poll. On a trigger, sell via
    ``simulate_exit_fill``. A thin bid book that can't sell the FULL position size
    does NOT fire the exit (stay holding, re-check next poll) — never a silent
    partial close.

    If ``polls`` is exhausted while still holding (no exit ever fired),
    ``pending_resolution=True`` is set and the caller does the final resolve step.
    """
    outcome = WindowOutcome(window_id=window_id)
    position: Optional[OpenPosition] = None

    for poll_index, snap in enumerate(polls):
        ctx = Ctx.from_snapshot(snap)

        if position is None:
            sig = entry_signal(ctx.candles, ctx.window_open_price, ctx.spot,
                               ctx.seconds_remaining, ctx.books, params)
            if sig is None:
                continue
            asks = snap.books.get(sig.side, {}).get("asks", [])
            other = "Down" if sig.side == "Up" else "Up"
            comp_bids = snap.books.get(other, {}).get("bids", []) if config.use_cross_book_fill else None
            fill = simulate_fill(sig.side, asks, config.stake, comp_bids,
                                 config.slippage_coeff, config.slippage_exp,
                                 max_slippage=config.max_slippage,
                                 participation=config.book_participation)
            if fill is None:
                continue  # signal fired but couldn't fill yet — keep scanning
            position = OpenPosition(
                side=sig.side, fill=fill, entry_model_p=sig.model_p,
                entry_poll_index=poll_index, entry_seconds_remaining=snap.seconds_remaining,
            )
            outcome.entered = True
            outcome.entry_signal = sig
            outcome.entry_fill = fill
            continue

        if not params.exits_enabled:
            continue  # baseline mode: never check exits, always hold to resolution

        decision = check_exit(position, ctx.candles, ctx.window_open_price, ctx.spot,
                              ctx.seconds_remaining, ctx.books, params)
        if decision is None:
            continue
        bids = snap.books.get(position.side, {}).get("bids", [])
        exit_fill = simulate_exit_fill(position.side, bids, position.fill.shares,
                                       participation=config.book_participation)
        if exit_fill is None or exit_fill.shares < position.fill.shares - 1e-9:
            continue  # bid book can't absorb the full size yet — keep holding
        trade = score_exit_trade(strategy_name, window_id, position.fill, exit_fill, decision.reason)
        outcome.exit_reason = decision.reason
        outcome.exit_fill = exit_fill
        outcome.exit_trade = trade
        return outcome

    if position is not None and outcome.exit_trade is None:
        outcome.pending_resolution = True
    return outcome
