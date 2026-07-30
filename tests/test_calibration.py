"""Offline tests for the paper-vs-real calibration path (mocked Synthesis)."""

import pytest

from polybot.synthesis import OrderResult, SynthesisClient, SynthesisError, parse_fee, parse_order

from evolver.calibrate import load_driver, run_calibration, summarize, write_report
from evolver.execution import PaperExecutor, SynthesisExecutor
from evolver.store import Store

from helpers import (
    MockMarket,
    WindowSpec,
    default_window_specs,
    make_config,
    simple_poll,
)


# --- Synthesis response parsing (pure) ------------------------------------ #
def test_parse_fee_from_object_and_scalar():
    assert parse_fee({"amount": "0.03"}) == pytest.approx(0.03)
    assert parse_fee(0.05) == pytest.approx(0.05)
    assert parse_fee({"a": "0.01", "b": "0.02"}) == pytest.approx(0.03)
    assert parse_fee(None) == 0.0


def test_parse_order_maps_fields():
    o = parse_order({
        "order_id": "abc", "token_id": "123", "side": "BUY", "type": "MARKET",
        "status": "MATCHED", "amount": "1", "filled": "1", "shares": "1.8",
        "price": "0.55", "fee": {"amount": "0.02"},
    })
    assert o.order_id == "abc" and o.matched
    assert o.shares == pytest.approx(1.8) and o.price == pytest.approx(0.55)
    assert o.fee == pytest.approx(0.02)


def test_parse_order_unwraps_response_envelope():
    # Synthesis wraps bodies as {"success":true,"response":{...}} — must unwrap,
    # else shares parse to 0 and a real fill is misread as "did not fill".
    o = parse_order({
        "success": True,
        "response": {
            "order_id": "xyz", "side": "BUY", "type": "MARKET", "status": "MATCHED",
            "amount": "1", "filled": "1", "shares": "2.5", "price": "0.40",
            "fee": {"amount": "0.01"},
        },
    })
    assert o.order_id == "xyz" and o.matched
    assert o.shares == pytest.approx(2.5) and o.price == pytest.approx(0.40)


def test_parse_order_tolerates_alt_field_names():
    o = parse_order({"response": {"id": "q", "state": "MATCHED",
                                  "size": "3", "avg_price": "0.33"}})
    assert o.order_id == "q" and o.shares == pytest.approx(3.0)
    assert o.price == pytest.approx(0.33)


def test_client_refuses_without_credentials():
    client = SynthesisClient(api_key="", wallet_id="")
    with pytest.raises(SynthesisError):
        client.place_market_order("123", "BUY", 1.0)


def test_wallet_path_uses_the_api_host_not_docs_host():
    client = SynthesisClient(api_key="k", wallet_id="W", base_url="https://synthesis.trade")
    assert client._wallet_path("/order") == "https://synthesis.trade/api/v1/wallet/pol/W/order"


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_check_reachable_flags_404_host_error(monkeypatch):
    import polybot.synthesis as syn

    monkeypatch.setattr(syn.requests, "get", lambda *a, **k: _FakeResp(404, text="NOT_FOUND"))
    client = SynthesisClient(api_key="k", wallet_id="W")
    ok, detail = client.check_reachable()
    assert ok is False
    assert "SYNTHESIS_BASE_URL" in detail


def test_check_reachable_ok(monkeypatch):
    import polybot.synthesis as syn

    monkeypatch.setattr(syn.requests, "get", lambda *a, **k: _FakeResp(200, payload={"usdc": "5"}))
    client = SynthesisClient(api_key="k", wallet_id="W")
    ok, detail = client.check_reachable()
    assert ok is True


def test_extract_usdc_balance_nested_sums_usdc_family():
    from polybot.synthesis import extract_usdc_balance

    payload = {
        "success": True,
        "response": {"chain_id": "POL", "balance": {"USDC.e": "1000.000", "USDC": "500.000"}},
    }
    assert extract_usdc_balance(payload) == pytest.approx(1500.0)


def test_extract_usdc_balance_flat_and_missing():
    from polybot.synthesis import extract_usdc_balance

    assert extract_usdc_balance({"balance": {"USDC": "12.5"}}) == pytest.approx(12.5)
    assert extract_usdc_balance({"nope": 1}) is None


def test_extract_usdc_balance_list_of_assets():
    from polybot.synthesis import extract_usdc_balance

    payload = {"response": {"assets": [
        {"symbol": "USDC", "amount": "10"},
        {"symbol": "USDC.e", "amount": "5"},
        {"symbol": "WETH", "amount": "2"},
    ]}}
    assert extract_usdc_balance(payload) == pytest.approx(15.0)


def test_extract_usdc_balance_deeply_nested():
    from polybot.synthesis import extract_usdc_balance

    payload = {"success": True, "response": {"wallet": {"balances": {"USDC.e": "7.25"}}}}
    assert extract_usdc_balance(payload) == pytest.approx(7.25)


