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


def test_client_refuses_without_credentials():
    client = SynthesisClient(api_key="", wallet_id="")
    with pytest.raises(SynthesisError):
        client.place_market_order("123", "BUY", 1.0)


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
