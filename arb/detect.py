"""Arbitrage detection: normalize markets, then find complementary sets < $1.

The math is pure and deterministic (hence unit-tested): given the executable asks
for a binary event's two outcomes, an arb exists when the cheapest way to buy BOTH
outcomes — possibly on different venues — costs less than $1 after fees, since the
set is guaranteed to pay exactly $1 at resolution.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from polybot import synthesis

from . import fees as feemod
from .model import ArbLeg, ArbOpportunity, Market, Quote

# --------------------------------------------------------------------------- #
# Normalization: unified /markets response -> Market objects
# --------------------------------------------------------------------------- #
def parse_markets(payload: Any, venue_hint: Optional[str] = None) -> List[Market]:
    """Turn a unified ``GET /api/v1/markets`` response into :class:`Market` objects."""
    out: List[Market] = []
    for event in synthesis._as_list(synthesis._unwrap(payload)):
        if not isinstance(event, dict):
            continue
        venue = event.get("venue") or venue_hint or ""
        event_id = str(event.get("event_id") or event.get("id") or "")
        markets = event.get("markets") if isinstance(event.get("markets"), list) else [event]
        for m in markets:
            if not isinstance(m, dict):
                continue
            market = _parse_market(m, venue, event_id)
            if market is not None:
                out.append(market)
    return out


def _parse_market(m: Dict[str, Any], venue: str, event_id: str) -> Optional[Market]:
    mid = (m.get("condition_id") or m.get("conditionId") or m.get("market_id")
           or m.get("kalshi_id") or m.get("id"))
    if mid is None:
        return None
    quotes: List[Quote] = []
    for out_key, tok_key, price_key in (
        ("left_outcome", "left_token_id", "left_price"),
        ("right_outcome", "right_token_id", "right_price"),
    ):
        outcome, token = m.get(out_key), m.get(tok_key)
        if outcome and token:
            quotes.append(Quote(outcome=str(outcome), token_id=str(token),
                                mid=synthesis._num(m.get(price_key))))
    if len(quotes) != 2:
        return None
    return Market(
        venue=str(venue), market_id=str(mid), event_id=str(event_id or m.get("event_id") or ""),
        title=str(m.get("title") or m.get("question") or ""),
        ends_at=(str(m.get("ends_at")) if m.get("ends_at") is not None else None),
        resolved=bool(m.get("resolved")),
        quotes=quotes,
        liquidity=synthesis._num(m.get("liquidity")),
        volume=synthesis._num(m.get("volume")),
        raw=m,
    )


def apply_orderbooks(markets: List[Market], books_by_token: Dict[str, dict]) -> None:
    """Fill each quote's ``ask``/``ask_size``/``bid`` from fetched order books (in place)."""
    for market in markets:
        for q in market.quotes:
            book = books_by_token.get(q.token_id)
            if not book:
                continue
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            if asks:
                q.ask, q.ask_size = float(asks[0][0]), float(asks[0][1])
            if bids:
                q.bid = float(bids[0][0])


# --------------------------------------------------------------------------- #
# Core arb math
# --------------------------------------------------------------------------- #
def _leg(market: Market, q: Quote) -> ArbLeg:
    return ArbLeg(venue=market.venue, market_id=market.market_id, title=market.title,
                  outcome=q.outcome, token_id=q.token_id, ask=float(q.ask or 0.0),
                  ask_size=float(q.ask_size or 0.0))


def _build(kind: str, title: str, leg_a: ArbLeg, leg_b: ArbLeg,
           ends_at: Optional[str]) -> ArbOpportunity:
    gross = leg_a.ask + leg_b.ask
    fees = feemod.fee_per_share(leg_a.venue, leg_a.ask) + feemod.fee_per_share(leg_b.venue, leg_b.ask)
    net = gross + fees
    return ArbOpportunity(
        kind=kind, title=title, legs=[leg_a, leg_b], gross_cost=gross, fees=fees,
        net_cost=net, edge=1.0 - net, max_size=min(leg_a.ask_size, leg_b.ask_size),
        ends_at=ends_at,
    )


