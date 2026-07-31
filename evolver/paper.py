"""Live paper-trading loop for ONE strategy (no real money, no population/culling).

Unlike ``python -m evolver run`` (which evolves a whole population with breeding
and culling) or ``calibrate`` (which places REAL orders alongside the paper sim),
this just runs a single driver strategy's ``decide()`` against live windows and
scores the paper fill — for watching one already-tuned strategy trade live before
ever risking real money on it. Same in-memory-book, no-SQLite style as
``mispricing/paper.py`` and ``arb/paper.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import Config
from .context import Ctx
from .engine import score_trade
from .execution import PaperExecutor
from .market import MarketProvider
from .strategy import LoadedStrategy

log = logging.getLogger("evolver.paper")

_NO_RETIRE = 10**9  # keep the single driver alive for the whole run


@dataclass
class PaperBook:
    start_bankroll: float
    bankroll: float
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0
    log_lines: List[str] = field(default_factory=list)

    def record(self, net_pnl: float, won: bool, detail: str) -> None:
        self.trades += 1
        self.wins += 1 if won else 0
        self.net_pnl += net_pnl
        self.bankroll += net_pnl
        self.log_lines.append(detail)


def format_paper_board(book: PaperBook, name: str) -> str:
    hit = (book.wins / book.trades * 100) if book.trades else 0.0
    return (f"=== {name} paper · bankroll ${book.bankroll:.2f} · trades {book.trades} "
            f"({book.wins} won, {hit:.1f}%) · net ${book.net_pnl:+.2f} ===")


def run_paper_strategy(
    market: MarketProvider,
    driver: LoadedStrategy,
    config: Config,
    max_windows: Optional[int] = None,
    on_window: Optional[Callable[[PaperBook], None]] = None,
) -> PaperBook:
    """Loop: next_window -> decide() each poll -> paper-fill on first signal ->
    resolve -> score -> record. Runs until ``max_windows`` windows are seen or the
    caller Ctrl-Cs (KeyboardInterrupt propagates — caller's ``finally`` closes market,
    same pattern as ``evolver.calibrate.run_calibration``)."""
    paper = PaperExecutor(use_cross_book=config.use_cross_book_fill,
                          slippage_coeff=config.slippage_coeff, slippage_exp=config.slippage_exp,
                          max_slippage=config.max_slippage, participation=config.book_participation)
    book = PaperBook(start_bankroll=config.starting_bankroll, bankroll=config.starting_bankroll)
    seen = 0

    while max_windows is None or seen < max_windows:
        handle = market.next_window()
        seen += 1
        driver.start_window()
        entry = None  # (side, fill)

        for snap in market.poll_snapshots(handle):
            ctx = Ctx.from_snapshot(snap)
            action = driver.decide(ctx, config.decide_timeout_seconds, _NO_RETIRE)
            if not action:
                continue
            side = action["side"]
            token_id = handle.token_map.get(side, "")
            asks = snap.books.get(side, {}).get("asks", [])
            other = "Down" if side == "Up" else "Up"
            comp_bids = snap.books.get(other, {}).get("bids", [])
            fill = paper.fill(side, token_id, asks, config.live_stake, comp_bids)
            if fill is None:
                log.info("%s: %s signal fired but couldn't fill yet", handle.window_id, side)
                continue
            entry = (side, fill)
            break

        if entry is None:
            log.info("%s: no entry signal this window", handle.window_id)
            continue

        resolution = market.resolve(handle)  # blocks until official settlement
        resolved = resolution.official_side or resolution.coinbase_side
        if resolved is None:
            log.warning("%s: never resolved; skipping", handle.window_id)
            continue

        side, fill = entry
        trade = score_trade(driver.name, handle.window_id, fill, resolved)
        book.record(trade.net_pnl, trade.won,
                   f"{handle.window_id} {side}->{resolved} net {trade.net_pnl:+.2f}")
        log.info("%s: %s->%s, net %+.2f", handle.window_id, side, resolved, trade.net_pnl)

        print(format_paper_board(book, driver.name), flush=True)
        if on_window is not None:
            on_window(book)

    return book


def write_report(book: PaperBook, name: str, path: str) -> None:
    hit = (book.wins / book.trades * 100) if book.trades else 0.0
    lines = [
        f"# {name} — live paper-trading report\n",
        f"Trades: **{book.trades}**  ·  Wins: **{book.wins}** ({hit:.1f}%)"
        if book.trades else "Trades: **0**",
        f"Net P&L: **${book.net_pnl:+.2f}**  ·  Bankroll: ${book.start_bankroll:.2f} -> ${book.bankroll:.2f}\n",
        "## Trade log\n",
    ] + [f"- {line}" for line in book.log_lines]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
