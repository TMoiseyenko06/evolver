"""Unit tests for the arbitrage detection math (pure, deterministic)."""

import pytest

from arb import detect, fees
from arb.model import Market, Quote
from arb.scan import parse_orderbooks


def _markets_payload(venue, cid, title, yes_p, no_p, ends="1700000000"):
    return {"success": True, "response": [{
        "venue": venue, "event_id": "e_" + cid,
        "markets": [{
            "condition_id": cid, "event_id": "e_" + cid, "title": title,
            "left_outcome": "Yes", "right_outcome": "No",
            "left_token_id": cid + "_y", "right_token_id": cid + "_n",
            "left_price": yes_p, "right_price": no_p,
            "ends_at": ends, "resolved": False, "liquidity": 100, "volume": 1000,
        }],
    }]}


def _book(token, ask, bid, size=100):
    return {"token_id": token, "orderbook": {"asks": {str(ask): size}, "bids": {str(bid): size}}}


# --------------------------------------------------------------------------- #
def test_parse_markets_normalizes_both_venues():
    m = detect.parse_markets(_markets_payload("kalshi", "k1", "Will it rain?", 0.6, 0.4))
    assert len(m) == 1
    assert m[0].venue == "kalshi" and m[0].market_id == "k1"
    assert {q.outcome for q in m[0].quotes} == {"Yes", "No"}
    assert m[0].quote("Yes").mid == pytest.approx(0.6)


def test_parse_orderbooks_maps_tokens():
    payload = {"success": True, "response": [_book("t1", 0.55, 0.50), _book("t2", 0.40, 0.35)]}
    books = parse_orderbooks(payload)
    assert books["t1"]["asks"][0] == (0.55, 100)
    assert books["t2"]["bids"][0] == (0.35, 100)


def test_fee_models():
    assert fees.fee_per_share("polymarket", 0.5) == pytest.approx(0.0312 * 0.5)
    assert fees.fee_per_share("kalshi", 0.5) == pytest.approx(0.07 * 0.25)
    assert fees.fee_per_share("kalshi", 0.05) < fees.fee_per_share("kalshi", 0.5)  # cheaper at extremes
    assert fees.fee_per_share("unknown", 0.5) == 0.0


# --------------------------------------------------------------------------- #
def test_intra_market_arb_detected_when_sides_sum_below_one():
    markets = detect.parse_markets(_markets_payload("polymarket", "c1", "X?", 0.6, 0.4))
    detect.apply_orderbooks(markets, parse_orderbooks(
        {"response": [_book("c1_y", 0.55, 0.50), _book("c1_n", 0.40, 0.35)]}))
    opp = detect.intra_market_arb(markets[0])
    assert opp is not None
    assert opp.kind == "intra"
    assert opp.gross_cost == pytest.approx(0.95)
    # fees = 0.0312*0.45 + 0.0312*0.40
    assert opp.fees == pytest.approx(0.0312 * 0.45 + 0.0312 * 0.40)
    assert opp.edge == pytest.approx(1 - 0.95 - opp.fees)
    assert opp.edge > 0
    assert opp.max_size == 100


def test_intra_market_no_arb_when_sides_sum_above_one():
    markets = detect.parse_markets(_markets_payload("polymarket", "c2", "X?", 0.6, 0.45))
    detect.apply_orderbooks(markets, parse_orderbooks(
        {"response": [_book("c2_y", 0.60, 0.55), _book("c2_n", 0.45, 0.40)]}))
    assert detect.intra_market_arb(markets[0]) is None  # 1.05 gross -> no lock


def test_cross_venue_arb_picks_cheapest_side_per_venue():
    poly = detect.parse_markets(_markets_payload("polymarket", "p1", "Team wins title?", 0.6, 0.45))
    kal = detect.parse_markets(_markets_payload("kalshi", "k1", "Team wins the title?", 0.62, 0.42))
    books = parse_orderbooks({"response": [
        _book("p1_y", 0.55, 0.50), _book("p1_n", 0.48, 0.44),
        _book("k1_y", 0.60, 0.55), _book("k1_n", 0.40, 0.36),
    ]})
    detect.apply_orderbooks(poly + kal, books)
    opp = detect.cross_venue_arb([poly[0], kal[0]])
    assert opp is not None and opp.kind == "cross"
    # cheapest Yes = poly 0.55, cheapest No = kalshi 0.40
    assert opp.gross_cost == pytest.approx(0.95)
    assert {leg.venue for leg in opp.legs} == {"polymarket", "kalshi"}
    assert opp.edge > 0


def test_cross_venue_arb_requires_matching_outcome_labels():
    # Up/Down vs Yes/No must NOT be auto-mapped (ambiguous direction).
    a = Market("polymarket", "a", "e", "BTC up?", None, False,
               [Quote("Up", "ay", ask=0.4), Quote("Down", "an", ask=0.4)])
    b = Market("kalshi", "b", "e", "BTC up?", None, False,
               [Quote("Yes", "by", ask=0.4), Quote("No", "bn", ask=0.4)])
    assert detect.cross_venue_arb([a, b]) is None


# --------------------------------------------------------------------------- #
def test_title_similarity_and_matching():
    assert detect.title_similarity("Will the Lakers win the title?",
                                   "Lakers win title") > 0.6
    assert detect.title_similarity("Will it rain in NYC?", "Fed raises rates") == 0.0

    poly = detect.parse_markets(_markets_payload("polymarket", "p", "Lakers win the 2025 title?", 0.5, 0.5))
    kal = detect.parse_markets(_markets_payload("kalshi", "k", "Will Lakers win 2025 title?", 0.5, 0.5))
    pairs = detect.match_events(poly, kal, min_similarity=0.5)
    assert len(pairs) == 1
    assert pairs[0][0].venue == "polymarket" and pairs[0][1].venue == "kalshi"


def test_match_events_excludes_far_apart_end_times():
    poly = detect.parse_markets(_markets_payload("polymarket", "p", "Same event today?", 0.5, 0.5, ends="1700000000"))
    kal = detect.parse_markets(_markets_payload("kalshi", "k", "Same event today?", 0.5, 0.5, ends="1700100000"))
    # ~27h apart, tol 1h -> excluded
    assert detect.match_events(poly, kal, min_similarity=0.4, ends_tol_seconds=3600) == []
