"""End-to-end offline generation cycle with a mocked OpenRouter and mocked market.

Exercises the whole loop: seed -> forward-test -> rank/cull -> carry-forward ->
evolve, plus persistence, deterministic replay, decision logging, and
near-duplicate rejection.
"""

import json

import pytest

from evolver.config import Config
from evolver.generation import (
    _is_duplicate,
    generate_batch,
    seed_population,
)
from evolver.replay import action_vector, agreement, replay_strategy
from evolver.runner import run_loop
from evolver.store import Store

from helpers import (
    BODY_ALWAYS_DOWN,
    BODY_ALWAYS_UP,
    BODY_CRASH,
    BODY_LATE_UP,
    BODY_MOMENTUM,
    BODY_PASS,
    FakeClient,
    MockMarket,
    default_window_specs,
    make_config,
    make_fake_sources,
    strategy_source,
)

SEED = [
    ("momentum", BODY_MOMENTUM, "novel"),
    ("always_up", BODY_ALWAYS_UP, "novel"),
    ("late_up", BODY_LATE_UP, "novel"),
    ("passer", BODY_PASS, "novel"),
    ("always_down", BODY_ALWAYS_DOWN, "novel"),
    ("crasher", BODY_CRASH, "novel"),
]
EVO_GEN2 = [
    ("e2_down", BODY_ALWAYS_DOWN, "always_down"),
    ("e2_pass", BODY_PASS, "novel"),
    ("e2_late_down", 'if ctx.seconds_remaining <= 60:\n    return {"side": "Down"}\nreturn None', "novel"),
]
EVO_GEN3 = [
    ("e3_down", BODY_ALWAYS_DOWN, "novel"),
    ("e3_pass", BODY_PASS, "novel"),
    ("e3_mom", BODY_MOMENTUM, "momentum"),
]


def _client():
    return FakeClient.from_source_batches(
        [make_fake_sources(SEED), make_fake_sources(EVO_GEN2), make_fake_sources(EVO_GEN3)]
    )


def _run_two_generations(tmp_path):
    cfg = make_config(tmp_path, population_size=6, survivors=3, windows_per_generation=4)
    cfg.duplicate_threshold = 1.0  # disable near-dup rejection for this cycle test
    store = Store(cfg)
    market = MockMarket(default_window_specs())
    client = _client()
    run_loop(market, client, store, cfg, max_generations=2)
    return cfg, store, client


# --------------------------------------------------------------------------- #
def test_full_cycle_runs_and_persists(tmp_path):
    cfg, store, client = _run_two_generations(tmp_path)

    # Seed + two evolution calls == 3 OpenRouter calls.
    assert len(client.calls) == 3
    # Prompts archived (seed + 2 evolution).
    prompts = store.conn.execute("SELECT kind FROM prompts ORDER BY id").fetchall()
    assert [p["kind"] for p in prompts] == ["seed", "evolution", "evolution"]

    # Every strategy's source persisted to disk and DB.
    files = {p.name for p in cfg.strategies_dir.glob("*.py")}
    assert "gen1_momentum.py" in files
    for name in ("momentum", "always_up", "passer", "crasher"):
        assert store.get_strategy_row(name) is not None

    # 4 resolved windows per generation, both generations logged.
    n_windows = store.conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"]
    assert n_windows == 8

    # Per-generation reports written.
    assert (cfg.runs_dir / "gen1_report.md").exists()
    assert (cfg.runs_dir / "gen2_report.md").exists()


def test_ranking_and_carry_forward(tmp_path):
    cfg, store, client = _run_two_generations(tmp_path)

    # momentum picks the winning side every window -> lifetime leader.
    board = store.leaderboard_rows()
    assert board[0]["name"] == "momentum"
    momentum = next(r for r in board if r["name"] == "momentum")
    assert momentum["alive"] is True
    # Survived both generations.
    assert momentum["generations_survived"] == 2
    # Traded 4 windows x 2 generations.
    assert momentum["stats"].trades == 8
    assert momentum["stats"].net_pnl > 0
    # Bankroll carried: 500 + lifetime net.
    assert momentum["bankroll"] == pytest.approx(cfg.starting_bankroll + momentum["stats"].net_pnl)


