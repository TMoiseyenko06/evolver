"""Real-time market data over WebSockets (order books + spot).

Two public channels, both auth-free:

- Polymarket CLOB market channel — ``wss://ws-subscriptions-clob.polymarket.com/ws/market``
  Subscribe with ``{"assets_ids": [...], "type": "market"}``; receive a full
  ``book`` snapshot then incremental ``price_change`` deltas per asset.
- Coinbase Exchange feed — ``wss://ws-feed.exchange.coinbase.com``
  Subscribe ``ticker`` for a product; each message carries the last trade ``price``.

Design: the socket I/O (`_WSClient`, `OrderBookStream`, `SpotStream`) is kept thin,
and all book parsing/mutation lives in **pure functions** (`apply_book_snapshot`,
`apply_price_change`, `sorted_book`) that are unit-tested without a live socket.
A background daemon thread maintains state; readers take a consistent copy under a
lock. Any WS failure is non-fatal — callers fall back to REST.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import websocket  # from the `websocket-client` package
    # Guard against the unrelated `websocket` (0.2.1) package, which lacks this.
    if not hasattr(websocket, "WebSocketApp"):
        websocket = None
except ImportError:  # pragma: no cover
    websocket = None

# True only when the real `websocket-client` package is importable. When False,
# the streams disable themselves and callers transparently fall back to REST.
WEBSOCKET_AVAILABLE = websocket is not None

log = logging.getLogger("polybot.streaming")

PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

Level = Tuple[float, float]


# --------------------------------------------------------------------------- #
# Pure book state + mutations (no I/O — fully testable)
# --------------------------------------------------------------------------- #
@dataclass
class BookState:
    """A single asset's book as price->size maps, plus freshness bookkeeping."""

    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)
    has_snapshot: bool = False
    stale: bool = False
    last_ts: float = 0.0
    hash: Optional[str] = None


def apply_book_snapshot(state: BookState, msg: dict, now: Optional[float] = None) -> BookState:
    """Replace a book from a full ``book`` snapshot message."""
    now = time.time() if now is None else now
    state.asks = {float(l["price"]): float(l["size"]) for l in msg.get("asks", []) if float(l["size"]) > 0}
    state.bids = {float(l["price"]): float(l["size"]) for l in msg.get("bids", []) if float(l["size"]) > 0}
    state.has_snapshot = True
    state.stale = False
    state.hash = msg.get("hash")
    state.last_ts = now
    return state


def apply_price_change(state: BookState, msg: dict, now: Optional[float] = None) -> BookState:
    """Apply incremental level deltas from a ``price_change`` message.

    Each change is ``{price, size, side}``; ``side`` BUY/BID updates bids,
    SELL/ASK updates asks; ``size == 0`` removes the level. Deltas before any
    snapshot has arrived are ignored (nothing to base them on).
    """
    now = time.time() if now is None else now
    if not state.has_snapshot:
        return state
    changes = msg.get("changes") or msg.get("price_changes") or []
    for ch in changes:
        side = str(ch.get("side", "")).upper()
        try:
            price = float(ch["price"])
            size = float(ch["size"])
        except (KeyError, TypeError, ValueError):
            continue
        book = state.bids if side in ("BUY", "BID") else state.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
    if "hash" in msg:
        state.hash = msg.get("hash")
    state.last_ts = now
    return state


def sorted_book(state: BookState) -> Dict[str, List[Level]]:
    """Render a BookState as ``{"asks": [(p,sz) asc], "bids": [(p,sz) desc]}``.

    Matches the shape of ``polybot.polymarket.order_book`` so downstream code is
    identical whether the book came from WS or REST.
    """
    asks = sorted(((p, s) for p, s in state.asks.items() if s > 0), key=lambda x: x[0])
    bids = sorted(((p, s) for p, s in state.bids.items() if s > 0), key=lambda x: x[0], reverse=True)
    return {"asks": asks, "bids": bids}


