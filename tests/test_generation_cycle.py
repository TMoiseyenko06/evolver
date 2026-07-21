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
