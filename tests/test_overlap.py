"""Overlap test: with background resolution, the trader starts the next window
before the previous one finishes resolving (so no windows are missed)."""

import threading
import time

import pytest

from evolver.generation import run_generation
from evolver.store import Store
from evolver.strategy import LoadedStrategy

from helpers import (
    BODY_ALWAYS_UP,
    MockMarket,
    default_window_specs,
    make_config,
    strategy_source,
)


class RecordingMarket(MockMarket):
    """MockMarket that records event order and makes resolve() take time."""

    def __init__(self, specs, events, lock, resolve_delay):
        super().__init__(specs)
        self._events = events
        self._elock = lock
        self._delay = resolve_delay

    def next_window(self):
        handle = super().next_window()
        with self._elock:
            self._events.append(("next", handle.window_id))
        return handle

    def resolve(self, handle):
        time.sleep(self._delay)  # simulate settlement lag on the worker thread
        with self._elock:
            self._events.append(("res_done", handle.window_id))
        return super().resolve(handle)


def _run(tmp_path, overlap):
    cfg = make_config(tmp_path, population_size=1, survivors=1, windows_per_generation=4)
    cfg.overlap_resolution = overlap
    cfg.live_window_reports = False
    store = Store(cfg)
    pop = [LoadedStrategy.create(strategy_source("probe", BODY_ALWAYS_UP), 1, cfg)]
    events, lock = [], threading.Lock()
    market = RecordingMarket(default_window_specs(), events, lock, resolve_delay=0.15)
    run_generation(pop, market, store, cfg, 1)
    return cfg, store, pop[0], events


def test_overlap_fetches_next_window_before_prior_resolves(tmp_path):
    cfg, store, strat, events = _run(tmp_path, overlap=True)

    # Before the first window finishes resolving, more than one window was fetched
    # (i.e. the trader moved on instead of blocking) -> overlap.
    first_res_done = next(i for i, (k, _) in enumerate(events) if k == "res_done")
    nexts_before = [w for (k, w) in events[:first_res_done] if k == "next"]
    assert len(nexts_before) >= 2

    # All 4 windows still traded, resolved, persisted, and scored.
    assert store.conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] == 4
    assert strat.gen.trades == 4
    assert strat.bankroll != cfg.starting_bankroll


class SlowFirstMarket(MockMarket):
    """First window resolves slowly; the rest are fast."""

    def __init__(self, specs, events, lock):
        super().__init__(specs)
        self._events = events
        self._elock = lock

    def resolve(self, handle):
        time.sleep(0.5 if handle.window_id == "win_up_1" else 0.05)
        with self._elock:
            self._events.append(handle.window_id)
        return super().resolve(handle)


def test_slow_market_does_not_block_the_others(tmp_path):
    cfg = make_config(tmp_path, population_size=1, survivors=1, windows_per_generation=4)
    cfg.overlap_resolution = True
    cfg.live_window_reports = False
    cfg.resolution_workers = 4
    store = Store(cfg)
    pop = [LoadedStrategy.create(strategy_source("probe", BODY_ALWAYS_UP), 1, cfg)]
    events, lock = [], threading.Lock()
    run_generation(pop, SlowFirstMarket(default_window_specs(), events, lock), store, cfg, 1)

    # The slow window (win_up_1) must NOT be the first to finish resolving —
    # the pool resolved the fast ones while it waited (no head-of-line block).
    assert events[0] != "win_up_1"
    assert store.conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] == 4


def test_sequential_mode_waits_for_each_resolution(tmp_path):
    cfg, store, strat, events = _run(tmp_path, overlap=False)

    # In sequential mode only the first window is fetched before it resolves.
    first_res_done = next(i for i, (k, _) in enumerate(events) if k == "res_done")
    nexts_before = [w for (k, w) in events[:first_res_done] if k == "next"]
    assert len(nexts_before) == 1
    assert store.conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] == 4