def _proxy_from_env() -> dict:
    """Return run_forever proxy kwargs from HTTPS_PROXY/WSS_PROXY, if set."""
    proxy = os.environ.get("WSS_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return {}
    parsed = urlparse(proxy)
    if not parsed.hostname:
        return {}
    return {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": "http",
    }


# --------------------------------------------------------------------------- #
# WebSocket client base
# --------------------------------------------------------------------------- #
class _WSClient:
    """Daemon-thread WebSocket client with reconnect + exponential backoff."""

    def __init__(self, url: str, name: str):
        self.url = url
        self.name = name
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.connected = False
        self.last_msg_ts = 0.0

    # --- lifecycle --- #
    def start(self) -> None:
        if not WEBSOCKET_AVAILABLE:
            log.warning(
                "%s stream disabled: `websocket-client` is not installed "
                "(falling back to REST). Install it with: pip install websocket-client",
                self.name,
            )
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        backoff = 1.0
        proxy = _proxy_from_env()
        while not self._stop.is_set():
            opened_at = None
            try:
                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open_cb,
                    on_message=self._on_message_cb,
                    on_error=self._on_error_cb,
                    on_close=self._on_close_cb,
                )
                opened_at = time.monotonic()
                self._ws.run_forever(ping_interval=20, ping_timeout=10, **proxy)
            except Exception as exc:  # noqa: BLE001 — network, keep retrying
                log.warning("%s ws error: %s", self.name, exc)
            self.connected = False
            self._on_disconnect()
            if self._stop.is_set():
                break
            # Reset backoff if the last connection was healthy for a while.
            if opened_at is not None and time.monotonic() - opened_at > 30:
                backoff = 1.0
            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)

    # --- callbacks --- #
    def _on_open_cb(self, ws) -> None:
        self.connected = True
        log.info("%s ws connected", self.name)
        try:
            self.on_open(ws)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s on_open failed: %s", self.name, exc)

    def _on_message_cb(self, ws, message) -> None:
        self.last_msg_ts = time.time()
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        try:
            self.on_message(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s on_message error: %s", self.name, exc)

    def _on_error_cb(self, ws, error) -> None:
        log.debug("%s ws error cb: %s", self.name, error)

    def _on_close_cb(self, ws, status_code, msg) -> None:
        self.connected = False

    def _send(self, payload: dict) -> None:
        try:
            if self._ws is not None and self.connected:
                self._ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            log.debug("%s send failed: %s", self.name, exc)

    # --- overridable hooks --- #
    def on_open(self, ws) -> None:  # pragma: no cover - subclass
        ...

    def on_message(self, data) -> None:  # pragma: no cover - subclass
        ...

    def _on_disconnect(self) -> None:
        ...


# --------------------------------------------------------------------------- #
# Order book stream (Polymarket)
# --------------------------------------------------------------------------- #
class OrderBookStream(_WSClient):
    def __init__(self, url: str = PM_WS_URL):
        super().__init__(url, "orderbook")
        self._books: Dict[str, BookState] = {}
        self._assets: List[str] = []

    def on_open(self, ws) -> None:
        if self._assets:
            self._send({"assets_ids": list(self._assets), "type": "market"})

    def resubscribe(self, asset_ids: List[str]) -> None:
        """Point the stream at a new window's tokens (subscribe + drop old books)."""
        asset_ids = [a for a in asset_ids if a]
        with self._lock:
            self._assets = list(asset_ids)
            # Forget books for assets we no longer track.
            for token in list(self._books):
                if token not in asset_ids:
                    self._books.pop(token, None)
        self._send({"assets_ids": list(asset_ids), "type": "market"})

    def on_message(self, data) -> None:
        events = data if isinstance(data, list) else [data]
        with self._lock:
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                etype = ev.get("event_type") or ev.get("type")
                token = ev.get("asset_id") or ev.get("asset")
                if not token:
                    continue
                state = self._books.setdefault(token, BookState())
                if etype == "book":
                    apply_book_snapshot(state, ev)
                elif etype == "price_change":
                    apply_price_change(state, ev)

    def get_book(self, token_id: str, max_age: float) -> Optional[Dict[str, List[Level]]]:
        """Return a fresh book copy, or None if stale/absent (caller falls back)."""
        with self._lock:
            state = self._books.get(token_id)
            if state is None or not self._is_fresh(state, max_age):
                return None
            return sorted_book(state)

    def is_fresh(self, token_id: str, max_age: float) -> bool:
        with self._lock:
            state = self._books.get(token_id)
            return state is not None and self._is_fresh(state, max_age)

    @staticmethod
    def _is_fresh(state: BookState, max_age: float) -> bool:
        return state.has_snapshot and not state.stale and (time.time() - state.last_ts) <= max_age

    def _on_disconnect(self) -> None:
        with self._lock:
            for state in self._books.values():
                state.stale = True


# --------------------------------------------------------------------------- #
# Spot stream (Coinbase Exchange ticker)
# --------------------------------------------------------------------------- #
class SpotStream(_WSClient):
    def __init__(self, product: str = "BTC-USD", url: str = COINBASE_WS_URL):
        super().__init__(url, "spot")
        self.product = product
        self._price: Optional[float] = None
        self._price_ts: float = 0.0

    def on_open(self, ws) -> None:
        self._send({"type": "subscribe", "product_ids": [self.product], "channels": ["ticker"]})

    def on_message(self, data) -> None:
        if not isinstance(data, dict):
            return
        if data.get("type") == "ticker" and data.get("price") is not None:
            try:
                price = float(data["price"])
            except (TypeError, ValueError):
                return
            with self._lock:
                self._price = price
                self._price_ts = time.time()

    def get_spot(self, max_age: float) -> Optional[float]:
        with self._lock:
            if self._price is None or (time.time() - self._price_ts) > max_age:
                return None
            return self._price

    def is_fresh(self, max_age: float) -> bool:
        with self._lock:
            return self._price is not None and (time.time() - self._price_ts) <= max_age
