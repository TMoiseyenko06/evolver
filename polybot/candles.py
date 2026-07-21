"""Coinbase Exchange public-API candles and spot.

Binance blocks US IPs, so all BTC price data comes from Coinbase Exchange's
public REST API (no auth required):

- Candles:  GET /products/{product}/candles?granularity={s}&start=&end=
            -> [[time, low, high, open, close, volume], ...] newest first.
- Ticker:   GET /products/{product}/ticker -> {"price": "...", ...}

CRITICAL: a strategy must never see the *forming* candle. A 1-minute candle
whose bucket start is the current minute has not closed yet; feeding it to a
strategy leaks the future (lookahead bias) and invalidates every backtest. The
helpers here filter to *closed* candles only: a candle is closed once
``bucket_start + granularity <= now``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

COINBASE_BASE = "https://api.exchange.coinbase.com"
DEFAULT_PRODUCT = "BTC-USD"

# Coinbase candle array positions.
_T, _LOW, _HIGH, _OPEN, _CLOSE, _VOL = range(6)


def _get(url: str, params: Optional[dict] = None, timeout: float = 10.0) -> Any:
    resp = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "evolver/0.1 (+polybot.candles)"},
    )
    resp.raise_for_status()
    return resp.json()


def raw_candles(
    product: str,
    granularity: int,
    start: Optional[float] = None,
    end: Optional[float] = None,
) -> List[dict]:
    """Fetch candles, returned oldest-first as dicts.

    Each dict has keys ``time`` (epoch seconds, bucket start), ``low``, ``high``,
    ``open``, ``close``, ``volume``.
    """
    params: Dict[str, Any] = {"granularity": granularity}
    if start is not None:
        params["start"] = _iso(start)
    if end is not None:
        params["end"] = _iso(end)
    rows = _get(f"{COINBASE_BASE}/products/{product}/candles", params)
    out = [
        {
            "time": int(r[_T]),
            "low": float(r[_LOW]),
            "high": float(r[_HIGH]),
            "open": float(r[_OPEN]),
            "close": float(r[_CLOSE]),
            "volume": float(r[_VOL]),
        }
        for r in rows
    ]
    out.sort(key=lambda c: c["time"])  # oldest first, newest last
    return out


def closed_1m_candles(
    product: str = DEFAULT_PRODUCT,
    lookback_minutes: int = 60,
    now: Optional[float] = None,
) -> List[dict]:
    """Return CLOSED 1-minute candles, newest last, forming candle excluded.

    This is exactly the shape strategies receive via ``ctx.candles``: a list of
    dicts with at least ``time``/``open``/``close``, closed only.
    """
    now = time.time() if now is None else now
    start = now - lookback_minutes * 60
    candles = raw_candles(product, 60, start=start, end=now)
    # A 1-minute candle is closed once its bucket end (time + 60) has passed.
    return [c for c in candles if c["time"] + 60 <= now]


def five_minute_candle(
    product: str,
    window_start: float,
    now: Optional[float] = None,
) -> Optional[dict]:
    """Return the closed 5-minute candle whose bucket starts at ``window_start``.

    Used to score a resolved window: ``close > open`` -> Up, tie/``close <= open``
    -> Down. Returns None if the candle has not closed yet.
    """
    now = time.time() if now is None else now
    bucket = int(window_start // 300 * 300)
    if bucket + 300 > now:
        return None  # not closed yet
    candles = raw_candles(product, 300, start=bucket, end=bucket + 300)
    for c in candles:
        if c["time"] == bucket:
            return c
    return None


def spot(product: str = DEFAULT_PRODUCT) -> float:
    """Latest trade price from the Coinbase ticker."""
    data = _get(f"{COINBASE_BASE}/products/{product}/ticker")
    return float(data["price"])


def _iso(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.utcfromtimestamp(epoch).isoformat()
