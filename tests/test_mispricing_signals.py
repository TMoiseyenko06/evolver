"""Unit tests for entry_signal + the three early-exit triggers (mispricing/signals.py)."""

import pytest

from evolver.models import Fill
from mispricing.config import MispricingParams
from mispricing.model import fair_value_probs
from mispricing.signals import OpenPosition, check_exit, entry_signal


def _candles(closes):
    return [{"time": i * 60, "open": c, "close": c} for i, c in enumerate(closes)]


FLAT_CANDLES = _candles([100, 100, 100, 100, 100])  # zero realized vol -> fallback_sd


def _model_p(spot, seconds_remaining, params, open_price=100.0):
    return fair_value_probs(FLAT_CANDLES, open_price, spot, seconds_remaining, params)["Up"]


# --- entry_signal ------------------------------------------------------------- #
def test_entry_signal_fires_when_both_thresholds_cleared():
    params = MispricingParams()
    p = _model_p(spot=101.0, seconds_remaining=150, params=params)
    # Ask well below the model's fair value -> big gap AND fee-margin cleared.
    ask = p - 0.20
    books = {"Up": {"asks": [(ask, 1000)]}, "Down": {"asks": [(1 - ask - 0.01, 1000)]}}
    sig = entry_signal(FLAT_CANDLES, 100.0, 101.0, 150, books, params)
    assert sig is not None
    assert sig.side == "Up"
    assert sig.ask == pytest.approx(ask)
    assert sig.model_p == pytest.approx(p)


def test_entry_signal_does_not_fire_below_entry_gap():
    params = MispricingParams()
    p = _model_p(spot=101.0, seconds_remaining=150, params=params)
    ask = p - 0.02  # gap of only 2c, below the 8c entry_gap threshold
    books = {"Up": {"asks": [(ask, 1000)]}, "Down": {"asks": [(0.99, 1000)]}}
    assert entry_signal(FLAT_CANDLES, 100.0, 101.0, 150, books, params) is None


def test_entry_signal_gap_cleared_but_fee_margin_not():
    from polybot import fees

    # A tiny entry_gap (0.001) so it's easy to clear on its own, paired with the
    # normal fee_margin (0.05). ask=0.50 sits at the fee curve's peak (~1.56c/share),
    # and this spot/vol combo puts the model's fair value just ~2c above it — enough
    # to clear entry_gap but not enough to clear the fee-adjusted margin.
    params = MispricingParams(entry_gap=0.001, fee_margin=0.05, fallback_sd=0.02)
    ask = 0.50
    p = _model_p(spot=100.15, seconds_remaining=150, params=params)
    assert p - ask > params.entry_gap  # raw gap clears the (tiny) threshold...
    assert p - fees.breakeven(ask) <= params.fee_margin  # ...but the fee margin doesn't
    books = {"Up": {"asks": [(ask, 1000)]}, "Down": {"asks": [(0.99, 1000)]}}
    assert entry_signal(FLAT_CANDLES, 100.0, 100.15, 150, books, params) is None


def test_entry_signal_requires_enough_candles():
    params = MispricingParams(lookback_candles=5)
    short_candles = _candles([100, 101])
    books = {"Up": {"asks": [(0.10, 1000)]}, "Down": {"asks": [(0.10, 1000)]}}
    assert entry_signal(short_candles, 100.0, 101.0, 150, books, params) is None


def test_entry_signal_fires_on_down_side_for_a_down_move():
    params = MispricingParams()
    p_down = 1.0 - _model_p(spot=99.0, seconds_remaining=150, params=params)
    ask = p_down - 0.20
    books = {"Up": {"asks": [(0.99, 1000)]}, "Down": {"asks": [(ask, 1000)]}}
    sig = entry_signal(FLAT_CANDLES, 100.0, 99.0, 150, books, params)
    assert sig is not None and sig.side == "Down"


