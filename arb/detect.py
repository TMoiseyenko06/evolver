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
from evolver.engine import executable_price  # reuse the exact realistic-fill model

from . import fees as feemod
from .model import ArbLeg, ArbOpportunity, Market, Quote

# Fallback slippage curve params (only used when a complement bid is absent); the
# primary correction is the no-arb floor from the sibling outcome's bid.
SLIPPAGE_COEFF = 0.55
SLIPPAGE_EXP = 2.0

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


def apply_orderbooks(markets: List[Market], books_by_token: Dict[str, dict],
                     realistic: bool = True) -> None:
    """Fill each quote's ``ask``/``ask_size``/``bid`` from fetched order books (in place).

    When ``realistic``, also compute the EXECUTABLE ask (``ask_exec``) using the same
    cross-book no-arb model as the evolver: a displayed ask can't execute below
    ``1 - sibling_best_bid`` (the other outcome of the same market). This stops a
    phantom cheap ask from manufacturing a fake arb — the whole point of doing this
    on realistic data.
    """
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
        if realistic and len(market.quotes) == 2:
            a, b = market.quotes
            _set_exec(a, b)  # a's executable ask is floored by b's (complement) bid
            _set_exec(b, a)


def _set_exec(q: Quote, sibling: Quote) -> None:
    if q.ask is None or q.ask <= 0:
        return
    comp_bids = [(sibling.bid, sibling.ask_size or 1.0)] if sibling.bid else None
    q.ask_exec = executable_price(q.ask, comp_bids, SLIPPAGE_COEFF, SLIPPAGE_EXP)


