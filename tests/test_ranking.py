"""Tests for generation ranking, culling, and cumulative carry-forward."""

import pytest

from evolver.engine import score_trade, simulate_fill
from evolver.generation import rank_and_cull
from evolver.strategy import LoadedStrategy

from helpers import BODY_PASS, make_config, strategy_source


def _strat(cfg, name, life_net, life_trades=10, life_wins=6, life_be=0.5, life_pnl_sq=None):
    """Build a strategy with given LIFETIME stats (survival now ranks on lifetime).

    ``life_pnl_sq`` defaults to the zero-variance case (all trades = mean P&L), which
    gives a clean, low-variance score; pass a larger value to simulate a
    high-variance (longshot) strategy with the same total P&L.
    """
    s = LoadedStrategy.create(strategy_source(name, BODY_PASS), 1, cfg)
    s.bankroll = cfg.starting_bankroll + life_net
    s.lifetime.net_pnl = life_net
    s.lifetime.trades = life_trades
    s.lifetime.wins = life_wins
    s.lifetime.sum_breakeven = life_be * life_trades
    mean = life_net / life_trades if life_trades else 0.0
    s.lifetime.sum_pnl_sq = life_pnl_sq if life_pnl_sq is not None else life_trades * mean ** 2
    return s


def test_top_n_by_risk_adjusted_survive(tmp_path):
    cfg = make_config(tmp_path, population_size=4, survivors=2)
    # Same trade count, low variance: higher total P&L => higher score.
    pop = [
        _strat(cfg, "a", life_net=+50.0),
        _strat(cfg, "b", life_net=-30.0),
        _strat(cfg, "c", life_net=+90.0),
        _strat(cfg, "d", life_net=+10.0),
    ]
    survivors, retirees = rank_and_cull(pop, cfg)
    assert [s.name for s in survivors] == ["c", "a"]
    assert {s.name for s in retirees} == {"b", "d"}


def test_steady_beats_lucky_longshot_despite_lower_pnl(tmp_path):
    # The whole point of risk-adjustment: a consistent earner should outrank a
    # higher-P&L longshot strategy whose profit came from a few volatile jackpots.
    cfg = make_config(tmp_path, population_size=2, survivors=1)
    steady = _strat(cfg, "steady", life_net=+30.0, life_trades=30)  # +$1/trade, ~0 variance
    # +84 over 4 trades: one +$114 jackpot, three -$10 losses => high variance.
    longshot = _strat(cfg, "longshot", life_net=+84.0, life_trades=4,
                      life_pnl_sq=114**2 + 3 * (10**2))
    survivors, _ = rank_and_cull([steady, longshot], cfg)
    assert survivors[0].name == "steady"


def test_never_traded_strategy_sinks(tmp_path):
    # A do-nothing strategy (0 trades) must not survive over one that actually traded
    # with an edge, even in a generation where trading strategies lost money.
    cfg = make_config(tmp_path, population_size=2, survivors=1)
    idle = _strat(cfg, "idle", life_net=0.0, life_trades=0, life_wins=0)
    small_loser = _strat(cfg, "small_loser", life_net=-2.0, life_trades=20, life_wins=9)
    survivors, retirees = rank_and_cull([idle, small_loser], cfg)
    assert survivors[0].name == "small_loser"
    assert idle.name in {s.name for s in retirees}


def test_survivors_increment_generations_survived(tmp_path):
    cfg = make_config(tmp_path, population_size=4, survivors=2)
    pop = [_strat(cfg, n, net) for n, net in [("a", 50), ("b", 40), ("c", 10), ("d", 1)]]
    survivors, retirees = rank_and_cull(pop, cfg)
    assert all(s.generations_survived == 1 for s in survivors)
    assert all(s.generations_survived == 0 for s in retirees)


def test_auto_retired_always_sinks_to_bottom(tmp_path):
    cfg = make_config(tmp_path, population_size=3, survivors=2)
    good = _strat(cfg, "good", life_net=+80.0)
    crashy = _strat(cfg, "crashy", life_net=+1000.0)  # would top the board...
    crashy.retire("auto-retired after 3 failures (boom)")  # ...but it crashed
    ok = _strat(cfg, "ok", life_net=+10.0)
    survivors, retirees = rank_and_cull([good, crashy, ok], cfg)
    assert crashy.name in {s.name for s in retirees}
    assert {s.name for s in survivors} == {"good", "ok"}


def test_retirees_get_reason(tmp_path):
    cfg = make_config(tmp_path, population_size=3, survivors=1)
    pop = [_strat(cfg, n, net) for n, net in [("a", 50), ("b", 40), ("c", 10)]]
    _, retirees = rank_and_cull(pop, cfg)
    for s in retirees:
        assert s.retired
        assert s.retired_reason


def test_lifetime_stats_accumulate_across_generations(tmp_path):
    cfg = make_config(tmp_path)
    s = LoadedStrategy.create(strategy_source("carry", BODY_PASS), 1, cfg)
    s.bankroll = cfg.starting_bankroll

    # Generation 1: one winning trade.
    s.start_generation()
    fill1 = simulate_fill("Up", [(0.50, 1000)], cfg.stake)
    r1 = score_trade(s.name, "w1", fill1, "Up")
    s.bankroll += r1.net_pnl
    s.gen.record(r1)
    s.lifetime.record(r1)
    gen1_net = s.gen.net_pnl

    # Generation 2: gen stats reset, lifetime persists; one losing trade.
    s.start_generation()
    assert s.gen.trades == 0  # reset
    assert s.lifetime.trades == 1  # preserved
    fill2 = simulate_fill("Up", [(0.50, 1000)], cfg.stake)
    r2 = score_trade(s.name, "w2", fill2, "Down")
    s.bankroll += r2.net_pnl
    s.gen.record(r2)
    s.lifetime.record(r2)

    assert s.lifetime.trades == 2
    assert s.lifetime.wins == 1
    assert s.lifetime.net_pnl == pytest.approx(gen1_net + r2.net_pnl)
    assert s.bankroll == pytest.approx(cfg.starting_bankroll + r1.net_pnl + r2.net_pnl)
