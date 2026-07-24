"""Pure paper-trading accounting: fills, fees, resolution, scoring.

Everything here is a deterministic function of its inputs — no network, no clock,
no globals — which is what makes ``replay`` reproduce live results exactly and
makes the accounting unit-testable.
"""

from __future__ import annotations

from typing import List, Optional

from polybot import fees

from .models import Book, Fill, Level, TradeResult


def walk_ask_book(asks: List[Level], stake: float) -> tuple:
    """Walk the ascending ask book spending up to ``stake`` dollars on shares.

    Returns ``(shares, cost, avg_price)``. Levels are consumed best (lowest) price
    first; the last level may be partially filled. If the book cannot absorb the
    full stake, ``cost`` will be less than ``stake`` (thin book).
    """
    remaining = stake
    shares = 0.0
    cost = 0.0
    for price, size in sorted(asks, key=lambda x: x[0]):
        if remaining <= 1e-12 or price <= 0:
            break
        level_notional = price * size
        if level_notional <= remaining:
            shares += size
            cost += level_notional
            remaining -= level_notional
        else:
            take_shares = remaining / price
            shares += take_shares
            cost += remaining
            remaining = 0.0
            break
    avg_price = (cost / shares) if shares > 0 else 0.0
    return shares, cost, avg_price


def apply_slippage(avg_price: float, slippage_coeff: float, slippage_exp: float) -> float:
    """Worsen a fill price to model thin/phantom liquidity at cheap prices.

    The displayed ask book is walked to get ``avg_price``, but displayed size at
    cheap "longshot" prices is largely non-executable: live calibration showed a
    0.06 displayed fill actually executing near 0.17. We model that as
    ``slip = slippage_coeff * (0.5 - price)**slippage_exp`` for sub-0.50 entries —
    it grows toward the cheap extreme and is ~0 at normal/favorite prices (where the
    sim already matches reality). The effective price is capped just under 1.0.
    """
    if slippage_coeff <= 0 or avg_price <= 0:
        return avg_price
    gap = 0.5 - avg_price
    if gap <= 0:  # buying the favorite side — displayed book is realistic enough
        return avg_price
    slip = slippage_coeff * (gap ** slippage_exp)
    return min(avg_price + slip, 0.999)


def simulate_fill(
    side: str,
    asks: List[Level],
    stake: float,
    slippage_coeff: float = 0.0,
    slippage_exp: float = 2.0,
) -> Optional[Fill]:
    """Simulate buying ``stake`` dollars of ``side`` by walking its ask book.

    The taker fee is charged on the filled shares at the volume-weighted average
    fill price, matching ``ctx.fee`` so a strategy's breakeven estimate lines up
    with what it actually pays. Returns None if nothing could be filled.

    ``slippage_coeff`` (0 = off) models non-executable displayed liquidity at cheap
    prices (see :func:`apply_slippage`): the same dollars fill at a worse price, so
    you get fewer shares — which is what stops the sim from paying longshot
    strategies for fills the market never gives.
    """
    shares, cost, avg_price = walk_ask_book(asks, stake)
    if shares <= 0:
        return None
    eff_price = apply_slippage(avg_price, slippage_coeff, slippage_exp)
    if eff_price > avg_price:
        shares = cost / eff_price  # same dollars, worse price => fewer shares
        avg_price = eff_price
    fee = fees.fee(shares, avg_price)
    return Fill(side=side, shares=shares, cost=cost, avg_price=avg_price, fee=fee)


def score_trade(strategy_name: str, window_id: str, fill: Fill, resolved_side: str) -> TradeResult:
    """Score a filled position against the resolved winning side.

    Winning shares each pay $1; losing shares pay $0. Net P&L is
    ``payout - cost - fee``.
    """
    won = fill.side == resolved_side
    payout = fill.shares if won else 0.0
    net_pnl = payout - fill.cost - fill.fee
    return TradeResult(
        strategy_name=strategy_name,
        window_id=window_id,
        fill=fill,
        won=won,
        payout=payout,
        net_pnl=net_pnl,
        breakeven=fees.breakeven(fill.avg_price),
    )


def coinbase_score(five_min_candle: dict) -> str:
    """Score a window from its Coinbase 5-minute candle: close>open -> Up, else Down.

    A tie resolves Down, matching Polymarket's convention for these markets.
    """
    return "Up" if five_min_candle["close"] > five_min_candle["open"] else "Down"
