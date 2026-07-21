"""Offline tests for the WebSocket streaming layer (pure book logic + selection).

No live socket is opened: we drive the parsing/mutation helpers and the stream
objects' message handlers directly, exactly as the socket callback would.
"""

import time

import pytest

import polybot.streaming as streaming
from polybot.streaming import (
    BookState,
    OrderBookStream,
    SpotStream,
    _proxy_from_env,
    apply_book_snapshot,
    apply_price_change,
    sorted_book,
)


def _book_msg(token="tokA"):
    return {
        "event_type": "book",
        "asset_id": token,
        "asks": [{"price": "0.55", "size": "100"}, {"price": "0.54", "size": "50"}],
        "bids": [{"price": "0.45", "size": "200"}, {"price": "0.44", "size": "10"}],
        "hash": "h1",
    }


# --- pure mutation logic -------------------------------------------------- #
def test_snapshot_populates_sorted_book():
    state = apply_book_snapshot(BookState(), _book_msg())
    book = sorted_book(state)
    assert book["asks"] == [(0.54, 50.0), (0.55, 100.0)]  # ascending
    assert book["bids"] == [(0.45, 200.0), (0.44, 10.0)]  # descending
    assert state.has_snapshot and not state.stale


def test_price_change_add_update_remove():
    state = apply_book_snapshot(BookState(), _book_msg())
    apply_price_change(state, {
        "asset_id": "tokA",
        "changes": [
            {"price": "0.54", "size": "0", "side": "SELL"},    # remove ask level
            {"price": "0.56", "size": "30", "side": "SELL"},   # add ask level
            {"price": "0.45", "size": "250", "side": "BUY"},   # update bid level
        ],
    })
    book = sorted_book(state)
    assert book["asks"] == [(0.55, 100.0), (0.56, 30.0)]
    assert book["bids"] == [(0.45, 250.0), (0.44, 10.0)]


def test_price_change_before_snapshot_is_ignored():
    state = BookState()
    apply_price_change(state, {"changes": [{"price": "0.5", "size": "10", "side": "SELL"}]})
    assert sorted_book(state) == {"asks": [], "bids": []}
    assert not state.has_snapshot


def test_new_snapshot_resyncs_after_deltas():
    state = apply_book_snapshot(BookState(), _book_msg())
    apply_price_change(state, {"changes": [{"price": "0.99", "size": "5", "side": "SELL"}]})
    # A fresh snapshot fully replaces prior (possibly desynced) state.
    apply_book_snapshot(state, _book_msg())
    assert 0.99 not in state.asks
    assert sorted_book(state)["asks"] == [(0.54, 50.0), (0.55, 100.0)]


# --- OrderBookStream message handling + freshness ------------------------- #
def test_orderbook_stream_serves_fresh_book_then_falls_stale():
    s = OrderBookStream()
    s.on_message(_book_msg("tokA"))
    assert s.is_fresh("tokA", max_age=100)
    assert s.get_book("tokA", max_age=100)["asks"][0] == (0.54, 50.0)

    # Unknown token -> no book (caller will REST-fallback).
    assert s.get_book("nope", max_age=100) is None

    # Disconnect marks everything stale -> get_book returns None.
    s._on_disconnect()
    assert not s.is_fresh("tokA", max_age=100)
    assert s.get_book("tokA", max_age=100) is None


def test_orderbook_freshness_respects_max_age():
    s = OrderBookStream()
    s.on_message(_book_msg("tokA"))
    s._books["tokA"].last_ts = time.time() - 60  # age it
    assert not s.is_fresh("tokA", max_age=5)
    assert s.get_book("tokA", max_age=5) is None


def test_orderbook_handles_batched_list_message():
    s = OrderBookStream()
    s.on_message([_book_msg("tokA"), _book_msg("tokB")])
    assert s.is_fresh("tokA", max_age=100) and s.is_fresh("tokB", max_age=100)


def test_resubscribe_drops_old_books():
    s = OrderBookStream()
    s.on_message(_book_msg("old"))
    s.resubscribe(["new1", "new2"])  # not connected -> just updates desired + drops old
    assert s.get_book("old", max_age=100) is None
    assert s._assets == ["new1", "new2"]


# --- SpotStream ----------------------------------------------------------- #
def test_spot_stream_tracks_last_trade_price():
    s = SpotStream("BTC-USD")
    assert s.get_spot(max_age=100) is None  # nothing yet
    s.on_message({"type": "ticker", "product_id": "BTC-USD", "price": "51000.5"})
    assert s.get_spot(max_age=100) == pytest.approx(51000.5)
    assert s.is_fresh(max_age=100)


def test_spot_stream_ignores_non_ticker():
    s = SpotStream("BTC-USD")
    s.on_message({"type": "subscriptions"})
    assert s.get_spot(max_age=100) is None


def test_spot_freshness_respects_max_age():
    s = SpotStream("BTC-USD")
    s.on_message({"type": "ticker", "price": "50000"})
    s._price_ts = time.time() - 60
    assert s.get_spot(max_age=5) is None


# --- graceful degradation when websocket-client is missing ---------------- #
def test_stream_start_is_noop_when_websocket_unavailable(monkeypatch):
    monkeypatch.setattr(streaming, "WEBSOCKET_AVAILABLE", False)
    s = OrderBookStream()
    s.start()  # must not raise or spawn a thread
    assert s._thread is None


def test_live_market_skips_streams_when_unavailable(monkeypatch, tmp_path):
    from evolver.config import Config
    from evolver.market import LiveMarket

    monkeypatch.setattr(streaming, "WEBSOCKET_AVAILABLE", False)
    m = LiveMarket(Config(use_websocket=True))
    m._ensure_streams()
    assert m._book_stream is None and m._spot_stream is None  # -> REST fallback path


# --- proxy parsing -------------------------------------------------------- #
def test_proxy_from_env(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("WSS_PROXY", raising=False)
    assert _proxy_from_env() == {}
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
    p = _proxy_from_env()
    assert p["http_proxy_host"] == "127.0.0.1" and p["http_proxy_port"] == 8899
