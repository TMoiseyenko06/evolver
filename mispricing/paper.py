"""Live paper-trading loop — ``arb/paper.py`` style: in-memory book, no SQLite.

Runs ``run_window`` (the same function the backtest uses) against a live
``LiveMarket``, prints a board after every resolved/exited window, and — unlike the
live evolver population — carries no strategies/generations schema: this is one
hand-written strategy, not a population, so a lightweight in-memory book plus an
optional end-of-run markdown summary is the right amount of persistence (matching
``arb/paper.py``'s precedent, which persists nothing during the run at all).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from evolver.config import Config
from evolver.engine import score_trade
from evolver.market import LiveMarket

from .backtest import ExitReasonCounts
from .config import MispricingParams
from .reporting import format_paper_board
from .runner import run_window

log = logging.getLogger("mispricing.paper")


@dataclass
class PaperBook:
    start_bankroll: float
    bankroll: float
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0
    exit_counts: ExitReasonCounts = field(default_factory=ExitReasonCounts)
    log_lines: list = field(default_factory=list)

    def record(self, net_pnl: float, won: bool, detail: str) -> None:
        self.trades += 1
        self.wins += 1 if won else 0
        self.net_pnl += net_pnl
        self.bankroll += net_pnl
        self.log_lines.append(detail)


def run_paper(
    market: LiveMarket,
    config: Config,
    params: MispricingParams,
    strategy_name: str = "mispricing",
    max_windows: Optional[int] = None,
    on_window: Optional[Callable[[PaperBook], None]] = None,
) -> PaperBook:
    """Loop: next_window -> run_window -> resolve if pending -> record -> print.

    Runs until ``max_windows`` windows are seen or the caller Ctrl-Cs (KeyboardInterrupt
    propagates — the caller's ``finally`` is responsible for ``market.close()``, same
    pattern as ``evolver.calibrate.run_calibration``).
    """
    book = PaperBook(start_bankroll=config.starting_bankroll, bankroll=config.starting_bankroll)
    seen = 0
    while max_windows is None or seen < max_windows:
        handle = market.next_window()
        seen += 1
        outcome = run_window(market.poll_snapshots(handle), strategy_name, handle.window_id, config, params)

        if not outcome.entered:
            log.info("%s: no entry signal this window", handle.window_id)
            continue

        if outcome.exit_trade is not None:
            book.exit_counts.bump(outcome.exit_reason)
            book.record(outcome.exit_trade.net_pnl, outcome.exit_trade.net_pnl > 0,
                       f"{handle.window_id} EXIT({outcome.exit_reason}) {outcome.entry_signal.side} "
                       f"net {outcome.exit_trade.net_pnl:+.2f}")
            log.info("%s: exited on %s, net %+.2f", handle.window_id, outcome.exit_reason,
                     outcome.exit_trade.net_pnl)
        else:
            resolution = market.resolve(handle)  # blocks until official settlement
            resolved = resolution.official_side or resolution.coinbase_side
            if resolved is None:
                log.warning("%s: never resolved; skipping", handle.window_id)
                continue
            trade = score_trade(strategy_name, handle.window_id, outcome.entry_fill, resolved)
            book.exit_counts.bump("held_to_resolution")
            book.record(trade.net_pnl, trade.won,
                       f"{handle.window_id} HOLD {outcome.entry_signal.side}->{resolved} "
                       f"net {trade.net_pnl:+.2f}")
            log.info("%s: held to resolution %s, net %+.2f", handle.window_id, resolved, trade.net_pnl)

        print(format_paper_board(book), flush=True)
        if on_window is not None:
            on_window(book)

    return book


def write_report(book: PaperBook, path: str) -> None:
    lines = [
        "# Mispricing paper-trading report\n",
        f"Trades: **{book.trades}**  ·  Wins: **{book.wins}** ({book.wins/book.trades*100:.1f}%)"
        if book.trades else "Trades: **0**",
        f"Net P&L: **${book.net_pnl:+.2f}**  ·  Bankroll: ${book.start_bankroll:.2f} -> ${book.bankroll:.2f}\n",
        f"Exit reasons: gap_closed={book.exit_counts.gap_closed} "
        f"time_expired={book.exit_counts.time_expired} adverse_move={book.exit_counts.adverse_move} "
        f"held_to_resolution={book.exit_counts.held_to_resolution}\n",
        "## Trade log\n",
    ] + [f"- {line}" for line in book.log_lines]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
