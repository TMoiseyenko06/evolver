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
    def fill(self, side: str, token_id: str, asks: List[Level], usd: float) -> Optional[Fill]: ...


class PaperExecutor:
    """Simulates the fill by walking the ask book (the existing paper behavior)."""

    def fill(self, side: str, token_id: str, asks: List[Level], usd: float) -> Optional[Fill]:
        return simulate_fill(side, asks, usd)


@dataclass
class SynthesisExecutor:
    """Places a real MARKET order via Synthesis and returns the actual fill."""

    client: SynthesisClient
    slippage_cap: Optional[float] = 0.98
    last_order: Optional[OrderResult] = None

    def fill(self, side: str, token_id: str, asks: List[Level], usd: float) -> Optional[Fill]:
        order = self.client.place_market_order(token_id, "BUY", usd, self.slippage_cap)
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
    return SynthesisExecutor(client=client, slippage_cap=config.order_slippage_cap)
