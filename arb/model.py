"""Normalized market model shared by the detectors and the scanner.

Synthesis returns Polymarket and Kalshi markets in one shape (``left/right_outcome``,
``left/right_price``, ``left/right_token_id``), so both venues map onto the same
:class:`Market` with two :class:`Quote` outcomes. Prices from the listing are
indicative (mid); the executable ``ask`` is filled in later from the order book.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Quote:
    """One outcome of a market: its token, indicative price, and (once fetched) book."""

    outcome: str                       # canonical label, e.g. "Yes"/"No"/"Up"/"Down"
    token_id: str
    mid: float = 0.0                   # left/right_price from the listing (indicative)
    ask: Optional[float] = None        # best (lowest) DISPLAYED ask
    ask_size: float = 0.0              # shares available at/near the best ask
    bid: Optional[float] = None        # best (highest) bid
    ask_exec: Optional[float] = None   # EXECUTABLE ask after cross-book/no-arb correction

    def eff_ask(self, realistic: bool) -> Optional[float]:
        """The price a buyer actually pays: executable (realistic) or displayed."""
        if realistic and self.ask_exec is not None:
            return self.ask_exec
        return self.ask


@dataclass
class Market:
    """A binary (two-outcome) prediction market, venue-tagged."""

    venue: str                         # "polymarket" | "kalshi"
    market_id: str                     # condition_id (poly) / market_id (kalshi)
    event_id: str
    title: str
    ends_at: Optional[str]
    resolved: bool
    quotes: List[Quote] = field(default_factory=list)
    liquidity: float = 0.0
    volume: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    def quote(self, outcome: str) -> Optional[Quote]:
        for q in self.quotes:
            if q.outcome.lower() == outcome.lower():
                return q
        return None

    @property
    def token_ids(self) -> List[str]:
        return [q.token_id for q in self.quotes if q.token_id]


@dataclass
class ArbLeg:
    """One buy in an arbitrage set: acquire ``outcome`` on ``venue`` at ``ask``."""

    venue: str
    market_id: str
    title: str
    outcome: str
    token_id: str
    ask: float
    ask_size: float


@dataclass
class ArbOpportunity:
    """A set of legs that together cover every outcome for < $1 → guaranteed profit.

    ``edge`` is the net profit per $1 of guaranteed payout (i.e. per matched share
    set) after fees; positive means a real arb. ``max_size`` is bounded by the
    thinnest leg's book, and ``max_profit = edge * max_size``.
    """

    kind: str                          # "intra" | "cross"
    title: str
    legs: List[ArbLeg]
    gross_cost: float                  # sum of leg asks
    fees: float                        # combined per-set fee
    net_cost: float                    # gross_cost + fees
    edge: float                        # 1 - net_cost  (>0 = arb)
    max_size: float                    # min ask_size across legs
    ends_at: Optional[str] = None
    sum_mid: Optional[float] = None    # field arb: Σ mid prices (completeness signal, ~1 = full field)

    @property
    def max_profit(self) -> float:
        return max(0.0, self.edge) * self.max_size

    @property
    def venues(self) -> List[str]:
        return sorted({leg.venue for leg in self.legs})
