"""Pure paper-trading accounting: fills, fees, resolution, scoring.

Everything here is a deterministic function of its inputs — no network, no clock,
no globals — which is what makes ``replay`` reproduce live results exactly and
makes the accounting unit-testable.
"""

from __future__ import annotations

from typing import List, Optional

from polybot import fees

from .models import Book, Fill, Level, TradeResult


def walk_ask_book(asks: List[Level], stake: float, participation: float = 1.0) -> tuple:
    """Walk the ascending ask book spending up to ``stake`` dollars on shares.

    Returns ``(shares, cost, avg_price)``. Levels are consumed best (lowest) price
    first; the last level may be partially filled. If the book cannot absorb the
    full stake, ``cost`` will be less than ``stake`` (thin book).

    ``participation`` (0-1) is the fraction of each level's DISPLAYED size we assume
    is actually executable for us. Displayed size is not all real or all ours — some
    is stale, some is spoofed, and other takers compete for it — so assuming we sweep
    100% of a level is optimistic. This bites exactly where it should: on thin books
    (a $10 order against 100 displayed shares at 0.10 gets a partial fill instead of
    the whole level) while leaving deep books unchanged (25% of thousands of shares
    still covers a $10 order), which preserves the fill accuracy calibration measured
    at normal 0.40-0.60 prices.
    """
    part = 1.0 if participation is None else max(0.0, min(1.0, participation))
    remaining = stake
    shares = 0.0
    cost = 0.0
    for price, size in sorted(asks, key=lambda x: x[0]):
        if remaining <= 1e-12 or price <= 0:
            break
        avail = size * part
        if avail <= 0:
            continue
        level_notional = price * avail
        if level_notional <= remaining:
            shares += avail
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


def no_arb_floor(complement_bids: Optional[List[Level]]) -> Optional[float]:
    """The no-arbitrage lower bound on the executable ask, from the complement book.

    On a binary market Up+Down shares redeem for $1, so a market-maker bidding
    ``cb`` for the OTHER side is implicitly offering THIS side at ``1 - cb`` (the
    same position). The executable ask therefore can't sit below ``1 - best
    complement bid`` — a displayed ask cheaper than that is a phantom/stale order the
    MMs would arb away. Live calibration confirmed this: a 0.06 displayed Down ask
    executed near 0.17 while Up was bid ~0.83 (1 - 0.83 = 0.17).

    Returns ``1 - best_bid`` in (0, 1), or None when there's no usable complement bid.
    """
    if not complement_bids:
        return None
    best_bid = max((p for p, _ in complement_bids if 0.0 < p < 1.0), default=None)
    if best_bid is None:
        return None
    return 1.0 - best_bid


def apply_slippage(avg_price: float, slippage_coeff: float, slippage_exp: float) -> float:
    """Fallback slippage model for when the complement book is unavailable.

    Worsens the fill price by ``slippage_coeff * (0.5 - price)**slippage_exp`` for
    sub-0.50 entries (≈0 at normal/favorite prices, large at the cheap extreme).
    Used only when :func:`no_arb_floor` can't be computed (no complement bids), since
    the per-window no-arb reconstruction is more accurate than this aggregate curve.
    """
    if slippage_coeff <= 0 or avg_price <= 0:
        return avg_price
    gap = 0.5 - avg_price
    if gap <= 0:  # buying the favorite side — displayed book is realistic enough
        return avg_price
    slip = slippage_coeff * (gap ** slippage_exp)
    return min(avg_price + slip, 0.999)


def executable_price(
    avg_price: float,
    complement_bids: Optional[List[Level]],
    slippage_coeff: float,
    slippage_exp: float,
) -> float:
    """Correct a displayed fill price to what the market would actually execute.

    Prefers the exact per-window no-arbitrage floor from the complement book; falls
    back to the parametric slippage curve when no complement bids are available.
    """
    floor = no_arb_floor(complement_bids)
    if floor is not None:
        return min(max(avg_price, floor), 0.999)
    return apply_slippage(avg_price, slippage_coeff, slippage_exp)


def price_cap(asks: List[Level], max_slippage: Optional[float]) -> Optional[float]:
    """The highest price we'd accept: ``best_ask + max_slippage`` (None = no cap).

    Mirrors the guard the real executor sends with its order, so paper doesn't
    assume fills the venue would refuse.
    """
    if max_slippage is None or not asks:
        return None
    prices = [p for p, _ in asks if p > 0]
    if not prices:
        return None
    return min(min(prices) + max_slippage, 0.999)


def simulate_fill(
    side: str,
    asks: List[Level],
    stake: float,
    complement_bids: Optional[List[Level]] = None,
    slippage_coeff: float = 0.0,
    slippage_exp: float = 2.0,
    max_slippage: Optional[float] = None,
    participation: float = 1.0,
) -> Optional[Fill]:
    """Simulate buying ``stake`` dollars of ``side`` by walking its ask book.

    The taker fee is charged on the filled shares at the volume-weighted average
    fill price, matching ``ctx.fee`` so a strategy's breakeven estimate lines up
    with what it actually pays. Returns None if nothing could be filled.

    Displayed liquidity at cheap "longshot" prices is largely non-executable, so the
    walked price is corrected via :func:`executable_price` — the per-window no-arb
    floor from ``complement_bids`` (the OTHER side's bid book) when available, else
    the parametric slippage curve. The same dollars then fill at the worse price, so
    you get fewer shares — which stops the sim from paying longshot strategies for
    fills the market never gives. Pass no ``complement_bids`` and ``slippage_coeff=0``
    for the raw (idealised) walk.
    """
    shares, cost, avg_price = walk_ask_book(asks, stake, participation)
    if shares <= 0:
        return None
    eff_price = executable_price(avg_price, complement_bids, slippage_coeff, slippage_exp)
    if eff_price > avg_price:
        shares = cost / eff_price  # same dollars, worse price => fewer shares
        avg_price = eff_price
    cap = price_cap(asks, max_slippage)
    if cap is not None and avg_price > cap:
        return None  # the real order's price guard would reject this fill
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
