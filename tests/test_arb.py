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
        {"response": [_book("c1_y", 0.55, 0.50), _book("c1_n", 0.40, 0.35)]}), realistic=False)
    opp = detect.intra_market_arb(markets[0], realistic=False)  # pure displayed-ask math
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
    detect.apply_orderbooks(poly + kal, books, realistic=False)
    opp = detect.cross_venue_arb([poly[0], kal[0]], realistic=False)  # pure displayed-ask math
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
def _field_market(cid, title, yes_ask, yes_mid, size=100):
    m = detect.parse_markets(_markets_payload("kalshi", cid, title, yes_mid, 1 - yes_mid))[0]
    m.event_id = "TOURNEY"
    # No bid kept low enough (1 - yes_ask + margin) that the no-arb floor does NOT lift
    # the YES ask, so the executable ask equals the displayed one for a clean test.
    no_bid = round(1 - yes_ask + 0.05, 3)
    detect.apply_orderbooks([m], parse_orderbooks({"response": [
        {"token_id": cid + "_y", "orderbook": {"asks": {str(yes_ask): size}, "bids": {"0.01": size}}},
        {"token_id": cid + "_n", "orderbook": {"asks": {"0.99": size}, "bids": {str(no_bid): size}}},
    ]}), realistic=True)
    return m


def test_field_arb_locks_when_field_sums_below_one():
    # Three-outcome field, YES asks 0.30+0.30+0.30 = 0.90 < 1 -> lock. Mids sum ~1.
    field = [
        _field_market("g1", "Open Winner - A", 0.30, 0.34),
        _field_market("g2", "Open Winner - B", 0.30, 0.33),
        _field_market("g3", "Open Winner - C", 0.30, 0.33),
    ]
    opp = detect.field_arb(field, min_edge=0.0)
    assert opp is not None and opp.kind == "field"
    assert len(opp.legs) == 3
    assert opp.gross_cost == pytest.approx(0.90)
    assert opp.edge > 0
    assert opp.sum_mid == pytest.approx(1.0, abs=0.02)


def test_field_arb_none_when_field_sums_above_one():
    field = [
        _field_market("h1", "Open Winner - A", 0.40, 0.40),
        _field_market("h2", "Open Winner - B", 0.40, 0.40),
        _field_market("h3", "Open Winner - C", 0.40, 0.40),
    ]  # Σ ask 1.20 -> no lock
    assert detect.field_arb(field, min_edge=0.0) is None


def test_field_arb_rejects_incomplete_field():
    # Only two thirds of the field present -> Σ mid ~0.66 < mid_low -> rejected even
    # though Σ ask (0.30+0.30=0.60) is under $1 (a missing outcome could win -> $0).
    field = [
        _field_market("i1", "Open Winner - A", 0.30, 0.33),
        _field_market("i2", "Open Winner - B", 0.30, 0.33),
    ]
    assert detect.field_arb(field, min_edge=0.0, min_outcomes=2) is None


def test_group_by_event():
    ms = (detect.parse_markets(_markets_payload("kalshi", "a", "X - A", 0.5, 0.5)) +
          detect.parse_markets(_markets_payload("kalshi", "b", "X - B", 0.5, 0.5)))
    for m in ms:
        m.event_id = "E1"
    groups = detect.group_by_event(ms)
    assert list(groups) == ["kalshi:E1"] and len(groups["kalshi:E1"]) == 2


def test_title_similarity_and_matching():
    assert detect.title_similarity("Will the Lakers win the title?",
                                   "Lakers win title") > 0.6
    assert detect.title_similarity("Will it rain in NYC?", "Fed raises rates") == 0.0

    poly = detect.parse_markets(_markets_payload("polymarket", "p", "Lakers win the 2025 title?", 0.5, 0.5))
    kal = detect.parse_markets(_markets_payload("kalshi", "k", "Will Lakers win 2025 title?", 0.5, 0.5))
    pairs = detect.match_events(poly, kal, min_similarity=0.5)
    assert len(pairs) == 1
    assert pairs[0][0].venue == "polymarket" and pairs[0][1].venue == "kalshi"