def test_always_down_retired_and_crasher_auto_retired(tmp_path):
    cfg, store, client = _run_two_generations(tmp_path)

    crasher = store.get_state_row("crasher")
    assert crasher["alive"] == 0
    assert "auto-retired" in (crasher["retired_reason"] or "")

    always_down = store.get_state_row("always_down")
    assert always_down["alive"] == 0


def test_decisions_including_passes_are_logged(tmp_path):
    cfg, store, client = _run_two_generations(tmp_path)
    # passer never trades -> its decisions are all NULL actions.
    rows = store.conn.execute(
        "SELECT action_json FROM decisions WHERE strategy_name='passer'"
    ).fetchall()
    assert rows, "expected logged pass decisions"
    assert all(r["action_json"] is None for r in rows)

    # momentum entered -> at least one non-null action logged.
    entered = store.conn.execute(
        "SELECT COUNT(*) c FROM decisions WHERE strategy_name='momentum' AND action_json IS NOT NULL"
    ).fetchone()["c"]
    assert entered >= 1


def test_replay_is_deterministic(tmp_path):
    cfg, store, client = _run_two_generations(tmp_path)
    row = store.get_strategy_row("momentum")
    generations = store.generations_for_strategy("momentum")
    windows = store.windows_for_generations(generations)
    result = replay_strategy(row["source"], windows, cfg)

    state = store.get_state_row("momentum")
    recorded = json.loads(state["lifetime_json"])
    # Re-scoring the archived windows reproduces the recorded lifetime P&L exactly.
    assert result.stats.net_pnl == pytest.approx(recorded["net_pnl"])
    assert result.stats.trades == recorded["trades"]

    # And replaying twice yields identical numbers.
    again = replay_strategy(row["source"], windows, cfg)
    assert again.stats.net_pnl == pytest.approx(result.stats.net_pnl)


def test_reset_stats_keeps_strategies_clears_pnl(tmp_path):
    import json as _json

    cfg = make_config(tmp_path, population_size=2, survivors=1, windows_per_generation=2)
    store = Store(cfg)
    from evolver.generation import run_generation
    from evolver.strategy import LoadedStrategy

    pop = [
        LoadedStrategy.create(strategy_source("mom", BODY_MOMENTUM), 1, cfg),
        LoadedStrategy.create(strategy_source("dn", BODY_ALWAYS_DOWN), 1, cfg),
    ]
    for s in pop:
        store.save_strategy(s, None)
    run_generation(pop, MockMarket(default_window_specs()), store, cfg, 1)
    for s in pop:
        store.save_state(s, alive=True, generation=1)
        store.save_gen_stats(1, s, survived=True, rank=1)
    assert store.conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] > 0

    store.reset_stats()

    # Strategies (code) survive the reset.
    assert store.get_strategy_row("mom") is not None and store.get_strategy_row("dn") is not None
    # History wiped.
    for tbl in ("windows", "trades", "gen_stats"):
        assert store.conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()["c"] == 0
    # State zeroed but the strategy is still alive.
    st = store.get_state_row("mom")
    assert st["alive"] == 1
    assert st["bankroll"] == cfg.starting_bankroll
    assert st["generations_survived"] == 0
    assert _json.loads(st["lifetime_json"])["net_pnl"] == 0.0


def test_live_window_status_board_is_printed(tmp_path, capsys):
    cfg = make_config(tmp_path, population_size=3, survivors=1, windows_per_generation=4)
    store = Store(cfg)
    from evolver.generation import run_generation
    from evolver.strategy import LoadedStrategy

    pop = [
        LoadedStrategy.create(strategy_source("momentum", BODY_MOMENTUM), 1, cfg),
        LoadedStrategy.create(strategy_source("always_down", BODY_ALWAYS_DOWN), 1, cfg),
        LoadedStrategy.create(strategy_source("passer", BODY_PASS), 1, cfg),
    ]
    run_generation(pop, MockMarket(default_window_specs()), store, cfg, 1)
    out = capsys.readouterr().out

    # One board per resolved window, each showing the resolution and every strategy.
    assert out.count("window 1/4") == 1
    assert "window 4/4" in out
    assert "resolved Up" in out and "resolved Down" in out
    assert "bankroll" in out and "life P&L" in out
    for name in ("momentum", "always_down", "passer"):
        assert name in out
    assert "WIN" in out and "LOSS" in out and "pass" in out