# --- check_exit: gap_closed --------------------------------------------------- #
def test_check_exit_gap_closed_fires_when_ask_catches_up():
    params = MispricingParams()
    now_spot, now_sr = 100.65, 190
    model_p_now = _model_p(spot=now_spot, seconds_remaining=now_sr, params=params)
    entry_fill = Fill(side="Up", shares=20.0, cost=10.0, avg_price=0.50, fee=0.1)
    position = OpenPosition(side="Up", fill=entry_fill, entry_model_p=0.65,
                            entry_poll_index=0, entry_seconds_remaining=200)
    # avg_price (0.50) stays below model_p_now -> not adverse. The ask has caught all
    # the way up to the model's current fair value -> gap_closed (gap <= 0).
    ask_now = model_p_now
    books = {"Up": {"asks": [(ask_now, 1000)]}}
    decision = check_exit(position, FLAT_CANDLES, 100.0, now_spot, seconds_remaining=now_sr,
                          books=books, params=params)
    assert decision is not None
    assert decision.reason == "gap_closed"


# --- check_exit: adverse_move ------------------------------------------------- #
def test_check_exit_adverse_move_fires_when_model_drops_below_entry_price():
    params = MispricingParams()
    entry_fill = Fill(side="Up", shares=20.0, cost=17.0, avg_price=0.85, fee=0.1)
    position = OpenPosition(side="Up", fill=entry_fill, entry_model_p=0.95,
                            entry_poll_index=0, entry_seconds_remaining=200)
    # Spot has reverted toward the open -> model_p_now drops well below the 0.85 paid.
    # Ask stays cheap (below model_p_now) so gap_closed does NOT also fire.
    books = {"Up": {"asks": [(0.30, 1000)]}}
    decision = check_exit(position, FLAT_CANDLES, 100.0, spot=100.0, seconds_remaining=190,
                          books=books, params=params)
    assert decision is not None
    assert decision.reason == "adverse_move"
    assert decision.model_p_now < 0.85


# --- check_exit: time_expired -------------------------------------------------- #
def test_check_exit_time_expired_fires_when_thesis_never_closes():
    params = MispricingParams(time_expired_seconds=30.0)
    entry_fill = Fill(side="Up", shares=20.0, cost=10.0, avg_price=0.50, fee=0.1)
    position = OpenPosition(side="Up", fill=entry_fill, entry_model_p=0.60,
                            entry_poll_index=0, entry_seconds_remaining=200)
    # model_p_now stays >= avg_price (not adverse) and the ask stays well below it
    # (not gap_closed) -> only time_expired can fire, once enough time has elapsed.
    books = {"Up": {"asks": [(0.20, 1000)]}}
    too_soon = check_exit(position, FLAT_CANDLES, 100.0, 100.6, seconds_remaining=185,
                          books=books, params=params)  # only 15s elapsed
    assert too_soon is None
    expired = check_exit(position, FLAT_CANDLES, 100.0, 100.6, seconds_remaining=165,
                         books=books, params=params)  # 35s elapsed
    assert expired is not None
    assert expired.reason == "time_expired"


# --- priority when multiple triggers are simultaneously true ----------------- #
def test_check_exit_priority_gap_closed_beats_adverse_move():
    params = MispricingParams()
    entry_fill = Fill(side="Up", shares=20.0, cost=17.0, avg_price=0.85, fee=0.1)
    position = OpenPosition(side="Up", fill=entry_fill, entry_model_p=0.95,
                            entry_poll_index=0, entry_seconds_remaining=200)
    # model_p_now (spot==open -> 0.5) < avg_price (0.85) -> adverse_move condition true,
    # AND ask_now (0.60) > model_p_now (0.5) -> gap_closed condition ALSO true.
    # gap_closed is checked first, so it should win.
    books = {"Up": {"asks": [(0.60, 1000)]}}
    decision = check_exit(position, FLAT_CANDLES, 100.0, spot=100.0, seconds_remaining=190,
                          books=books, params=params)
    assert decision is not None
    assert decision.reason == "gap_closed"


def test_check_exit_none_when_nothing_triggers():
    params = MispricingParams(time_expired_seconds=999.0)
    entry_fill = Fill(side="Up", shares=20.0, cost=10.0, avg_price=0.50, fee=0.1)
    position = OpenPosition(side="Up", fill=entry_fill, entry_model_p=0.60,
                            entry_poll_index=0, entry_seconds_remaining=200)
    books = {"Up": {"asks": [(0.20, 1000)]}}
    decision = check_exit(position, FLAT_CANDLES, 100.0, 100.6, seconds_remaining=195,
                          books=books, params=params)
    assert decision is None
