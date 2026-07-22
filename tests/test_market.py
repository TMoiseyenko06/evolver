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


# --- official (authoritative) resolution ---------------------------------- #
def test_official_outcome_reads_gamma_outcome_prices(monkeypatch):
    import polybot.polymarket as pm

    # "Down Won": outcomePrices parallel to outcomes, Down at 1.
    row = {"conditionId": "X", "outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]'}
    monkeypatch.setattr(pm, "_get", lambda *a, **k: [row])
    assert pm.official_outcome("X") == "Down"


def test_official_outcome_up_won(monkeypatch):
    import polybot.polymarket as pm

    row = {"conditionId": "X", "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}
    monkeypatch.setattr(pm, "_get", lambda *a, **k: [row])
    assert pm.official_outcome("X") == "Up"


def test_official_outcome_none_until_resolved(monkeypatch):
    import polybot.polymarket as pm

    row = {"conditionId": "X", "outcomes": '["Up", "Down"]', "outcomePrices": '["0.5", "0.5"]'}
    monkeypatch.setattr(pm, "_get", lambda *a, **k: [row])
    assert pm.official_outcome("X") is None


def test_find_market_filters_by_condition_id():
    import polybot.polymarket as pm

    rows = [{"conditionId": "A", "outcomePrices": '["1","0"]'},
            {"conditionId": "B", "outcomePrices": '["0","1"]'}]
    assert pm._find_market(rows, "B")["conditionId"] == "B"
    assert pm._find_market([], "B") is None