def test_parse_markets_builds_5min_windows_with_token_map():
    import datetime as dt
    from polybot.synthesis import parse_markets

    now = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)  # 8:00 AM EDT
    payload = {"success": True, "response": [{
        "markets": [
            {
                "condition_id": "0xabc",
                "question": "Bitcoin Up or Down - July 22, 8:00AM-8:05AM ET",
                "resolved": False,
                "left_outcome": "Up", "left_token_id": "111",
                "right_outcome": "Down", "right_token_id": "222",
            },
            {   # a 15-minute market must be excluded
                "condition_id": "0xdef",
                "question": "Bitcoin Up or Down - July 22, 8:00AM-8:15AM ET",
                "resolved": False,
                "left_outcome": "Up", "left_token_id": "333",
                "right_outcome": "Down", "right_token_id": "444",
            },
        ]
    }]}
    windows = parse_markets(payload, now=now, window_seconds=300)
    assert len(windows) == 1
    w = windows[0]
    assert w.condition_id == "0xabc"
    assert w.token_map == {"Up": "111", "Down": "222"}


def test_parse_markets_skips_resolved():
    import datetime as dt
    from polybot.synthesis import parse_markets

    now = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
    payload = [{"markets": [{
        "condition_id": "0x1", "question": "Bitcoin Up or Down - July 22, 8:00AM-8:05AM ET",
        "resolved": True, "left_outcome": "Up", "left_token_id": "1",
        "right_outcome": "Down", "right_token_id": "2",
    }]}]
    assert parse_markets(payload, now=now) == []


def test_parse_resolution_uses_winner_token_id():
    from polybot.synthesis import parse_resolution

    payload = {"success": True, "response": {"markets": [{
        "condition_id": "0xe9b3",
        "resolved": True,
        "winner_token_id": "222",
        "left_outcome": "Up", "left_token_id": "111",
        "right_outcome": "Down", "right_token_id": "222",
        "left_price": "0.510", "right_price": "0.500",  # stale mid — must be ignored
    }]}}
    assert parse_resolution(payload, "0xe9b3") == "Down"


def test_parse_resolution_none_until_resolved():
    from polybot.synthesis import parse_resolution

    payload = {"response": [{"condition_id": "X", "resolved": False,
                             "left_outcome": "Up", "left_token_id": "1",
                             "right_outcome": "Down", "right_token_id": "2"}]}
    assert parse_resolution(payload, "X") is None


def test_parse_resolution_price_fallback():
    from polybot.synthesis import parse_resolution

    payload = {"response": {"condition_id": "X", "resolved": True,
                            "left_outcome": "Up", "left_token_id": "1",
                            "right_outcome": "Down", "right_token_id": "2",
                            "left_price": "1", "right_price": "0"}}
    assert parse_resolution(payload, "X") == "Up"


def test_parse_orderbook_maps_price_size_dicts():
    from polybot.synthesis import parse_orderbook

    payload = {"response": [{"venue": "polymarket", "orderbook": {
        "token_id": "111",
        "bids": {"0.61": "1000", "0.60": "800"},
        "asks": {"0.63": "600", "0.62": "900"},
    }}]}
    book = parse_orderbook(payload)
    assert book["asks"] == [(0.62, 900.0), (0.63, 600.0)]   # ascending
    assert book["bids"] == [(0.61, 1000.0), (0.60, 800.0)]  # descending


def test_get_balance_parses_nested_response(monkeypatch):
    import polybot.synthesis as syn

    payload = {"success": True, "response": {"balance": {"USDC.e": "1000", "USDC": "500"}}}
    monkeypatch.setattr(syn.requests, "get", lambda *a, **k: _FakeResp(200, payload=payload))
    client = SynthesisClient(api_key="k", wallet_id="W")
    assert client.get_balance() == pytest.approx(1500.0)


# --- Fake Synthesis client + executor ------------------------------------- #
class FakeSynthesisClient:
    def __init__(self, price=0.56, fee=0.01, status="MATCHED", fail=False):
        self.price, self.fee, self.status, self.fail = price, fee, status, fail
        self.calls = []

    def place_market_order(self, token_id, side, usdc, slippage_cap=None):
        self.calls.append((token_id, side, usdc, slippage_cap))
        if self.fail:
            raise SynthesisError("simulated rejection")
        shares = usdc / self.price
        return OrderResult(
            order_id=f"oid{len(self.calls)}", token_id=token_id, side="BUY", type="MARKET",
            status=self.status, amount_usdc=usdc, filled=usdc, shares=shares,
            price=self.price, fee=self.fee, raw={"order_id": f"oid{len(self.calls)}"},
        )

    def get_balance(self):
        return 100.0


