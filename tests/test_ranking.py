"""Tests for generation ranking, culling, and cumulative carry-forward."""

import pytest

from evolver.engine import score_trade, simulate_fill
from evolver.generation import rank_and_cull
from evolver.strategy import LoadedStrategy

from helpers import BODY_PASS, make_config, strategy_source


def _strat(cfg, name, gen_net, gen_trades=1, gen_wins=1, gen_be=0.5):
    s = LoadedStrategy.create(strategy_source(name, BODY_PASS), 1, cfg)
    s.bankroll = cfg.starting_bankroll + gen_net
    s.gen.net_pnl = gen_net
    s.gen.trades = gen_trades
    s.gen.wins = gen_wins
    s.gen.sum_breakeven = gen_be * gen_trades
    return s


def test_top_n_by_gen_pnl_survive(tmp_path):
    cfg = make_config(tmp_path, population_size=4, survivors=2)
    pop = [
        _strat(cfg, "a", gen_net=+5.0),
        _strat(cfg, "b", gen_net=-3.0),
        _strat(cfg, "c", gen_net=+9.0),
        _strat(cfg, "d", gen_net=+1.0),
    ]
    survivors, retirees = rank_and_cull(pop, cfg)
    assert [s.name for s in survivors] == ["c", "a"]
    assert {s.name for s in retirees} == {"b", "d"}


def test_survivors_increment_generations_survived(tmp_path):
    cfg = make_config(tmp_path, population_size=4, survivors=2)
    pop = [_strat(cfg, n, net) for n, net in [("a", 5), ("b", 4), ("c", 1), ("d", 0)]]
    survivors, retirees = rank_and_cull(pop, cfg)
    assert all(s.generations_survived == 1 for s in survivors)
    assert all(s.generations_survived == 0 for s in retirees)


def test_tiebreak_by_hitpct_minus_breakeven(tmp_path):
    cfg = make_config(tmp_path, population_size=2, survivors=1)
    # Equal net P&L; 'good_be' has a better hit%-minus-breakeven tiebreak.
    a = _strat(cfg, "high_be", gen_net=2.0, gen_trades=2, gen_wins=1, gen_be=0.60)
    b = _strat(cfg, "good_be", gen_net=2.0, gen_trades=2, gen_wins=2, gen_be=0.52)
    survivors, _ = rank_and_cull([a, b], cfg)
    assert survivors[0].name == "good_be"


def test_auto_retired_always_sinks_to_bottom(tmp_path):
    cfg = make_config(tmp_path, population_size=3, survivors=2)
    good = _strat(cfg, "good", gen_net=+8.0)
    crashy = _strat(cfg, "crashy", gen_net=+100.0)  # would top the board...
    crashy.retire("auto-retired after 3 failures (boom)")  # ...but it crashed
    ok = _strat(cfg, "ok", gen_net=+1.0)
    survivors, retirees = rank_and_cull([good, crashy, ok], cfg)
    assert crashy.name in {s.name for s in retirees}
    assert {s.name for s in survivors} == {"good", "ok"}


def test_retirees_get_reason(tmp_path):
    cfg = make_config(tmp_path, population_size=3, survivors=1)
    pop = [_strat(cfg, n, net) for n, net in [("a", 5), ("b", 4), ("c", 1)]]
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