def test_realistic_fills_kill_phantom_arb():
    # Displayed Yes ask 0.06 + No ask 0.40 = 0.46 looks like a huge arb, but the
    # complement (No) is bid at 0.90, so Yes can't execute below 1-0.90 = 0.10... and
    # in fact the sibling bids clamp both sides up until the fake arb disappears.
    markets = detect.parse_markets(_markets_payload("polymarket", "c9", "X?", 0.5, 0.5))
    # Yes: ask 0.06 (phantom), bid 0.05 ; No: ask 0.40, bid 0.90
    books = parse_orderbooks({"response": [
        {"token_id": "c9_y", "orderbook": {"asks": {"0.06": 100}, "bids": {"0.05": 100}}},
        {"token_id": "c9_n", "orderbook": {"asks": {"0.40": 100}, "bids": {"0.90": 100}}},
    ]})
    detect.apply_orderbooks(markets, books, realistic=True)
    m = markets[0]
    # Yes executable ask floored by 1 - No_bid(0.90) = 0.10, not the phantom 0.06.
    assert m.quote("Yes").ask_exec == pytest.approx(0.10)
    # On DISPLAYED asks it would look like an arb; on executable asks it must not.
    fake = detect.intra_market_arb(m, realistic=False)
    real = detect.intra_market_arb(m, realistic=True)
    assert fake is not None and fake.edge > 0      # phantom "arb"
    assert real is None                            # gone once fills are realistic


def test_paper_enter_and_resolve_locks_edge():
    from arb.model import ArbLeg, ArbOpportunity
    from arb.paper import PaperBook, _maybe_enter, _resolve_matured

    book = PaperBook(start_bankroll=500.0, bankroll=500.0)
    # net_cost 0.95 -> edge 0.05/set; 100 shares available.
    opp = ArbOpportunity(
        kind="intra", title="Both sides cheap?",
        legs=[ArbLeg("polymarket", "m1", "t", "Yes", "ty", 0.55, 100),
              ArbLeg("polymarket", "m1", "t", "No", "tn", 0.40, 100)],
        gross_cost=0.95, fees=0.0, net_cost=0.95, edge=0.05, max_size=100, ends_at="0",
    )
    _maybe_enter(book, opp, per_arb_cap=100)
    assert book.n_taken == 1
    pos = next(iter(book.open.values()))
    assert pos.shares == 100 and pos.cost == pytest.approx(95.0)
    assert book.bankroll == pytest.approx(405.0)
    assert pos.locked_pnl == pytest.approx(5.0)

    # ends_at=0 (epoch) is far in the past -> matures -> payout=shares, pnl=edge*shares.
    _resolve_matured(book, settle_delay=0.0, max_hold=0.0)
    assert not book.open
    assert book.realized_pnl == pytest.approx(5.0)
    assert book.bankroll == pytest.approx(505.0)  # 405 + 100 payout


def test_paper_skips_when_no_edge():
    from arb.model import ArbLeg, ArbOpportunity
    from arb.paper import PaperBook, _maybe_enter

    book = PaperBook(start_bankroll=500.0, bankroll=500.0)
    opp = ArbOpportunity("intra", "no edge", [
        ArbLeg("polymarket", "m2", "t", "Yes", "ty", 0.60, 100),
        ArbLeg("polymarket", "m2", "t", "No", "tn", 0.45, 100)],
        gross_cost=1.05, fees=0.0, net_cost=1.05, edge=-0.05, max_size=100)
    _maybe_enter(book, opp, per_arb_cap=100)
    assert book.n_taken == 0 and not book.open


def test_match_events_excludes_far_apart_end_times():
    poly = detect.parse_markets(_markets_payload("polymarket", "p", "Same event today?", 0.5, 0.5, ends="1700000000"))
    kal = detect.parse_markets(_markets_payload("kalshi", "k", "Same event today?", 0.5, 0.5, ends="1700100000"))
    # ~27h apart, tol 1h -> excluded
    assert detect.match_events(poly, kal, min_similarity=0.4, ends_tol_seconds=3600) == []


# --------------------------------------------------------------------------- #
# Cross-venue semantic matching
# --------------------------------------------------------------------------- #
def _mkt(venue, cid, title):
    return detect.parse_markets(_markets_payload(venue, cid, title, 0.5, 0.5))[0]