def intra_market_arb(market: Market, min_edge: float = 0.0) -> Optional[ArbOpportunity]:
    """Both outcomes of ONE market for < $1 after fees. The cleanest, safest lock."""
    if len(market.quotes) != 2:
        return None
    a, b = market.quotes
    if a.ask is None or b.ask is None or a.ask <= 0 or b.ask <= 0:
        return None
    opp = _build("intra", market.title, _leg(market, a), _leg(market, b), market.ends_at)
    return opp if opp.edge > min_edge else None


def cross_venue_arb(markets: List[Market], min_edge: float = 0.0) -> Optional[ArbOpportunity]:
    """Best complementary lock across markets for the SAME event (>=1 venue).

    Collects the cheapest ask for each of the two canonical outcomes across all
    ``markets`` and forms the lock. Only auto-computes when the markets agree on the
    outcome label set (e.g. both Yes/No), to avoid mis-mapping Up/Down↔Yes/No.
    """
    label_sets = {frozenset(q.outcome.lower() for q in m.quotes) for m in markets}
    if len(label_sets) != 1:
        return None
    labels = sorted(next(iter(label_sets)))
    if len(labels) != 2:
        return None
    best: Dict[str, Tuple[Market, Quote]] = {}
    for m in markets:
        for q in m.quotes:
            if q.ask is None or q.ask <= 0:
                continue
            key = q.outcome.lower()
            if key not in best or q.ask < best[key][1].ask:
                best[key] = (m, q)
    if labels[0] not in best or labels[1] not in best:
        return None
    (m_a, q_a), (m_b, q_b) = best[labels[0]], best[labels[1]]
    kind = "intra" if m_a.market_id == m_b.market_id and m_a.venue == m_b.venue else "cross"
    opp = _build(kind, m_a.title, _leg(m_a, q_a), _leg(m_b, q_b), m_a.ends_at)
    return opp if opp.edge > min_edge else None


# --------------------------------------------------------------------------- #
# Cross-venue event matching (fuzzy — surfaced for human confirmation)
# --------------------------------------------------------------------------- #
_STOP = {"will", "the", "a", "an", "be", "to", "of", "in", "on", "at", "by", "for",
         "and", "or", "is", "are", "this", "that", "than", "market", "?"}


def normalize_title(title: str) -> frozenset:
    """Tokenize a title into a comparable bag of significant words."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(w for w in words if w not in _STOP and len(w) > 1)


def title_similarity(a: str, b: str) -> float:
    """Jaccard overlap of significant title words in [0, 1]."""
    ta, tb = normalize_title(a), normalize_title(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_events(
    markets_a: List[Market], markets_b: List[Market],
    min_similarity: float = 0.6, ends_tol_seconds: float = 3600.0,
) -> List[Tuple[Market, Market, float]]:
    """Greedily match markets across two venues by title similarity + close end time.

    Returns ``(market_a, market_b, similarity)`` candidates, best first. These are
    CANDIDATES for a human to confirm resolve identically — matching titles does not
    guarantee the same reference price / settlement rule, which is the whole risk.
    """
    pairs: List[Tuple[Market, Market, float]] = []
    for ma in markets_a:
        for mb in markets_b:
            if not _ends_close(ma.ends_at, mb.ends_at, ends_tol_seconds):
                continue
            sim = title_similarity(ma.title, mb.title)
            if sim >= min_similarity:
                pairs.append((ma, mb, sim))
    pairs.sort(key=lambda t: t[2], reverse=True)
    return pairs


def _ends_close(a: Optional[str], b: Optional[str], tol: float) -> bool:
    ta, tb = _to_epoch(a), _to_epoch(b)
    if ta is None or tb is None:
        return True  # can't compare -> don't exclude on time alone
    return abs(ta - tb) <= tol


def _to_epoch(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    try:
        return float(s)  # unix timestamp
    except ValueError:
        pass
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None
