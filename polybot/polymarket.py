"""Polymarket discovery, token mapping, and resolution.

Behaviors ported verbatim from the task spec:

- Discovery uses the Gamma ``/markets`` endpoint with active/closed/archived
  filters AND ``end_date_min=now``. Without ``end_date_min`` Gamma happily
  returns months-old stale markets, so it is mandatory.

- The window is parsed from the market title, e.g.
  ``"Bitcoin Up or Down - July 20, 3:00PM-3:05PM ET"``. Times are
  America/New_York; the year is inferred (titles omit it).

- CRITICAL: the outcome -> token_id mapping is taken from the CLOB
  ``/markets/{conditionId}`` response, where each token carries an explicit
  ``"outcome"`` label. Gamma's parallel ``outcomes`` / ``clobTokenIds`` arrays
  have shipped in *flipped* order, so pairing them by index is a known bug.

- Resolution reads Gamma ``outcomePrices`` for the official outcome and is
  reconciled against the immediate Coinbase 5m score by the caller.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
ET = ZoneInfo("America/New_York")

# "Bitcoin Up or Down - July 20, 3:00PM-3:05PM ET"
_TITLE_RE = re.compile(
    r"-\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})(?P<sap>[AP]M)"
    r"\s*-\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})(?P<eap>[AP]M)",
    re.IGNORECASE,
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "",
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
    )
    if i
}


@dataclass
class Window:
    """A single 5-minute Bitcoin Up/Down market."""

    condition_id: str
    title: str
    start: dt.datetime  # tz-aware, ET
    end: dt.datetime  # tz-aware, ET
    token_map: Dict[str, str] = field(default_factory=dict)  # "Up"/"Down" -> token_id
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def window_id(self) -> str:
        return f"{self.condition_id}"


def _get(url: str, params: Optional[dict] = None, timeout: float = 15.0) -> Any:
    resp = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "evolver/0.1 (+polybot.polymarket)"},
    )
    resp.raise_for_status()
    return resp.json()


def parse_window_title(title: str, now: Optional[dt.datetime] = None) -> Optional[tuple]:
    """Parse ``(start, end)`` ET datetimes from a market title.

    The year is inferred: pick the year that puts the window closest to ``now``
    (handles the Dec->Jan rollover without a hard-coded year).
    """
    m = _TITLE_RE.search(title)
    if not m:
        return None
    now = now or dt.datetime.now(ET)
    month = _MONTHS.get(m.group("month").lower())
    if not month:
        return None
    day = int(m.group("day"))

    def _mk(hour12: int, minute: int, ampm: str, year: int) -> dt.datetime:
        hour = hour12 % 12
        if ampm.upper() == "PM":
            hour += 12
        return dt.datetime(year, month, day, hour, minute, tzinfo=ET)

    # Try the year around `now` and pick the closest.
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            start = _mk(int(m.group("sh")), int(m.group("sm")), m.group("sap"), year)
            end = _mk(int(m.group("eh")), int(m.group("em")), m.group("eap"), year)
        except ValueError:
            continue
        if end < start:  # crosses midnight
            end += dt.timedelta(days=1)
        candidates.append((abs((start - now).total_seconds()), start, end))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1], candidates[0][2]


def discover_windows(
    now: Optional[dt.datetime] = None,
    limit: int = 100,
    slug_contains: str = "bitcoin-up-or-down",
) -> List[Window]:
    """Discover live/upcoming Bitcoin Up/Down 5-minute markets.

    Uses ``end_date_min=now`` so Gamma does not return stale markets.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "end_date_min": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": limit,
        "order": "endDate",
        "ascending": "true",
    }
    rows = _get(f"{GAMMA_BASE}/markets", params)
    windows: List[Window] = []
    now_et = now.astimezone(ET)
    for row in rows:
        title = row.get("question") or row.get("title") or ""
        slug = row.get("slug", "")
        if slug_contains not in slug and "bitcoin up or down" not in title.lower():
            continue
        parsed = parse_window_title(title, now_et)
        if not parsed:
            continue
        start, end = parsed
        cond = row.get("conditionId") or row.get("condition_id")
        if not cond:
            continue
        windows.append(Window(condition_id=cond, title=title, start=start, end=end, raw=row))
    return windows


def token_map(condition_id: str) -> Dict[str, str]:
    """Map ``{"Up": token_id, "Down": token_id}`` from CLOB, by explicit label.

    This is the authoritative source: each token in the CLOB market carries an
    explicit ``"outcome"`` field. Never index Gamma's parallel arrays.
    """
    data = _get(f"{CLOB_BASE}/markets/{condition_id}")
    tokens = data.get("tokens", [])
    mapping: Dict[str, str] = {}
    for tok in tokens:
        outcome = (tok.get("outcome") or "").strip()
        token_id = tok.get("token_id") or tok.get("tokenId")
        if outcome and token_id:
            mapping[outcome] = str(token_id)
    return mapping


def order_book(token_id: str) -> Dict[str, List[tuple]]:
    """Fetch the CLOB order book for a token as ``{"asks":[(p,sz)], "bids":[...]}``.

    Asks are returned ascending by price (best/lowest ask first); bids descending.
    """
    data = _get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    asks = [(float(l["price"]), float(l["size"])) for l in data.get("asks", [])]
    bids = [(float(l["price"]), float(l["size"])) for l in data.get("bids", [])]
    asks.sort(key=lambda x: x[0])
    bids.sort(key=lambda x: x[0], reverse=True)
    return {"asks": asks, "bids": bids}


def official_outcome(condition_id: str) -> Optional[str]:
    """Return the official winning side ("Up"/"Down") from Gamma outcomePrices.

    ``outcomePrices`` is a JSON array of "1"/"0" strings parallel to
    ``outcomes``. Returns None if the market has not resolved yet.
    """
    rows = _get(f"{GAMMA_BASE}/markets", params={"condition_ids": condition_id})
    if not rows:
        return None
    row = rows[0] if isinstance(rows, list) else rows
    outcomes = _maybe_json(row.get("outcomes"))
    prices = _maybe_json(row.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    for name, price in zip(outcomes, prices):
        try:
            if float(price) >= 0.99:
                return str(name)
        except (TypeError, ValueError):
            continue
    return None


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value
