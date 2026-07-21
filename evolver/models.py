"""Core data structures shared across the engine, store, and CLI.

These are plain, JSON-friendly dataclasses. The two that matter most for
determinism are :class:`PollSnapshot` (everything a strategy sees at one poll)
and :class:`WindowData` (the full record of one resolved 5-minute window). If
these are logged faithfully, ``replay`` reconstructs the exact ``ctx`` a
strategy saw and re-derives identical decisions and fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

Level = Tuple[float, float]  # (price, size)
Book = Dict[str, List[Level]]  # {"asks": [...], "bids": [...]}


@dataclass
class PollSnapshot:
    """Exactly the market state visible to every strategy at one poll instant."""

    poll_index: int
    seconds_remaining: int
    window_open_price: float
    spot: float
    candles: List[dict]  # CLOSED 1-min candles, newest last
    books: Dict[str, Book]  # {"Up": {...}, "Down": {...}}

    def to_json(self) -> dict:
        return {
            "poll_index": self.poll_index,
            "seconds_remaining": self.seconds_remaining,
            "window_open_price": self.window_open_price,
            "spot": self.spot,
            "candles": self.candles,
            "books": _books_to_json(self.books),
        }

    @staticmethod
    def from_json(d: dict) -> "PollSnapshot":
        return PollSnapshot(
            poll_index=d["poll_index"],
            seconds_remaining=d["seconds_remaining"],
            window_open_price=d["window_open_price"],
            spot=d["spot"],
            candles=d["candles"],
            books=_books_from_json(d["books"]),
        )


@dataclass
class WindowData:
    """A fully-resolved window: all polls plus the outcome, ready to persist/replay."""

    window_id: str
    condition_id: str
    title: str
    start_iso: str
    end_iso: str
    token_map: Dict[str, str]
    polls: List[PollSnapshot] = field(default_factory=list)
    coinbase_side: Optional[str] = None  # immediate score from Coinbase 5m
    official_side: Optional[str] = None  # reconciled Gamma outcome
    resolved_side: Optional[str] = None  # final side used for scoring
    mismatch: bool = False
    candle_state_hash: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["polls"] = [p.to_json() for p in self.polls]
        return d

    @staticmethod
    def from_json(d: dict) -> "WindowData":
        w = WindowData(
            window_id=d["window_id"],
            condition_id=d["condition_id"],
            title=d["title"],
            start_iso=d["start_iso"],
            end_iso=d["end_iso"],
            token_map=d["token_map"],
            coinbase_side=d.get("coinbase_side"),
            official_side=d.get("official_side"),
            resolved_side=d.get("resolved_side"),
            mismatch=d.get("mismatch", False),
            candle_state_hash=d.get("candle_state_hash", ""),
        )
        w.polls = [PollSnapshot.from_json(p) for p in d.get("polls", [])]
        return w


@dataclass
class Fill:
    """The simulated result of walking the ask book for one entry."""

    side: str
    shares: float
    cost: float  # dollars spent on shares
    avg_price: float
    fee: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    """One strategy's decision at one poll (including passes)."""

    strategy_name: str
    poll_index: int
    action: Optional[Dict[str, str]]  # None = pass, else {"side": "Up"|"Down"}
    error: Optional[str] = None  # exception/timeout message if decide() failed


@dataclass
class TradeResult:
    """A strategy's entered position, scored against the resolved side."""

    strategy_name: str
    window_id: str
    fill: Fill
    won: bool
    payout: float
    net_pnl: float
    breakeven: float  # breakeven(avg_price)

    def to_json(self) -> dict:
        d = {
            "strategy_name": self.strategy_name,
            "window_id": self.window_id,
            "fill": self.fill.to_json(),
            "won": self.won,
            "payout": self.payout,
            "net_pnl": self.net_pnl,
            "breakeven": self.breakeven,
        }
        return d


@dataclass
class Stats:
    """Accumulating performance stats — used for both per-generation and lifetime."""

    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0
    sum_breakeven: float = 0.0  # sum of breakeven(avg_price) over trades
    fees_paid: float = 0.0

    def record(self, result: TradeResult) -> None:
        self.trades += 1
        if result.won:
            self.wins += 1
        self.net_pnl += result.net_pnl
        self.sum_breakeven += result.breakeven
        self.fees_paid += result.fill.fee

    @property
    def hit_pct(self) -> float:
        """Win fraction in [0, 1]."""
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_breakeven(self) -> float:
        return self.sum_breakeven / self.trades if self.trades else 0.0

    @property
    def tiebreak(self) -> float:
        """Ranking tiebreak: hit% minus average breakeven."""
        return self.hit_pct - self.avg_breakeven

    def reset(self) -> None:
        self.trades = 0
        self.wins = 0
        self.net_pnl = 0.0
        self.sum_breakeven = 0.0
        self.fees_paid = 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Stats":
        return Stats(**{k: d.get(k, 0) for k in ("trades", "wins", "net_pnl", "sum_breakeven", "fees_paid")})


def _books_to_json(books: Dict[str, Book]) -> dict:
    return {
        side: {"asks": [list(l) for l in b.get("asks", [])], "bids": [list(l) for l in b.get("bids", [])]}
        for side, b in books.items()
    }


def _books_from_json(d: dict) -> Dict[str, Book]:
    return {
        side: {
            "asks": [tuple(l) for l in b.get("asks", [])],
            "bids": [tuple(l) for l in b.get("bids", [])],
        }
        for side, b in d.items()
    }
