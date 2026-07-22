"""Market providers: the interface the generation runner consumes, plus the live
implementation built on :mod:`polybot`.

The runner only depends on the small :class:`MarketProvider` protocol, so tests
inject a deterministic mock with zero network. :class:`LiveMarket` implements the
protocol against real Coinbase + Gamma/CLOB data, keeping every network-touching
behavior (discovery with ``end_date_min``, closed-candles-only, resolution
reconciliation) inside :mod:`polybot`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Protocol

from polybot import candles as cb
from polybot import polymarket as pm
from polybot import streaming

from .config import Config
from .models import PollSnapshot

log = logging.getLogger("evolver.market")


@dataclass
class WindowHandle:
    window_id: str
    condition_id: str
    title: str
    start: dt.datetime
    end: dt.datetime
    token_map: Dict[str, str]
    window_open_price: float = 0.0


@dataclass
class Resolution:
    coinbase_side: Optional[str]
    official_side: Optional[str]
    five_min_candle: Optional[dict] = None


class MarketProvider(Protocol):
    def next_window(self) -> WindowHandle: ...
    def poll_snapshots(self, handle: WindowHandle) -> Iterator[PollSnapshot]: ...
    def resolve(self, handle: WindowHandle) -> Resolution: ...


def hash_candles(candles: List[dict], window_open_price: float) -> str:
    """Stable hash of the closed-candle state (for repeatability auditing)."""
    payload = json.dumps(
        {
            "open": round(window_open_price, 4),
            "candles": [(c.get("time"), c.get("open"), c.get("close")) for c in candles],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class LiveMarket:
    """Live market provider backed by Coinbase + Polymarket Gamma/CLOB."""

    def __init__(self, config: Config):
        self.config = config
        self._book_stream: streaming.OrderBookStream = None
        self._spot_stream: streaming.SpotStream = None
        # Windows already handed out, so we never return the same market twice
        # (one trade per distinct 5-minute window).
        self._returned_windows: set = set()

    def _ensure_streams(self) -> None:
        """Lazily start the WebSocket feeds (once) when enabled and available."""
        if not self.config.use_websocket or not streaming.WEBSOCKET_AVAILABLE:
            return
        if self._book_stream is None:
            self._book_stream = streaming.OrderBookStream(self.config.pm_ws_url)
            self._book_stream.start()
        if self._spot_stream is None:
            self._spot_stream = streaming.SpotStream(self.config.product, self.config.coinbase_ws_url)
            self._spot_stream.start()

    def close(self) -> None:
        """Stop the WebSocket feeds. Safe to call more than once."""
        for stream in (self._book_stream, self._spot_stream):
            if stream is not None:
                stream.stop()
        self._book_stream = None
        self._spot_stream = None

    # --- discovery -------------------------------------------------------- #
    def _next_unreturned(self, windows: List[pm.Window], now: dt.datetime):
        """Earliest not-yet-in-the-past window we have not returned before."""
        fresh = [
            w for w in windows
            if w.end > now.astimezone(w.end.tzinfo) and w.window_id not in self._returned_windows
        ]
        fresh.sort(key=lambda w: w.start)
        return fresh[0] if fresh else None

    def next_window(self) -> WindowHandle:
        """Block until a NEW active/upcoming Bitcoin Up/Down window is available.

        Retries discovery (Gamma listings can be late). A window is returned at
        most once, so the caller trades each distinct 5-minute market only once
        (never re-entering the same still-open window repeatedly).
        """
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            w = self._next_unreturned(self._safe_discover(now), now)
            if w is not None:
                token_map = self._safe_token_map(w.condition_id)
                if token_map.get("Up") and token_map.get("Down"):
                    self._returned_windows.add(w.window_id)
                    handle = WindowHandle(
                        window_id=w.window_id,
                        condition_id=w.condition_id,
                        title=w.title,
                        start=w.start,
                        end=w.end,
                        token_map=token_map,
                    )
                    # Point the live book feed at this window's tokens.
                    self._ensure_streams()
                    if self._book_stream is not None:
                        self._book_stream.resubscribe([token_map["Up"], token_map["Down"]])
                    self._wait_until(handle.start)
                    return handle
            time.sleep(self.config.poll_interval_seconds)

    def _safe_discover(self, now) -> List[pm.Window]:
        try:
            return pm.discover_windows(now)
        except Exception:  # noqa: BLE001 — transient network, retry
            return []

    def _safe_token_map(self, condition_id: str) -> Dict[str, str]:
        try:
            return pm.token_map(condition_id)
        except Exception:  # noqa: BLE001
            return {}

    def _wait_until(self, when: dt.datetime) -> None:
        while True:
            now = dt.datetime.now(when.tzinfo)
            delta = (when - now).total_seconds()
            if delta <= 0:
                return
            time.sleep(min(delta, self.config.poll_interval_seconds))

    # --- polling ---------------------------------------------------------- #
    def poll_snapshots(self, handle: WindowHandle) -> Iterator[PollSnapshot]:
        """Yield a snapshot at window open and at a constant cadence thereafter.

        Polls are anchored to ``open + k*poll_interval`` on a monotonic clock, so
        the interval is exactly constant and drift-free — sampling the in-memory
        WS state is cheap, unlike per-poll REST calls. A final poll is guaranteed
        ``final_poll_lead_seconds`` before close so late-window strategies still
        get a decision in the closing seconds.
        """
        self._ensure_streams()
        interval = float(self.config.poll_interval_seconds)
        lead = float(self.config.final_poll_lead_seconds)
        anchor = time.monotonic()
        poll_index = 0
        window_open_price = 0.0
        while True:
            now = dt.datetime.now(handle.end.tzinfo)
            seconds_remaining = int((handle.end - now).total_seconds())
            spot = self._safe_spot()
            if poll_index == 0:
                window_open_price = spot
                handle.window_open_price = spot
            yield PollSnapshot(
                poll_index=poll_index,
                seconds_remaining=max(0, seconds_remaining),
                window_open_price=window_open_price,
                spot=spot,
                candles=self._safe_candles(),
                books=self._safe_books(handle.token_map),
            )
            poll_index += 1
            if seconds_remaining <= 0:
                return
            # Sleep to the next constant tick, but never past the close: if the
            # next tick would land after the window ends, take one final poll
            # `lead` seconds before close instead.
            secs_to_end = (handle.end - dt.datetime.now(handle.end.tzinfo)).total_seconds()
            delay = (anchor + poll_index * interval) - time.monotonic()
            if delay >= secs_to_end:
                delay = max(0.0, secs_to_end - lead)
            if delay > 0:
                time.sleep(delay)

    def _safe_spot(self) -> float:
        if self._spot_stream is not None:
            price = self._spot_stream.get_spot(self.config.ws_staleness_seconds)
            if price is not None:
                return price
        try:
            return cb.spot(self.config.product)
        except Exception:  # noqa: BLE001
            return 0.0

    def _safe_candles(self) -> List[dict]:
        try:
            return cb.closed_1m_candles(self.config.product)
        except Exception:  # noqa: BLE001
            return []

    def _safe_books(self, token_map: Dict[str, str]) -> Dict[str, dict]:
        books: Dict[str, dict] = {}
        for side in ("Up", "Down"):
            token_id = token_map.get(side)
            # Prefer the live WS book; fall back to REST when stale/absent.
            if token_id and self._book_stream is not None:
                ws_book = self._book_stream.get_book(token_id, self.config.ws_staleness_seconds)
                if ws_book is not None:
                    books[side] = ws_book
                    continue
            try:
                books[side] = pm.order_book(token_id) if token_id else {"asks": [], "bids": []}
            except Exception:  # noqa: BLE001
                books[side] = {"asks": [], "bids": []}
        return books

    # --- resolution ------------------------------------------------------- #
    def resolve(self, handle: WindowHandle) -> Resolution:
        """Resolve authoritatively from Polymarket's official outcome, WAITING for it.

        These 5-minute markets do NOT reliably match the Coinbase 5m candle
        (settlement uses Polymarket's own price feed/timing), so the official
        Gamma ``outcomePrices`` result is authoritative. We block until it is
        available; the Coinbase candle is kept only as a diagnostic. If
        ``resolution_timeout_seconds`` is set (not None), we give up after it and
        fall back to the Coinbase estimate, logging the fallback.
        """
        # The window may still be open (a strategy can enter mid-window and we
        # break early); scoring is meaningless until it closes.
        self._wait_until(handle.end)

        five_min = self._safe_five_minute(handle.start.timestamp())
        coinbase_side = self._coinbase_side(five_min)

        cap = self.config.resolution_timeout_seconds  # None => wait indefinitely
        start = time.monotonic()
        last_heartbeat = 0.0
        official_side: Optional[str] = None
        while True:
            official_side = self._safe_official(handle.condition_id)
            if official_side is not None:
                break
            elapsed = time.monotonic() - start
            if cap is not None and elapsed >= cap:
                log.warning(
                    "official resolution for %s not available after %.0fs; "
                    "falling back to Coinbase estimate (%s)",
                    handle.window_id, elapsed, coinbase_side,
                )
                break
            if five_min is None:  # keep the diagnostic fresh while we wait
                five_min = self._safe_five_minute(handle.start.timestamp())
                coinbase_side = self._coinbase_side(five_min)
            if elapsed - last_heartbeat >= self.config.resolution_heartbeat_seconds:
                last_heartbeat = elapsed
                log.info("waiting for official Polymarket resolution of %s (%.0fs elapsed)…",
                         handle.window_id, elapsed)
            time.sleep(self.config.resolution_poll_seconds)

        return Resolution(coinbase_side=coinbase_side, official_side=official_side, five_min_candle=five_min)

    @staticmethod
    def _coinbase_side(five_min: Optional[dict]) -> Optional[str]:
        if five_min is None:
            return None
        return "Up" if five_min["close"] > five_min["open"] else "Down"

    def _safe_five_minute(self, start_ts: float) -> Optional[dict]:
        try:
            return cb.five_minute_candle(self.config.product, start_ts)
        except Exception:  # noqa: BLE001
            return None

    def _safe_official(self, condition_id: str) -> Optional[str]:
        try:
            return pm.official_outcome(condition_id)
        except Exception:  # noqa: BLE001
            return None
