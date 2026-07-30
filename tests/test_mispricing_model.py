"""Unit tests for the pure fair-value model (mispricing/model.py)."""

import pytest

from mispricing.config import MispricingParams
from mispricing.model import fair_value_probs, one_min_returns, realized_sd, sqrt_time_sigma


def _candles(closes):
    return [{"time": i * 60, "open": c, "close": c} for i, c in enumerate(closes)]


# --- one_min_returns -------------------------------------------------------- #
def test_one_min_returns_too_few_candles():
    assert one_min_returns(_candles([100, 101, 100]), lookback_candles=5) == []


def test_one_min_returns_correct_values():
    rets = one_min_returns(_candles([100, 101, 100, 101, 100]), lookback_candles=5)
    assert len(rets) == 4
    assert rets[0] == pytest.approx(0.01)
    assert rets[1] == pytest.approx(-1 / 101)


def test_one_min_returns_skips_nonpositive_close():
    rets = one_min_returns(_candles([0, 101, 100, 101, 100]), lookback_candles=5)
    assert len(rets) == 3  # the (0 -> 101) pair is skipped


# --- realized_sd -------------------------------------------------------------#
def test_realized_sd_fallback_on_few_returns():
    assert realized_sd([0.01], fallback_sd=0.0005) == 0.0005
    assert realized_sd([], fallback_sd=0.0005) == 0.0005


def test_realized_sd_fallback_on_zero_variance():
    assert realized_sd([0.01, 0.01, 0.01], fallback_sd=0.0005) == 0.0005


def test_realized_sd_uses_pstdev_when_available():
    import statistics
    rets = [0.01, -0.02, 0.015, -0.005]
    assert realized_sd(rets, fallback_sd=0.0005) == pytest.approx(statistics.pstdev(rets))


# --- sqrt_time_sigma ---------------------------------------------------------#
def test_sqrt_time_sigma_scaling():
    sd = 0.01
    sigma_1min = sqrt_time_sigma(sd, seconds_remaining=60)
    sigma_4min = sqrt_time_sigma(sd, seconds_remaining=240)
    assert sigma_1min == pytest.approx(sd)
    # 4x the time -> sqrt(4)=2x the sigma.
    assert sigma_4min == pytest.approx(sigma_1min * 2)


def test_sqrt_time_sigma_floors_seconds_remaining_at_one():
    # seconds_remaining <= 0 shouldn't explode/zero out sigma unexpectedly.
    sd = 0.01
    sigma = sqrt_time_sigma(sd, seconds_remaining=0)
    assert sigma > 0
    assert sigma == pytest.approx(sd * (1 / 60) ** 0.5)


# --- fair_value_probs ---------------------------------------------------------#
def test_fair_value_probs_bounded_and_sums_to_one():
    params = MispricingParams()
    candles = _candles([100, 100.2, 99.9, 100.1, 100.0])
    probs = fair_value_probs(candles, window_open_price=100.0, spot=101.0,
                             seconds_remaining=150, params=params)
    assert 0.0 <= probs["Up"] <= 1.0
    assert probs["Down"] == pytest.approx(1.0 - probs["Up"])


def test_fair_value_probs_no_move_is_a_coin_flip():
    params = MispricingParams()
    candles = _candles([100, 100.2, 99.9, 100.1, 100.0])
    probs = fair_value_probs(candles, window_open_price=100.0, spot=100.0,
                             seconds_remaining=150, params=params)
    assert probs["Up"] == pytest.approx(0.5)


def test_fair_value_probs_monotonic_in_spot():
    params = MispricingParams()
    candles = _candles([100, 100, 100, 100, 100])  # zero realized vol -> fallback_sd
    p_small_move = fair_value_probs(candles, 100.0, 100.2, 150, params)["Up"]
    p_big_move = fair_value_probs(candles, 100.0, 101.0, 150, params)["Up"]
    assert p_big_move > p_small_move > 0.5


def test_fair_value_probs_more_time_left_flattens_confidence():
    # Same move, more time/vol remaining -> larger sigma -> closer to a coin flip.
    params = MispricingParams()
    candles = _candles([100, 100, 100, 100, 100])
    p_soon = fair_value_probs(candles, 100.0, 100.3, seconds_remaining=30, params=params)["Up"]
    p_later = fair_value_probs(candles, 100.0, 100.3, seconds_remaining=280, params=params)["Up"]
    assert 0.5 < p_later < p_soon
