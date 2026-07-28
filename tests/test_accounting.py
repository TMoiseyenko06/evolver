"""Tests for fill simulation, the fee curve, resolution scoring, and reconciliation."""

import math

import pytest

from polybot import fees

from evolver.engine import (
    coinbase_score,
    score_trade,
    simulate_fill,
    walk_ask_book,
)
from evolver.market import Resolution
from evolver.models import Fill
from evolver.strategy import LoadedStrategy


# --- fee curve ------------------------------------------------------------ #
def test_fee_formula():
    # fee = 0.0312 * shares * min(price, 1-price)
    assert fees.fee(100, 0.50) == pytest.approx(0.0312 * 100 * 0.50)
    assert fees.fee(100, 0.90) == pytest.approx(0.0312 * 100 * 0.10)
    assert fees.fee(100, 0.10) == pytest.approx(0.0312 * 100 * 0.10)


def test_fee_peaks_at_midpoint_and_decays_at_extremes():
    mid = fees.fee_per_share(0.50)
    near = fees.fee_per_share(0.55)
    extreme = fees.fee_per_share(0.95)
    assert mid > near > extreme
    assert extreme < 0.002  # ~0 at the extremes


def test_breakeven_is_ask_plus_fee():
    assert fees.breakeven(0.55) == pytest.approx(0.55 + 0.0312 * 0.45)


# --- walking the ask book ------------------------------------------------- #
def test_walk_single_level():
    shares, cost, avg = walk_ask_book([(0.50, 1000)], 10.0)
    assert cost == pytest.approx(10.0)
    assert shares == pytest.approx(20.0)  # $10 / 0.50
    assert avg == pytest.approx(0.50)


def test_walk_consumes_cheapest_first_then_partial():
    # $10 across (0.40, 10 shares=$4) then (0.60, ...) for remaining $6 -> 10 sh.
    shares, cost, avg = walk_ask_book([(0.60, 100), (0.40, 10)], 10.0)
    assert cost == pytest.approx(10.0)
    # 10 shares @0.40 ($4) + 10 shares @0.60 ($6) = 20 shares
    assert shares == pytest.approx(20.0)
    assert avg == pytest.approx(0.50)


def test_walk_thin_book_spends_less_than_stake():
    shares, cost, avg = walk_ask_book([(0.50, 4)], 10.0)  # only $2 available
    assert cost == pytest.approx(2.0)
    assert shares == pytest.approx(4.0)


def test_simulate_fill_applies_fee_at_avg_price():
    fill = simulate_fill("Up", [(0.50, 1000)], 10.0)
    assert fill is not None
    assert fill.shares == pytest.approx(20.0)
    assert fill.fee == pytest.approx(fees.fee(20.0, 0.50))


def test_simulate_fill_empty_book_returns_none():
    assert simulate_fill("Up", [], 10.0) is None


def test_limit_order_does_not_pay_above_limit():
    # Book: 5 shares @ 0.44, then deep @ 0.50. A limit of 0.46 fills only the 0.44
    # level (never crosses to 0.50), so a partial fill at 0.44 — no slippage, price
    # certainty, and NO cross-book correction (the cap is the protection).
    asks = [(0.44, 5), (0.50, 100000)]
    fill = simulate_fill("Up", asks, 10.0, complement_bids=[(0.40, 100000)], limit=0.46)
    assert fill is not None
    assert fill.avg_price == pytest.approx(0.44)     # capped: never paid the 0.50 level
    assert fill.shares == pytest.approx(5.0)         # only the 5 shares offered <= limit
    assert fill.cost == pytest.approx(5 * 0.44)      # partial fill, < full stake


def test_limit_order_no_fill_when_ask_above_limit_returns_none():
    # Best ask 0.50 but limit 0.46 -> nothing offered at your price -> None (the caller
    # keeps trying later polls; a resting order waiting for its price).
    assert simulate_fill("Up", [(0.50, 100000)], 10.0, limit=0.46) is None


def test_limit_order_fills_at_ask_when_ask_below_limit():
    # Ask 0.42 <= limit 0.46 -> you fill at the better price 0.42, not the limit.
    fill = simulate_fill("Up", [(0.42, 100000)], 10.0, limit=0.46)
    assert fill is not None
    assert fill.avg_price == pytest.approx(0.42)


def test_cross_book_no_arb_reconstruction():
    from evolver.engine import no_arb_floor

    # Reproduces the live calibration finding: a displayed 0.06 Down ask is phantom;
    # with the complement (Up) bid at 0.83, the true executable price is 1-0.83=0.17,
    # so the same dollars buy ~1/3 the shares.
    up_bids = [(0.83, 100000)]
    assert no_arb_floor(up_bids) == pytest.approx(0.17)
    ideal = simulate_fill("Down", [(0.06, 100000)], 1.0)                       # raw walk
    real = simulate_fill("Down", [(0.06, 100000)], 1.0, complement_bids=up_bids)
    assert ideal.avg_price == pytest.approx(0.06)
    assert real.avg_price == pytest.approx(0.17)         # clamped up to the no-arb floor
    assert real.shares < ideal.shares * 0.4              # ~1/3 the shares => ~1/3 payout
    assert real.cost == pytest.approx(ideal.cost)        # same dollars spent


