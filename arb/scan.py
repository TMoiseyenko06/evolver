"""Live scanning: list markets, fetch order books, detect arbs. Read-only.

Uses the unified ``GET /api/v1/markets`` (both venues) + the batch orderbook
endpoint. Nothing here places an order — it only measures whether real, executable
arbs exist after fees, which is the prerequisite for ever risking capital.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from polybot import synthesis
from polybot.synthesis import SynthesisClient

from . import detect
from .model import ArbOpportunity, Market

log = logging.getLogger("arb.scan")


def parse_orderbooks(payload) -> Dict[str, dict]:
    """Map token_id -> {asks, bids} from a batch orderbook response."""
    out: Dict[str, dict] = {}
    for entry in synthesis._as_list(synthesis._unwrap(payload)):
        if not isinstance(entry, dict):
            continue
        tok = str(entry.get("token_id") or entry.get("tokenId")
                  or entry.get("asset_id") or entry.get("id") or "")
        if tok:
            out[tok] = synthesis.parse_orderbook(entry)
    return out


def fetch_books(client: SynthesisClient, token_ids: List[str], batch: int = 100) -> Dict[str, dict]:
    """Batch-fetch order books for many tokens, chunked to keep requests sane."""
    books: Dict[str, dict] = {}
    uniq = [t for t in dict.fromkeys(token_ids) if t]
    for i in range(0, len(uniq), batch):
        chunk = uniq[i:i + batch]
        try:
            books.update(parse_orderbooks(synthesis.fetch_orderbooks(client, chunk)))
        except Exception as exc:  # noqa: BLE001
            log.warning("orderbook batch failed (%d tokens): %s", len(chunk), exc)
    return books


def list_all(client: SynthesisClient, venue: str, max_markets: int = 20000,
             page: int = 250, live: Optional[bool] = None) -> List[Market]:
    """Page through the unified markets endpoint for one venue, to exhaustion.

    Arbs live in THIN, low-volume markets (a mispriced $3 market is where the edge
    is), so we page all the way down the tail rather than stopping at the
    high-volume top. ``live`` defaults to None (no in-play filter) so future-dated
    markets — elections, year-end crypto — are included.
    """
    markets: List[Market] = []
    offset = 0
    while len(markets) < max_markets:
        try:
            payload = synthesis.list_markets(client, venue=venue, limit=page,
                                             offset=offset, live=live, sort="volume")
        except Exception as exc:  # noqa: BLE001
            log.warning("list_markets(%s, offset=%d) failed: %s", venue, offset, exc)
            break
        parsed = detect.parse_markets(payload, venue_hint=venue)
        markets.extend(m for m in parsed if not m.resolved)
        offset += page
        if len(parsed) < page:  # short page => end of the listing
            break
    return markets[:max_markets]


def scan_intra(client: SynthesisClient, venue: str, max_markets: int = 1000,
               min_edge: float = 0.0, realistic: bool = True) -> List[ArbOpportunity]:
    """Scan a whole venue for single-market both-sides arbs (ask_A + ask_B < 1).

    With ``realistic`` (default), uses executable asks (cross-book no-arb model), so
    phantom cheap asks don't manufacture arbs — the honest data the paper trader needs.
    """
    markets = list_all(client, venue, max_markets)
    log.info("%s: %d live markets", venue, len(markets))
    books = fetch_books(client, [t for m in markets for t in m.token_ids])
    detect.apply_orderbooks(markets, books, realistic=realistic)
    opps = [o for m in markets if (o := detect.intra_market_arb(m, min_edge, realistic)) is not None]
    opps.sort(key=lambda o: o.edge, reverse=True)
    return opps


def scan_field(client: SynthesisClient, venue: str, max_markets: int = 1000,
               min_edge: float = 0.0, realistic: bool = True) -> List[ArbOpportunity]:
    """Scan a venue for multi-outcome 'field' locks (buy YES on every outcome < $1).

    Groups markets by event (e.g. all golfers in a tournament) and checks the field
    sum. Uses executable asks; guards against incomplete fields via the mid-sum
    heuristic. Results are HUMAN-CONFIRM candidates (verify the field is exhaustive).
    """
    markets = list_all(client, venue, max_markets)
    groups = detect.group_by_event(markets)
    log.info("%s: %d markets in %d events", venue, len(markets), len(groups))
    all_tokens = [t for m in markets for t in m.token_ids]
    books = fetch_books(client, all_tokens)
    detect.apply_orderbooks(markets, books, realistic=realistic)
    opps = [o for g in groups.values() if (o := detect.field_arb(g, min_edge, realistic)) is not None]
    opps.sort(key=lambda o: o.edge, reverse=True)
    return opps


def scan_cross(client: SynthesisClient, max_markets: int = 20000, min_shared: int = 2,
               min_edge: float = 0.0, max_pairs: int = 4000, min_similarity: float = 0.0,
               ) -> List[Tuple[ArbOpportunity, float]]:
    """Scan the FULL universe for cross-venue locks (Kalshi YES + Polymarket NO < $1).

    Fetches both venues to exhaustion (arbs hide in thin, low-volume markets), blocks
    plausible pairs with an inverted index (scales), fetches books only for those
    pairs, and reports pairs with a real executable spread. Returns
    ``(opportunity, block_score)`` — the block score is a rough match confidence;
    confirm the two markets are truly the same question before trading.
    """
    poly = list_all(client, "polymarket", max_markets)
    kal = list_all(client, "kalshi", max_markets)
    log.info("scanning %d polymarket vs %d kalshi markets", len(poly), len(kal))
    pairs = detect.candidate_pairs(poly, kal, min_shared=min_shared, max_pairs=max_pairs)
    pairs = [p for p in pairs if p[2] >= min_similarity]
    log.info("%d candidate pairs (>=%d shared words)", len(pairs), min_shared)
    tokens: List[str] = []
    for ma, mb, _, _ in pairs:
        tokens.extend(ma.token_ids + mb.token_ids)
    books = fetch_books(client, tokens)
    detect.apply_orderbooks(poly + kal, books)
    results: List[Tuple[ArbOpportunity, float]] = []
    for ma, mb, score, _ in pairs:
        opp = detect.cross_venue_arb([ma, mb], min_edge)
        if opp is not None and opp.kind == "cross":
            results.append((opp, score))
    results.sort(key=lambda r: r[0].edge, reverse=True)
    return results
