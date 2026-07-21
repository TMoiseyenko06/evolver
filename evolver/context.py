"""The ``ctx`` object handed to ``Strategy.decide``.

Built fresh per strategy per poll from a :class:`~evolver.models.PollSnapshot`,
so untrusted code can freely mutate its copy without affecting the shared feed
or any other strategy. ``fee`` and ``breakeven`` are bound to the real fee curve
in :mod:`polybot.fees`.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

from polybot import fees

from .models import Book, PollSnapshot


class Ctx:
    """Read-model of the market for one decision.

    Attributes match the strategy contract exactly:
      * ``candles``            list of {time, open, close, ...}, CLOSED, newest last
      * ``window_open_price``  BTC price at window open
      * ``seconds_remaining``  seconds until the window resolves
      * ``books``              {"Up": {"asks":[(p,sz)], "bids":[...]}, "Down": {...}}
      * ``spot``               latest trade price
      * ``fee(shares, price)`` dollar taker fee
      * ``breakeven(ask)``     breakeven win-probability for buying at ``ask``
    """

    __slots__ = ("candles", "window_open_price", "seconds_remaining", "books", "spot")

    def __init__(
        self,
        candles: List[dict],
        window_open_price: float,
        seconds_remaining: int,
        books: Dict[str, Book],
        spot: float,
    ):
        # Deep-copy the mutable structures so strategy code cannot corrupt the feed.
        self.candles = copy.deepcopy(candles)
        self.window_open_price = window_open_price
        self.seconds_remaining = seconds_remaining
        self.books = copy.deepcopy(books)
        self.spot = spot

    @staticmethod
    def fee(shares: float, price: float) -> float:
        return fees.fee(shares, price)

    @staticmethod
    def breakeven(ask: float) -> float:
        return fees.breakeven(ask)

    @classmethod
    def from_snapshot(cls, snap: PollSnapshot) -> "Ctx":
        return cls(
            candles=snap.candles,
            window_open_price=snap.window_open_price,
            seconds_remaining=snap.seconds_remaining,
            books=snap.books,
            spot=snap.spot,
        )
