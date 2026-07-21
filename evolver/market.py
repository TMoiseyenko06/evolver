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
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Protocol

from polybot import candles as cb
from polybot import polymarket as pm

from .config import Config
from .models import PollSnapshot


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

    # --- discovery -------------------------------------------------------- #
    def next_window(self) -> WindowHandle:
        """Block until an active/upcoming Bitcoin Up/Down window is available.

        Retries discovery (Gamma listings can be late) until a window whose end
        is still in the future is found; waits out any gap before its start.
        """
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            windows = self._safe_discover(now)
            upcoming = [w for w in windows if w.end > now.astimezone(w.end.tzinfo)]
            upcoming.sort(key=lambda w: w.start)
            if upcoming:
                w = upcoming[0]
                token_map = self._safe_token_map(w.condition_id)
                if token_map.get("Up") and token_map.get("Down"):
                    handle = WindowHandle(
                        window_id=w.window_id,
                        condition_id=w.condition_id,
                        title=w.title,
                        start=w.start,
                        end=w.end,
                        token_map=token_map,
                    )
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
        """Yield a snapshot at window open and every ``poll_interval`` seconds."""
        poll_index = 0
        window_open_price = 0.0
        while True:
            now = dt.datetime.now(handle.end.tzinfo)
            seconds_remaining = int((handle.end - now).total_seconds())
            spot = self._safe_spot()
            if poll_index == 0:
                window_open_price = spot
                handle.window_open_price = spot
            snap = PollSnapshot(
                poll_index=poll_index,
                seconds_remaining=max(0, seconds_remaining),
                window_open_price=window_open_price,
                spot=spot,
                candles=self._safe_candles(),
                books=self._safe_books(handle.token_map),
            )
            yield snap
            poll_index += 1
            if seconds_remaining <= 0:
                return
            time.sleep(min(self.config.poll_interval_seconds, max(1, seconds_remaining)))

    def _safe_spot(self) -> float:
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
            try:
                books[side] = pm.order_book(token_id) if token_id else {"asks": [], "bids": []}
            except Exception:  # noqa: BLE001
                books[side] = {"asks": [], "bids": []}
        return books

    # --- resolution ------------------------------------------------------- #
    def resolve(self, handle: WindowHandle) -> Resolution:
        """Score immediately from the Coinbase 5m candle, then reconcile with Gamma."""
        start_ts = handle.start.timestamp()
        five_min = None
        # Wait briefly for the 5m candle to close, then fetch it.
        for _ in range(6):
            five_min = self._safe_five_minute(start_ts)
            if five_min is not None:
                break
            time.sleep(5)
        coinbase_side = None
        if five_min is not None:
            coinbase_side = "Up" if five_min["close"] > five_min["open"] else "Down"
        official_side = self._safe_official(handle.condition_id)
        return Resolution(coinbase_side=coinbase_side, official_side=official_side, five_min_candle=five_min)

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
