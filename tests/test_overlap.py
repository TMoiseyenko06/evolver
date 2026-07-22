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


def test_sequential_mode_waits_for_each_resolution(tmp_path):
    cfg, store, strat, events = _run(tmp_path, overlap=False)

    # In sequential mode only the first window is fetched before it resolves.
    first_res_done = next(i for i, (k, _) in enumerate(events) if k == "res_done")
    nexts_before = [w for (k, w) in events[:first_res_done] if k == "next"]
    assert len(nexts_before) == 1
    assert store.conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] == 4
