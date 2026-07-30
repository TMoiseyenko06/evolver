"""Execution backends: simulate a fill (paper) or place a real order (Synthesis).

Both return the same :class:`~evolver.models.Fill`, so downstream scoring
(`engine.score_trade`) is identical and a paper fill and a real fill are directly
comparable — which is exactly what the calibration experiment needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol

from polybot.synthesis import OrderResult, SynthesisClient

from .config import Config
from .engine import simulate_fill
from .models import Fill, Level


class Executor(Protocol):
    def fill(self, side: str, token_id: str, asks: List[Level], usd: float,
             complement_bids: Optional[List[Level]] = None) -> Optional[Fill]: ...


@dataclass
class PaperExecutor:
    """Simulates the fill with the same fill-realism model the evolver/tuner use
    (per-window no-arb reconstruction from the complement book, else the slippage
    curve), so calibration validates the exact sim that ranks strategies.

    The calibration report's real−paper P&L residual then measures how accurate the
    fill model is: centred on 0 => the sim matches reality.
    """

    use_cross_book: bool = True
    slippage_coeff: float = 0.0
    slippage_exp: float = 2.0
    max_slippage: Optional[float] = None   # mirror the real order's price guard
    participation: float = 1.0             # fraction of displayed size assumed executable

    def fill(self, side: str, token_id: str, asks: List[Level], usd: float,
             complement_bids: Optional[List[Level]] = None) -> Optional[Fill]:
        comp = complement_bids if self.use_cross_book else None
        return simulate_fill(side, asks, usd, comp, self.slippage_coeff, self.slippage_exp,
                             max_slippage=self.max_slippage, participation=self.participation)


@dataclass
class SynthesisExecutor:
    """Places a real MARKET order via Synthesis and returns the actual fill.

    The order carries a PRICE GUARD of ``best_ask + max_slippage`` (a marketable
    limit): if the book has moved or is too thin to fill near the price the strategy
    decided on, the order simply doesn't fill and we skip the window — instead of
    filling far above fair value, which is a negative-EV trade even when it wins.
    """

    client: SynthesisClient
    slippage_cap: Optional[float] = 0.98     # fallback when max_slippage is None
    max_slippage: Optional[float] = 0.03     # cents above best ask we'll tolerate
    last_order: Optional[OrderResult] = None

    def price_cap(self, asks: List[Level]) -> Optional[float]:
        """The max price to send with the order: best_ask + max_slippage."""
        if self.max_slippage is None or not asks:
            return self.slippage_cap
        best_ask = min(p for p, _ in asks if p > 0) if any(p > 0 for p, _ in asks) else None
        if best_ask is None:
            return self.slippage_cap
        return min(round(best_ask + self.max_slippage, 4), 0.999)

    def fill(self, side: str, token_id: str, asks: List[Level], usd: float,
             complement_bids: Optional[List[Level]] = None) -> Optional[Fill]:
        # complement_bids is unused for a real order (the venue fills it), but kept to
        # match the Executor protocol so paper and real are drop-in interchangeable.
        order = self.client.place_market_order(token_id, "BUY", usd, self.price_cap(asks))
        self.last_order = order
        if order.shares <= 0:
            return None
        cost = order.filled if order.filled > 0 else order.amount_usdc
        avg_price = order.price if order.price > 0 else (cost / order.shares if order.shares else 0.0)
        return Fill(side=side, shares=order.shares, cost=cost, avg_price=avg_price, fee=order.fee)


def build_synthesis_executor(config: Config) -> SynthesisExecutor:
    client = SynthesisClient(
        api_key=config.synthesis_api_key,
        wallet_id=config.synthesis_wallet_id,
        base_url=config.synthesis_base_url,
    )
    return SynthesisExecutor(client=client, slippage_cap=config.order_slippage_cap,
                             max_slippage=config.max_slippage)