def test_live_reports_can_be_disabled(tmp_path, capsys):
    cfg = make_config(tmp_path, population_size=2, survivors=1, windows_per_generation=2)
    cfg.live_window_reports = False
    store = Store(cfg)
    from evolver.generation import run_generation
    from evolver.strategy import LoadedStrategy

    pop = [LoadedStrategy.create(strategy_source("p", BODY_PASS), 1, cfg)]
    run_generation(pop, MockMarket(default_window_specs()), store, cfg, 1)
    assert "bankroll" not in capsys.readouterr().out


def test_window_log_has_book_snapshots_and_hash(tmp_path):
    cfg, store, client = _run_two_generations(tmp_path)
    row = store.conn.execute("SELECT data_json, candle_state_hash FROM windows LIMIT 1").fetchone()
    assert row["candle_state_hash"]
    data = json.loads(row["data_json"])
    assert data["polls"], "polls should be logged"
    assert "Up" in data["polls"][0]["books"] and "Down" in data["polls"][0]["books"]


# --------------------------------------------------------------------------- #
# Diversity / near-duplicate detection
# --------------------------------------------------------------------------- #
def test_agreement_and_duplicate_detection(tmp_path):
    cfg = make_config(tmp_path)
    market = MockMarket(default_window_specs())
    # Build a couple of archived windows by running one short generation.
    store = Store(cfg)
    from evolver.generation import run_generation
    from evolver.strategy import LoadedStrategy

    probe = LoadedStrategy.create(strategy_source("probe", BODY_PASS), 1, cfg)
    run_generation([probe], market, store, cfg, generation=1)
    windows = store.recent_windows(10)
    assert windows

    up_a = action_vector(strategy_source("ua", BODY_ALWAYS_UP), windows, cfg)
    up_b = action_vector(strategy_source("ub", BODY_ALWAYS_UP), windows, cfg)
    down = action_vector(strategy_source("d", BODY_ALWAYS_DOWN), windows, cfg)

    assert agreement(up_a, up_b) == 1.0  # identical decisions
    assert agreement(up_a, down) == 0.0  # opposite decisions


def test_agreement_ignores_mutual_pass_windows():
    # Two strategies that both pass a window aren't duplicates for it — only the
    # windows where at least one TRADES count. This catches thematic clones that
    # trade the same handful of windows the same way but pass most others.
    a = ["Up", None, None, "Down", None]
    b = ["Up", None, None, "Down", None]
    # Only 2 active windows (indices 0 and 3), both agree -> 1.0, not diluted by
    # the three mutual-pass windows.
    assert agreement(a, b) == 1.0

    c = ["Up", None, "Up", None]
    d = [None, None, "Down", None]
    # Active windows: index 0 (Up vs pass -> differ) and index 2 (Up vs Down ->
    # differ). 0/2 agree.
    assert agreement(c, d) == 0.0

    # All mutual passes -> no active windows -> 0.0 (not a duplicate).
    assert agreement([None, None], [None, None]) == 0.0


def test_evolve_refills_when_a_reply_underdelivers(tmp_path):
    # A single OpenRouter reply may return fewer valid blocks than requested; evolve
    # must keep breeding until the population is back up to population_size.
    from evolver.generation import evolve
    from evolver.strategy import LoadedStrategy

    cfg = make_config(tmp_path, population_size=5, survivors=2)
    cfg.duplicate_threshold = 1.0
    store = Store(cfg)
    survivors = [
        LoadedStrategy.create(strategy_source("s1", BODY_ALWAYS_UP), 1, cfg),
        LoadedStrategy.create(strategy_source("s2", BODY_ALWAYS_DOWN), 1, cfg),
    ]
    # First reply delivers only 1 of the 3 needed; the second supplies the rest.
    client = FakeClient.from_source_batches([
        make_fake_sources([("n1", BODY_PASS, "novel")]),
        make_fake_sources([("n2", BODY_MOMENTUM, "novel"), ("n3", BODY_LATE_UP, "novel")]),
    ])
    replacements = evolve(survivors, [], client, store, cfg, generation=2)
    assert len(replacements) == 3
    assert len(survivors) + len(replacements) == cfg.population_size
    assert len(client.calls) == 2  # a second breeding round was needed to fill
    store.close()