def test_candidate_pairs_blocks_on_shared_words():
    from arb import match
    poly = [_mkt("polymarket", "p1", "Will the Lakers win the 2025 NBA title?"),
            _mkt("polymarket", "p2", "Will it rain in Seattle tomorrow?")]
    kalshi = [_mkt("kalshi", "k1", "Lakers 2025 championship winner"),
              _mkt("kalshi", "k2", "Fed rate decision September")]
    pairs = match.candidate_pairs(poly, kalshi, min_shared=2)
    # Only the Lakers pair shares >=2 significant words.
    assert len(pairs) == 1
    assert pairs[0][0].market_id == "p1" and pairs[0][1].market_id == "k1"


def test_parse_match_response_tolerant():
    from arb import match
    txt = 'sure!\n[{"i":0,"match":true,"confidence":0.9,"map":{"Yes":"Yes"},"reason":"same"}]\ndone'
    out = match.parse_match_response(txt)
    assert out == [{"i": 0, "match": True, "confidence": 0.9, "map": {"Yes": "Yes"}, "reason": "same"}]
    assert match.parse_match_response("no json here") == []


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, system, user):
        self.calls += 1
        return self.reply


def test_llm_confirm_keeps_confident_matches():
    from arb import match
    poly = [_mkt("polymarket", "p1", "Lakers win title?"),
            _mkt("polymarket", "p2", "Bitcoin above 100k?")]
    kalshi = [_mkt("kalshi", "k1", "Lakers championship"),
              _mkt("kalshi", "k2", "BTC over 100000 at CME close")]
    pairs = [(poly[0], kalshi[0], 0.8, 2), (poly[1], kalshi[1], 0.7, 2)]
    # LLM confirms pair 0, rejects pair 1 (different settlement source).
    llm = _FakeLLM('[{"i":0,"match":true,"confidence":0.85,"map":{"Yes":"Yes","No":"No"}},'
                   '{"i":1,"match":false,"confidence":0.9,"reason":"CME vs unknown source"}]')
    matches = match.llm_confirm(llm, pairs, batch=15, min_confidence=0.6)
    assert len(matches) == 1
    assert matches[0].poly.market_id == "p1" and matches[0].confidence == 0.85
    assert llm.calls == 1


def test_match_venues_fuzzy_fallback_without_llm(monkeypatch):
    from arb import match
    poly = [_mkt("polymarket", "p1", "Lakers win the 2025 title")]
    kalshi = [_mkt("kalshi", "k1", "Lakers 2025 title winner")]
    monkeypatch.setattr(match, "fetch_broad",
                        lambda client, venue, target=3000: poly if venue == "polymarket" else kalshi)
    out = match.match_venues(client=None, llm=None)
    assert len(out) == 1 and "fuzzy" in out[0].reason


# --------------------------------------------------------------------------- #
# IDF-weighted / Levenshtein matching (distinguishes outcomes of the same event)
# --------------------------------------------------------------------------- #
def test_levenshtein_and_ratio():
    assert detect.levenshtein("warsh", "warsh") == 0
    assert detect.levenshtein("powell", "powel") == 1
    assert detect.lev_ratio("Kevin Warsh", "kevin warsh") == pytest.approx(1.0)
    assert detect.lev_ratio("Warsh", "Powell") < 0.5


def test_idf_matching_does_not_pair_different_candidates():
    poly = [_mkt("polymarket", "p_warsh", "Fed Chair - Kevin Warsh"),
            _mkt("polymarket", "p_powell", "Fed Chair - Jerome Powell")]
    kalshi = [_mkt("kalshi", "k_warsh", "Fed Chair Kevin Warsh"),
              _mkt("kalshi", "k_powell", "Fed Chair Jerome Powell")]
    filler = [_mkt("kalshi", f"f{i}", "Fed Chair Candidate nominee") for i in range(10)]
    idf = detect.token_idf(poly + kalshi + filler)
    pairs = detect.candidate_pairs(poly, kalshi + filler, min_shared=2, idf=idf)
    got = {(a.market_id, b.market_id) for a, b, _, _ in pairs}
    # Correct same-name pairs are found...
    assert ("p_warsh", "k_warsh") in got
    assert ("p_powell", "k_powell") in got
    # ...but different candidates of the same event are NOT paired (only shared "fed/chair").
    assert ("p_warsh", "k_powell") not in got
    assert ("p_powell", "k_warsh") not in got