# --------------------------------------------------------------------------- #
# Core arb math
# --------------------------------------------------------------------------- #
def _leg(market: Market, q: Quote, realistic: bool) -> ArbLeg:
    return ArbLeg(venue=market.venue, market_id=market.market_id, title=market.title,
                  outcome=q.outcome, token_id=q.token_id, ask=float(q.eff_ask(realistic) or 0.0),
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


def intra_market_arb(market: Market, min_edge: float = 0.0,
                     realistic: bool = True) -> Optional[ArbOpportunity]:
    """Both outcomes of ONE market for < $1 after fees. The cleanest, safest lock.

    With ``realistic``, uses executable asks — so a phantom cheap ask (whose
    executable price is clamped up by the sibling's bid) won't fake an arb.
    """
    if len(market.quotes) != 2:
        return None
    a, b = market.quotes
    if a.eff_ask(realistic) is None or b.eff_ask(realistic) is None:
        return None
    if a.eff_ask(realistic) <= 0 or b.eff_ask(realistic) <= 0:
        return None
    opp = _build("intra", market.title, _leg(market, a, realistic), _leg(market, b, realistic),
                 market.ends_at)
    return opp if opp.edge > min_edge else None


def group_by_event(markets: List[Market]) -> Dict[str, List[Market]]:
    """Group markets by (venue, event_id) — a multi-outcome event's sub-markets."""
    groups: Dict[str, List[Market]] = {}
    for m in markets:
        if not m.event_id:
            continue
        groups.setdefault(f"{m.venue}:{m.event_id}", []).append(m)
    return groups


def field_arb(
    markets: List[Market], min_edge: float = 0.0, realistic: bool = True,
    min_outcomes: int = 3, mid_low: float = 0.90, mid_high: float = 1.6,
) -> Optional[ArbOpportunity]:
    """Multi-outcome 'field' lock: buy YES on EVERY outcome of a one-winner event.

    A field of N mutually-exclusive Yes/No markets (e.g. one market per golfer, one
    winner) pays exactly $1 total. If ``Σ YES_ask < 1`` after fees, buying the whole
    field is a guaranteed lock. Uses EXECUTABLE asks so phantom prices don't fake it.

    Guards against the two ways this goes wrong:
    - **Not a field**: every sub-market must be Yes/No and there must be
      ``>= min_outcomes`` of them (excludes mixed events with totals/spreads/moneyline).
    - **Incomplete field** (the killer — a missing outcome could win and pay you $0):
      requires ``Σ YES_mid`` within ``[mid_low, mid_high]``. A complete, fairly-priced
      field sums to ~1 (+overround); a much smaller sum means outcomes are missing.
      This is a heuristic — still a HUMAN-CONFIRM candidate, never an auto-trade.
    """
    if len(markets) < min_outcomes:
        return None
    legs: List[ArbLeg] = []
    sum_mid = 0.0
    for m in markets:
        yes = m.quote("Yes")
        if yes is None or m.quote("No") is None:  # must be a Yes/No market
            return None
        price = yes.eff_ask(realistic)
        if price is None or price <= 0:            # a missing/empty book breaks the lock
            return None
        legs.append(_leg(m, yes, realistic))
        sum_mid += yes.mid or 0.0
    if not (mid_low <= sum_mid <= mid_high):       # likely incomplete or not a clean field
        return None
    gross = sum(l.ask for l in legs)
    fee = sum(feemod.fee_per_share(l.venue, l.ask) for l in legs)
    net = gross + fee
    opp = ArbOpportunity(
        kind="field", title=_field_title(markets), legs=legs, gross_cost=gross, fees=fee,
        net_cost=net, edge=1.0 - net, max_size=min(l.ask_size for l in legs),
        ends_at=markets[0].ends_at, sum_mid=sum_mid,
    )
    return opp if opp.edge > min_edge else None


def _field_title(markets: List[Market]) -> str:
    """Longest common title prefix (the shared proposition), else the first title."""
    titles = [m.title for m in markets if m.title]
    if not titles:
        return ""
    pre = titles[0]
    for t in titles[1:]:
        while not t.startswith(pre) and pre:
            pre = pre[:-1]
    return (pre.strip(" -:") or titles[0]) + f"  ({len(markets)} outcomes)"


def cross_venue_arb(markets: List[Market], min_edge: float = 0.0,
                    realistic: bool = True) -> Optional[ArbOpportunity]:
    """Best complementary lock across markets for the SAME event (>=1 venue).

    Collects the cheapest EXECUTABLE ask for each of the two canonical outcomes
    across all ``markets`` and forms the lock. Only auto-computes when the markets
    agree on the outcome label set (e.g. both Yes/No), to avoid mis-mapping
    Up/Down↔Yes/No.
    """
    label_sets = {frozenset(q.outcome.lower() for q in m.quotes) for m in markets}
    if len(label_sets) != 1:
        return None
    labels = sorted(next(iter(label_sets)))
    if len(labels) != 2:
        return None
    best: Dict[str, Tuple[Market, Quote, float]] = {}
    for m in markets:
        for q in m.quotes:
            price = q.eff_ask(realistic)
            if price is None or price <= 0:
                continue
            key = q.outcome.lower()
            if key not in best or price < best[key][2]:
                best[key] = (m, q, price)
    if labels[0] not in best or labels[1] not in best:
        return None
    (m_a, q_a, _), (m_b, q_b, _) = best[labels[0]], best[labels[1]]
    kind = "intra" if m_a.market_id == m_b.market_id and m_a.venue == m_b.venue else "cross"
    opp = _build(kind, m_a.title, _leg(m_a, q_a, realistic), _leg(m_b, q_b, realistic), m_a.ends_at)
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
    """Overlap coefficient of significant title words in [0, 1].

    Uses ``|A∩B| / min(|A|, |B|)`` rather than Jaccard so a short title on one venue
    ("Lakers win title") still matches a verbose one on the other ("Will the Los
    Angeles Lakers win the 2025 NBA championship title?"), which Jaccard would miss.
    """
    ta, tb = normalize_title(a), normalize_title(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def match_events(
    markets_a: List[Market], markets_b: List[Market],
    min_similarity: float = 0.5, ends_tol_seconds: float = 86400.0, min_shared: int = 2,
) -> List[Tuple[Market, Market, float]]:
    """Greedily match markets across two venues by title similarity + close end time.

    Returns ``(market_a, market_b, similarity)`` candidates, best first. Requires at
    least ``min_shared`` significant words in common (so a one-word overlap can't
    score 1.0). These are CANDIDATES for a human to confirm resolve identically —
    matching titles does not guarantee the same reference price / settlement rule,
    which is the whole risk.
    """
    pairs: List[Tuple[Market, Market, float]] = []
    for ma in markets_a:
        ta = normalize_title(ma.title)
        for mb in markets_b:
            if not _ends_close(ma.ends_at, mb.ends_at, ends_tol_seconds):
                continue
            shared = ta & normalize_title(mb.title)
            if len(shared) < min_shared:
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
