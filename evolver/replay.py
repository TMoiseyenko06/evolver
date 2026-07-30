"""Deterministic replay of a strategy against archived window data.

Both the CLI ``replay`` command and the near-duplicate diversity check run
strategies over logged :class:`~evolver.models.WindowData`. Because a window log
contains every poll snapshot (candles, books, spot, seconds_remaining) and the
final resolved side, re-running is a pure function of the logs: same code + same
windows => same decisions, fills, and P&L, every time.

A fresh sandboxed instance is compiled per replay so live population objects are
never mutated (no failure bookkeeping, no bankroll side effects).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .context import Ctx
from .engine import score_trade, simulate_fill
from .models import Stats, WindowData
from .sandbox import StrategyTimeout, compile_strategy, run_with_timeout


def _first_action(instance, window: WindowData, config: Config) -> Optional[dict]:
    """Replay one window; return the first {"side": ...} entered, or None (pass)."""
    for snap in window.polls:
        ctx = Ctx.from_snapshot(snap)
        try:
            result = run_with_timeout(instance.decide, (ctx,), config.decide_timeout_seconds)
        except (StrategyTimeout, BaseException):  # noqa: BLE001 — untrusted code
            continue
        if isinstance(result, dict) and result.get("side") in ("Up", "Down"):
            return {"side": result["side"], "poll_index": snap.poll_index}
    return None


def action_vector(source: str, windows: List[WindowData], config: Config) -> List[Optional[str]]:
    """Per-window entered side (or None), used for near-duplicate detection."""
    instance = compile_strategy(source, config.allowed_imports)
    out: List[Optional[str]] = []
    for w in windows:
        act = _first_action(instance, w, config)
        out.append(act["side"] if act else None)
    return out


def agreement(a: List[Optional[str]], b: List[Optional[str]]) -> float:
    """Fraction of *active* windows where two action vectors agree (1.0 identical).

    Windows where BOTH strategies passed are ignored — two strategies aren't
    duplicates just because they're both quiet; they're duplicates when they
    TRADE the same windows the same way. This catches thematic clones (e.g. many
    reversion strategies that all fade the same moves) that a pass-inclusive
    metric would miss.
    """
    n = min(len(a), len(b))
    pairs = [(a[i], b[i]) for i in range(n) if not (a[i] is None and b[i] is None)]
    if not pairs:
        return 0.0
    same = sum(1 for x, y in pairs if x == y)
    return same / len(pairs)


@dataclass
class ReplayResult:
    stats: Stats = field(default_factory=Stats)
    per_window: List[dict] = field(default_factory=list)

    @property
    def bankroll(self) -> float:
        return self.stats.net_pnl  # relative; caller adds starting bankroll


def replay_strategy(source: str, windows: List[WindowData], config: Config) -> ReplayResult:
    """Re-score ``source`` against archived ``windows`` deterministically."""
    instance = compile_strategy(source, config.allowed_imports)
    result = ReplayResult()
    for w in windows:
        if not w.resolved_side:
            continue
        act = _first_action(instance, w, config)
        if act is None:
            result.per_window.append({"window_id": w.window_id, "action": None})
            continue
        side = act["side"]
        # Fill against the book at the poll where the strategy entered.
        entry_snap = next((s for s in w.polls if s.poll_index == act["poll_index"]), w.polls[-1])
        asks = entry_snap.books.get(side, {}).get("asks", [])
        other = "Down" if side == "Up" else "Up"
        comp_bids = entry_snap.books.get(other, {}).get("bids", []) if config.use_cross_book_fill else None
        fill = simulate_fill(side, asks, config.stake, comp_bids,
                             config.slippage_coeff, config.slippage_exp,
                             max_slippage=config.max_slippage,
                             participation=config.book_participation)
        if fill is None:
            result.per_window.append({"window_id": w.window_id, "action": side, "filled": False})
            continue
        trade = score_trade("<replay>", w.window_id, fill, w.resolved_side)
        result.stats.record(trade)
        result.per_window.append(
            {
                "window_id": w.window_id,
                "action": side,
                "won": trade.won,
                "net_pnl": trade.net_pnl,
                "avg_price": fill.avg_price,
            }
        )
    return result
