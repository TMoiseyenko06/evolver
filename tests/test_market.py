"""Tests for LiveMarket window selection (dedup: one trade per distinct window)."""

import datetime as dt

from evolver.config import Config
from evolver.market import LiveMarket
from polybot.polymarket import Window


def _win(cond, start, minutes=5):
    return Window(
        condition_id=cond, title=f"Bitcoin Up or Down ({cond})",
        start=start, end=start + dt.timedelta(minutes=minutes), token_map={},
    )


def test_next_unreturned_skips_already_returned_windows():
    m = LiveMarket(Config(use_websocket=False))
    now = dt.datetime.now(dt.timezone.utc)
    a = _win("A", now)
    b = _win("B", now + dt.timedelta(minutes=5))

    # Earliest fresh window is A.
    picked = m._next_unreturned([a, b], now)
    assert picked.condition_id == "A"

    # After A is handed out, it must not be returned again — B is next.
    m._returned_windows.add("A")
    assert m._next_unreturned([a, b], now).condition_id == "B"

    # With both returned, nothing left (caller keeps polling for a new window).
    m._returned_windows.add("B")
    assert m._next_unreturned([a, b], now) is None


def test_next_unreturned_ignores_ended_windows():
    m = LiveMarket(Config(use_websocket=False))
    now = dt.datetime.now(dt.timezone.utc)
    past = _win("OLD", now - dt.timedelta(minutes=10))  # already ended
    live = _win("NEW", now)
    assert m._next_unreturned([past, live], now).condition_id == "NEW"