def test_cross_book_leaves_consistent_prices_alone():
    # When the displayed ask already clears the no-arb floor (normal, tight market),
    # cross-book barely changes it.
    down_bids = [(0.60, 1000)]  # floor for an Up buy = 1 - 0.60 = 0.40
    fill = simulate_fill("Up", [(0.39, 1000)], 10.0, complement_bids=down_bids)
    assert 0.39 <= fill.avg_price <= 0.41  # only lifted to the ~0.40 no-arb floor


def test_slippage_curve_fallback_when_no_complement_book():
    from evolver.engine import apply_slippage

    # With no complement bids, fall back to the parametric slippage curve.
    ideal = simulate_fill("Down", [(0.06, 100000)], 1.0)
    real = simulate_fill("Down", [(0.06, 100000)], 1.0, slippage_coeff=0.55, slippage_exp=2.0)
    assert real.avg_price == pytest.approx(apply_slippage(0.06, 0.55, 2.0), rel=1e-6)
    assert real.avg_price > 0.16
    assert real.shares < ideal.shares * 0.4


# --- scoring -------------------------------------------------------------- #
def test_winning_trade_pnl():
    fill = Fill(side="Up", shares=20.0, cost=10.0, avg_price=0.50, fee=0.312)
    r = score_trade("s", "w", fill, resolved_side="Up")
    assert r.won is True
    assert r.payout == pytest.approx(20.0)
    assert r.net_pnl == pytest.approx(20.0 - 10.0 - 0.312)


def test_losing_trade_pnl():
    fill = Fill(side="Up", shares=20.0, cost=10.0, avg_price=0.50, fee=0.312)
    r = score_trade("s", "w", fill, resolved_side="Down")
    assert r.won is False
    assert r.payout == 0.0
    assert r.net_pnl == pytest.approx(-10.0 - 0.312)


def test_buying_obvious_side_at_55c_loses_when_right_barely():
    # A favorite bought at 0.55 that wins pays < break-even edge unless p is high.
    fill = simulate_fill("Up", [(0.55, 1000)], 10.0)
    r_win = score_trade("s", "w", fill, "Up")
    r_loss = score_trade("s", "w", fill, "Down")
    # One win + one loss nets negative -> directional accuracy alone loses.
    assert r_win.net_pnl + r_loss.net_pnl < 0


def test_coinbase_score_close_gt_open_is_up_tie_is_down():
    assert coinbase_score({"open": 100.0, "close": 101.0}) == "Up"
    assert coinbase_score({"open": 100.0, "close": 99.0}) == "Down"
    assert coinbase_score({"open": 100.0, "close": 100.0}) == "Down"  # tie -> Down


# --- resolution reconciliation (Coinbase vs Gamma official) --------------- #
def test_resolution_uses_official_and_flags_mismatch():
    from evolver.config import Config
    from evolver.generation import resolve_window

    cfg = Config()
    strat = LoadedStrategy.create(
        "class Strategy:\n NAME='x'\n DESCRIPTION='x'\n def decide(self,ctx): return None",
        1, cfg,
    )
    strat.bankroll = cfg.starting_bankroll
    fill = simulate_fill("Up", [(0.50, 1000)], 10.0)
    fills = {strat.name: fill}

    # Coinbase said Down, Gamma official says Up -> trade scored to Up (a win).
    res = Resolution(coinbase_side="Down", official_side="Up")
    trades, resolved, mismatch = resolve_window([strat], fills, res, "w1")
    assert resolved == "Up"
    assert mismatch is True
    assert trades[0].won is True
    assert strat.bankroll > cfg.starting_bankroll  # winning payout applied


def test_resolution_no_mismatch_when_agree():
    from evolver.config import Config
    from evolver.generation import resolve_window

    cfg = Config()
    strat = LoadedStrategy.create(
        "class Strategy:\n NAME='y'\n DESCRIPTION='y'\n def decide(self,ctx): return None",
        1, cfg,
    )
    strat.bankroll = cfg.starting_bankroll
    fills = {strat.name: simulate_fill("Down", [(0.47, 1000)], 10.0)}
    res = Resolution(coinbase_side="Down", official_side="Down")
    trades, resolved, mismatch = resolve_window([strat], fills, res, "w2")
    assert mismatch is False
    assert resolved == "Down"
    assert trades[0].won is True
