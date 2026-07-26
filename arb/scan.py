"""Live scanning: list markets, fetch order books, detect arbs. Read-only.

Uses the unified ``GET /api/v1/markets`` (both venues) + the batch orderbook
endpoint. Nothing here places an order — it only measures whether real, executable
arbs exist after fees, which is the prerequisite for ever risking capital.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

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


def list_all(client: SynthesisClient, venue: str, max_markets: int = 1000,
             page: int = 250, live: bool = True) -> List[Market]:
    """Page through the unified markets endpoint for one venue."""
    markets: List[Market] = []
    offset = 0
    while len(markets) < max_markets:
        try:
            payload = synthesis.list_markets(client, venue=venue, limit=page,
                                             offset=offset, live=live, sort="volume")
        except Exception as exc:  # noqa: BLE001
            log.warning("list_markets(%s, offset=%d) failed: %s", venue, offset, exc)
            break
        batch = [m for m in detect.parse_markets(payload, venue_hint=venue) if not m.resolved]
        markets.extend(batch)
        got = len(detect.parse_markets(payload, venue_hint=venue))
        offset += page
        if got == 0:
            break
    return markets[:max_markets]


def scan_intra(client: SynthesisClient, venue: str, max_markets: int = 1000,
               min_edge: float = 0.0) -> List[ArbOpportunity]:
    """Scan a whole venue for single-market both-sides arbs (ask_A + ask_B < 1)."""
    markets = list_all(client, venue, max_markets)
    log.info("%s: %d live markets", venue, len(markets))
    books = fetch_books(client, [t for m in markets for t in m.token_ids])
    detect.apply_orderbooks(markets, books)
    opps = [o for m in markets if (o := detect.intra_market_arb(m, min_edge)) is not None]
    opps.sort(key=lambda o: o.edge, reverse=True)
    return opps


def scan_cross(client: SynthesisClient, max_markets: int = 1000, min_similarity: float = 0.6,
               min_edge: float = 0.0) -> List[Tuple[ArbOpportunity, float]]:
    """Match events across Polymarket+Kalshi and scan each pair for a cross-venue lock.

    Returns ``(opportunity, title_similarity)`` so a human can gauge how confident
    the event-match is before trusting the arb.
    """
    poly = list_all(client, "polymarket", max_markets)
    kal = list_all(client, "kalshi", max_markets)
    log.info("matching %d polymarket vs %d kalshi markets", len(poly), len(kal))
    pairs = detect.match_events(poly, kal, min_similarity)
    log.info("%d candidate event matches (sim >= %.2f)", len(pairs), min_similarity)
    tokens: List[str] = []
    for ma, mb, _ in pairs:
        tokens.extend(ma.token_ids + mb.token_ids)
    books = fetch_books(client, tokens)
    detect.apply_orderbooks(poly + kal, books)
    results: List[Tuple[ArbOpportunity, float]] = []
    for ma, mb, sim in pairs:
        opp = detect.cross_venue_arb([ma, mb], min_edge)
        if opp is not None and opp.kind == "cross":
            results.append((opp, sim))
    results.sort(key=lambda r: r[0].edge, reverse=True)
    return results
