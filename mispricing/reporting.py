"""Board/summary formatting shared by paper.py and backtest.py."""

from __future__ import annotations

from .backtest import BacktestReport


def format_backtest_report(report: BacktestReport, label: str = "mispricing") -> str:
    we, base = report.with_exits, report.baseline_no_exits
    ec = report.exit_counts
    lines = [
        f"=== {label}: backtest over {len(report.per_window)} window(s) ===",
        "",
        f"{'':14}{'trades':>8}{'wins':>6}{'hit%':>7}{'net_pnl':>12}",
        f"{'with exits':14}{we.trades:>8}{we.wins:>6}{we.hit_pct*100:>6.1f}%{we.net_pnl:>+12.2f}",
        f"{'baseline':14}{base.trades:>8}{base.wins:>6}{base.hit_pct*100:>6.1f}%{base.net_pnl:>+12.2f}",
        "",
        f"P&L delta (with_exits - baseline): {report.pnl_delta:+.2f}"
        f"  {'(exits HELPED)' if report.pnl_delta > 0 else '(exits HURT)' if report.pnl_delta < 0 else ''}",
        "",
        f"exit reasons: gap_closed={ec.gap_closed} time_expired={ec.time_expired} "
        f"adverse_move={ec.adverse_move} held_to_resolution={ec.held_to_resolution} "
        f"never_entered={ec.never_entered}",
    ]
    return "\n".join(lines)


def format_paper_board(book) -> str:
    return (
        f"=== mispricing paper · bankroll ${book.bankroll:.2f} · trades {book.trades} "
        f"({book.wins} won) · net ${book.net_pnl:+.2f} · "
        f"gap_closed={book.exit_counts.gap_closed} time_expired={book.exit_counts.time_expired} "
        f"adverse_move={book.exit_counts.adverse_move} "
        f"held_to_resolution={book.exit_counts.held_to_resolution} ==="
    )
