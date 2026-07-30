"""End-to-end backtest tests: synthetic archived WindowData, one per exit reason
plus one hold-to-resolution window, verifying with-exits vs baseline diverge exactly
as expected.
"""

import pytest

from evolver.config import Config
from evolver.engine import score_trade
from evolver.models import PollSnapshot, WindowData

from mispricing.backtest import run_backtest
from mispricing.config import MispricingParams
from mispricing.runner import run_window

CANDLES = [{"time": i * 60, "open": 100.0, "close": 100.0} for i in range(5)]
PARAMS = MispricingParams(fallback_sd=0.02)  # moderate vol -> non-saturated p, see model tests


def _cfg() -> Config:
    # Disable the fill-realism corrections for these tests so fills land exactly at
    # the displayed price/depth we construct — the corrections themselves are
    # covered by tests/test_accounting.py; here we're testing entry/exit LOGIC.
    return Config(use_cross_book_fill=False, slippage_coeff=0.0, max_slippage=None,
                 book_participation=1.0, stake=10.0)


def _snap(poll_index, seconds_remaining, spot, up_ask, down_ask, up_bid=None, down_bid=None,
         open_price=100.0, depth=1000.0) -> PollSnapshot:
    books = {
        "Up": {"asks": [(up_ask, depth)], "bids": [(up_bid, depth)] if up_bid else []},
        "Down": {"asks": [(down_ask, depth)], "bids": [(down_bid, depth)] if down_bid else []},
    }
    return PollSnapshot(poll_index=poll_index, seconds_remaining=seconds_remaining,
                        window_open_price=open_price, spot=spot, candles=CANDLES, books=books)


def _window(window_id, resolved_side, polls) -> WindowData:
    return WindowData(window_id=window_id, condition_id=f"cond_{window_id}", title=window_id,
                      start_iso="2026-01-01T00:00:00", end_iso="2026-01-01T00:05:00",
                      token_map={"Up": "tok_up", "Down": "tok_down"}, polls=polls,
                      coinbase_side=resolved_side, official_side=resolved_side,
                      resolved_side=resolved_side)


# Entry poll shared by all three windows: p0 ~= 0.5164 (from model tests), ask well
# below it clears both thresholds.
ENTRY_ASK = 0.3664


def _entry_poll():
    return _snap(0, seconds_remaining=200, spot=100.15, up_ask=ENTRY_ASK, down_ask=0.99)


# --- Window A: gap_closed (the ask rises to meet fair value -> take profit) ------ #
def window_gap_closed() -> WindowData:
    exit_poll = _snap(1, seconds_remaining=170, spot=100.4, up_ask=0.55, down_ask=0.45,
                      up_bid=0.53)  # model_p_now (~0.547) <= ask_now (0.55) -> gap_closed
    return _window("gap_closed_win", resolved_side="Up", polls=[_entry_poll(), exit_poll])


# --- Window B: adverse_move (model turns against the position -> stop loss) ------ #
def window_adverse_move() -> WindowData:
    exit_poll = _snap(1, seconds_remaining=170, spot=98.0, up_ask=0.15, down_ask=0.85,
                      up_bid=0.13)  # model_p_now (~0.276) < avg_price (0.3664) -> adverse
    return _window("adverse_move_win", resolved_side="Down", polls=[_entry_poll(), exit_poll])


# --- Window C: neither trigger fires -> falls through to hold-to-resolution ------ #
def window_hold_to_resolution() -> WindowData:
    later_poll = _snap(1, seconds_remaining=170, spot=100.3, up_ask=0.20, down_ask=0.80)
    # model_p_now (~0.536) > avg_price (0.3664) -> not adverse; ask_now (0.20) < model_p_now
    # -> gap not closed; only 30s elapsed -> under the 60s default time_expired_seconds.
    return _window("hold_win", resolved_side="Up", polls=[_entry_poll(), later_poll])


# --- per-window unit checks (exact trigger + direction) -------------------------- #
def test_window_gap_closed_locks_a_profit():
    config = _cfg()
    outcome = run_window(window_gap_closed().polls, "mispricing", "w", config, PARAMS)
    assert outcome.entered and outcome.exit_reason == "gap_closed"
    assert outcome.exit_trade.net_pnl > 0  # sold higher than bought


def test_window_adverse_move_cuts_a_smaller_loss_than_holding():
    config = _cfg()
    w = window_adverse_move()
    outcome = run_window(w.polls, "mispricing", "w", config, PARAMS)
    assert outcome.entered and outcome.exit_reason == "adverse_move"
    assert outcome.exit_trade.net_pnl < 0  # still a loss...

    # ...but a SMALLER loss than holding this same entry to its (losing) resolution.
    held_trade = score_trade("mispricing", "w", outcome.entry_fill, w.resolved_side)
    assert held_trade.won is False
    assert outcome.exit_trade.net_pnl > held_trade.net_pnl


def test_window_hold_falls_through_when_nothing_triggers():
    config = _cfg()
    outcome = run_window(window_hold_to_resolution().polls, "mispricing", "w", config, PARAMS)
    assert outcome.entered
    assert outcome.exit_trade is None
    assert outcome.pending_resolution is True


# --- end-to-end aggregate backtest ------------------------------------------------ #
def test_run_backtest_aggregates_all_three_windows():
    config = _cfg()
    windows = [window_gap_closed(), window_adverse_move(), window_hold_to_resolution()]
    report = run_backtest(windows, config, PARAMS)

    assert report.exit_counts.gap_closed == 1
    assert report.exit_counts.adverse_move == 1
    assert report.exit_counts.held_to_resolution == 1
    assert report.exit_counts.never_entered == 0

    # All three windows entered in both passes (entries are deterministic and
    # identical regardless of exits_enabled).
    assert report.with_exits.trades == 3
    assert report.baseline_no_exits.trades == 3

    # Exits are a RISK trade-off, not a free lunch: this scenario is deliberately
    # constructed so 2 of 3 windows would win big if held to resolution (gap_closed_win
    # and hold_win both resolve Up while holding an Up position), so the baseline's
    # total P&L is actually HIGHER here — gap_closed traded away upside for certainty,
    # exactly as discussed (locking in a smaller sure profit instead of riding a full
    # win). What exits reliably do is reduce variance: the adverse_move window lost
    # less than a full stake instead of the whole thing, and gap_closed's win was
    # capped rather than being fully exposed to the coin-flip outcome. That shows up
    # as a materially smaller P&L standard deviation, which is the real, defensible
    # claim to test — not "exits always make more money."
    assert report.with_exits.pnl_std < report.baseline_no_exits.pnl_std
    assert report.pnl_delta == pytest.approx(report.with_exits.net_pnl - report.baseline_no_exits.net_pnl)


def test_run_backtest_skips_unresolved_windows():
    config = _cfg()
    unresolved = _window("no_res", resolved_side=None, polls=[_entry_poll()])
    unresolved.resolved_side = None
    report = run_backtest([unresolved], config, PARAMS)
    assert report.with_exits.trades == 0
    assert report.baseline_no_exits.trades == 0
    assert report.per_window == []