def test_synthesis_executor_maps_order_to_fill():
    ex = SynthesisExecutor(client=FakeSynthesisClient(price=0.56, fee=0.01))
    fill = ex.fill("Up", "tok", [(0.55, 100)], 1.0)
    assert fill.side == "Up"
    assert fill.avg_price == pytest.approx(0.56)
    assert fill.shares == pytest.approx(1.0 / 0.56)
    assert fill.fee == pytest.approx(0.01)
    assert ex.last_order.order_id == "oid1"
    # A $1 MARKET buy with the slippage cap was sent.
    assert ex.client.calls[0][2] == 1.0


# --- full calibration loop (mocked market + mocked real fills) ------------ #
def test_calibration_collects_paired_trades(tmp_path):
    cfg = make_config(tmp_path)
    cfg.live_stake = 1.0
    store = Store(cfg)
    driver = load_driver(cfg, store)  # default probe: buys Up every window
    real = SynthesisExecutor(client=FakeSynthesisClient(price=0.56, fee=0.01))
    market = MockMarket(default_window_specs())

    records = run_calibration(market, real, driver, store, cfg, n_trades=5)

    assert len(records) == 5
    for r in records:
        assert r["side"] == "Up"
        assert r["paper_price"] == pytest.approx(0.55)   # paper walks the 0.55 ask
        assert r["real_price"] == pytest.approx(0.56)    # real fill from Synthesis
        assert r["order_id"] is not None
    # Persisted and summarizable.
    assert len(store.calibration_rows()) == 5
    s = summarize(records)
    assert s["n"] == 5
    assert s["mean_abs_price_err"] == pytest.approx(0.01, abs=1e-9)
    # real paid a higher price than paper assumed -> paper is optimistic (bias<0).
    assert s["mean_pnl_bias"] < 0
    path = write_report(cfg, store.calibration_rows())
    assert "Calibration report" in open(path).read()


def test_calibration_skips_windows_with_no_fillable_book(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    driver = load_driver(cfg, store)  # buys Up
    real = SynthesisExecutor(client=FakeSynthesisClient())

    # First window has an EMPTY Up book (no paper fill -> skipped, no real order);
    # second window is normal.
    empty = WindowSpec(
        window_id="empty_up", title="t", coinbase_side="Up", official_side="Up",
        polls=[simple_poll(0, 300, 50000.0, 50040.0, up_ask=0.0, down_ask=0.47)],
    )
    empty.polls[0].up_ask = 0.0
    # Build the empty-Up snapshot by zeroing asks: use depth 0 so book has no asks.
    empty.polls[0].depth = 0.0
    normal = default_window_specs()[0]
    market = MockMarket([empty, normal])

    records = run_calibration(market, real, driver, store, cfg, n_trades=1)
    assert len(records) == 1
    assert records[0]["window_id"] == normal.window_id
    # The real order was only placed for the fillable window.
    assert len(real.client.calls) == 1


def test_calibration_aborts_after_repeated_real_failures(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    driver = load_driver(cfg, store)
    real = SynthesisExecutor(client=FakeSynthesisClient(fail=True))
    market = MockMarket(default_window_specs())

    with pytest.raises(SynthesisError):
        run_calibration(market, real, driver, store, cfg, n_trades=5, max_consecutive_failures=3)
    assert len(store.calibration_rows()) == 0


def test_not_fillable_is_classified_separately():
    """A price-guard rejection must raise OrderNotFillable, not a generic error.

    Otherwise calibrate counts it toward the consecutive-failure abort and a choppy
    stretch (3 guard rejections in a row) would kill an entire run.
    """
    import pytest
    from polybot.synthesis import OrderNotFillable, SynthesisClient, SynthesisError, _is_not_fillable

    assert _is_not_fillable('{"success":false,"response":"Order could not be fully filled"}')
    assert _is_not_fillable("insufficient liquidity for this order")
    assert not _is_not_fillable('{"error":"invalid api key"}')

    class _Resp:
        def __init__(self, code, text):
            self.status_code, self.text = code, text
            self.headers = {}

    import polybot.synthesis as syn
    client = SynthesisClient(api_key="k", wallet_id="w")

    # 400 "could not be fully filled" -> OrderNotFillable (a normal skip)
    syn.requests = type("R", (), {"post": staticmethod(
        lambda *a, **k: _Resp(400, '{"success":false,"response":"Order could not be fully filled"}')),
        "RequestException": Exception})
    with pytest.raises(OrderNotFillable):
        client.place_market_order("t", "BUY", 5.0, 0.59)

    # any other 400 -> plain SynthesisError (a real failure worth aborting on)
    syn.requests = type("R", (), {"post": staticmethod(
        lambda *a, **k: _Resp(400, '{"error":"invalid api key"}')), "RequestException": Exception})
    with pytest.raises(SynthesisError) as ei:
        client.place_market_order("t", "BUY", 5.0, 0.59)
    assert not isinstance(ei.value, OrderNotFillable)