def test_resume_breeds_up_to_grown_population(tmp_path):
    # Operator raises population_size and restarts: the resumed run should top the
    # population back up to the new target instead of waiting a full generation.
    cfg = make_config(tmp_path, population_size=3, survivors=3, windows_per_generation=2)
    cfg.duplicate_threshold = 1.0
    store = Store(cfg)
    market = MockMarket(default_window_specs())
    seed_client = FakeClient.from_source_batches([make_fake_sources([
        ("a", BODY_ALWAYS_UP, "novel"),
        ("b", BODY_ALWAYS_DOWN, "novel"),
        ("c", BODY_PASS, "novel"),
    ])])
    run_loop(market, seed_client, store, cfg, max_generations=1)
    store.close()

    # Restart with a larger target.
    cfg2 = make_config(tmp_path, population_size=6, survivors=6, windows_per_generation=2)
    cfg2.duplicate_threshold = 1.0
    store2 = Store(cfg2)
    assert len(store2.load_alive_strategies(cfg2)) == 3  # only the 3 seeded so far
    topup_client = FakeClient.from_source_batches([make_fake_sources([
        ("d", BODY_MOMENTUM, "novel"),
        ("e", BODY_LATE_UP, "novel"),
        ("f", BODY_PASS, "novel"),
    ])])
    run_loop(MockMarket(default_window_specs()), topup_client, store2, cfg2, max_generations=1)
    assert len(store2.load_alive_strategies(cfg2)) == 6  # topped up to the new target
    store2.close()


def test_duplicate_is_rejected_and_replaced(tmp_path):
    cfg = make_config(tmp_path, population_size=2, survivors=1)
    cfg.duplicate_threshold = 0.9
    store = Store(cfg)
    market = MockMarket(default_window_specs())

    from evolver.generation import run_generation
    from evolver.strategy import LoadedStrategy

    survivor = LoadedStrategy.create(strategy_source("survivor_up", BODY_ALWAYS_UP), 1, cfg)
    run_generation([survivor], market, store, cfg, generation=1)

    # Evolution reply: first block duplicates the survivor (also always Up); the
    # replacement request returns a genuinely different strategy (always Down).
    dup = make_fake_sources([("dup_up", BODY_ALWAYS_UP, "survivor_up")])
    replacement = make_fake_sources([("novel_down", BODY_ALWAYS_DOWN, "novel")])
    client = FakeClient(responses=[
        "\n\n".join(f"```python\n{s}\n```" for s in dup),
        "\n\n".join(f"```python\n{s}\n```" for s in replacement),
    ])

    taken = set(store.all_strategy_sources().keys())
    new = generate_batch(
        client, store, cfg, generation=2, n_needed=1,
        system="sys", user="usr", kind="evolution",
        existing=[survivor], taken_names=taken,
    )
    # The duplicate was rejected; the distinct replacement was accepted.
    assert len(new) == 1
    assert new[0].name == "novel_down"
    # Two OpenRouter calls: the batch + the replacement request.
    assert len(client.calls) == 2


def test_system_prompt_emphasizes_volume():
    from evolver.prompts import SYSTEM_PROMPT, seed_prompt

    assert "volume" in SYSTEM_PROMPT.lower()
    assert "high" in SYSTEM_PROMPT.lower() and "low" in SYSTEM_PROMPT.lower()
    assert "volume" in seed_prompt(20).lower()


def test_default_population_is_20_keep_10():
    from evolver.config import Config

    c = Config()
    assert c.population_size == 20 and c.survivors == 10
