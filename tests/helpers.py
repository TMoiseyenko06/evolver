"""Shared test scaffolding: a deterministic mock market, a fake OpenRouter
client, strategy source templates, and a temp Config builder.

These mirror the real interfaces exactly (``MarketProvider`` protocol,
``client.chat(system, user) -> str``) so the same engine/runner code runs offline
with zero network.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from evolver.config import Config
from evolver.market import Resolution, WindowHandle
from evolver.models import PollSnapshot


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def make_config(tmp_path, **overrides) -> Config:
    cfg = Config(
        openrouter_api_key="test-key",
        model="test/model",
        population_size=overrides.pop("population_size", 6),
        survivors=overrides.pop("survivors", 3),
        windows_per_generation=overrides.pop("windows_per_generation", 4),
    )
    cfg.data_dir = tmp_path
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.ensure_dirs()
    return cfg


# --------------------------------------------------------------------------- #
# Strategy source templates
# --------------------------------------------------------------------------- #
def strategy_source(name: str, body: str, lineage: str = "novel", description: str = "") -> str:
    body = textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 8)
    desc = description or f"test strategy {name}"
    return (
        f"# lineage: {lineage}\n"
        f"class Strategy:\n"
        f'    NAME = "{name}"\n'
        f'    DESCRIPTION = "{desc}"\n'
        f"    def decide(self, ctx):\n"
        f"{body}\n"
    )


BODY_ALWAYS_UP = 'return {"side": "Up"}'
BODY_ALWAYS_DOWN = 'return {"side": "Down"}'
BODY_PASS = "return None"
BODY_LATE_UP = """
if ctx.seconds_remaining <= 60:
    return {"side": "Up"}
return None
"""
BODY_MOMENTUM = """
if len(ctx.candles) < 2:
    return None
if ctx.candles[-1]["close"] >= ctx.candles[-2]["close"]:
    return {"side": "Up"}
return {"side": "Down"}
"""
BODY_CRASH = 'raise ValueError("boom")'
BODY_TIMEOUT = """
x = 0
while True:
    x += 1
"""
BODY_BAD_RETURN = 'return {"side": "Sideways"}'


def make_fake_sources(names_bodies) -> List[str]:
    return [strategy_source(n, b, lineage=lin) for (n, b, lin) in names_bodies]


# --------------------------------------------------------------------------- #
# Fake OpenRouter client
# --------------------------------------------------------------------------- #
def _blocks(sources: List[str]) -> str:
    return "\n\n".join(f"```python\n{s}\n```" for s in sources)


@dataclass
class FakeClient:
    """Returns pre-baked responses in order; records prompts for assertions."""

    responses: List[str]
    calls: List[dict] = field(default_factory=list)
    _i: int = 0

    @classmethod
    def from_source_batches(cls, batches: List[List[str]]) -> "FakeClient":
        return cls(responses=[_blocks(b) for b in batches])

    def chat(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if self._i < len(self.responses):
            resp = self.responses[self._i]
            self._i += 1
            return resp
        # Exhausted: return an empty response (no code blocks).
        return "no more strategies"


# --------------------------------------------------------------------------- #
# Mock market
# --------------------------------------------------------------------------- #
@dataclass
class PollSpec:
    poll_index: int
    seconds_remaining: int
    window_open_price: float
    spot: float
    candles: List[dict]
    up_ask: float
    down_ask: float
    depth: float = 1000.0


@dataclass
class WindowSpec:
    window_id: str
    title: str
    coinbase_side: str
    official_side: str
    polls: List[PollSpec]


def simple_poll(poll_index, seconds_remaining, open_price, spot, up_ask, down_ask, candles=None) -> PollSpec:
    return PollSpec(
        poll_index=poll_index,
        seconds_remaining=seconds_remaining,
        window_open_price=open_price,
        spot=spot,
        candles=candles if candles is not None else [
            {"time": 1000, "open": open_price, "close": open_price},
            {"time": 1060, "open": open_price, "close": spot},
        ],
        up_ask=up_ask,
        down_ask=down_ask,
    )


def _snapshot(spec: PollSpec) -> PollSnapshot:
    books = {
        "Up": {"asks": [(spec.up_ask, spec.depth)], "bids": [(max(0.0, spec.up_ask - 0.02), spec.depth)]},
        "Down": {"asks": [(spec.down_ask, spec.depth)], "bids": [(max(0.0, spec.down_ask - 0.02), spec.depth)]},
    }
    return PollSnapshot(
        poll_index=spec.poll_index,
        seconds_remaining=spec.seconds_remaining,
        window_open_price=spec.window_open_price,
        spot=spec.spot,
        candles=[dict(c) for c in spec.candles],
        books=books,
    )


class MockMarket:
    """Cycles through a fixed list of window specs, forever, deterministically."""

    def __init__(self, specs: List[WindowSpec]):
        self.specs = specs
        self._i = 0
        self._current: Optional[WindowSpec] = None

    def next_window(self) -> WindowHandle:
        spec = self.specs[self._i % len(self.specs)]
        self._i += 1
        self._current = spec
        start = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(minutes=5)
        return WindowHandle(
            window_id=spec.window_id,
            condition_id=f"cond_{spec.window_id}",
            title=spec.title,
            start=start,
            end=end,
            token_map={"Up": f"tok_up_{spec.window_id}", "Down": f"tok_down_{spec.window_id}"},
        )

    def poll_snapshots(self, handle: WindowHandle) -> Iterator[PollSnapshot]:
        spec = self._spec_for(handle.window_id)
        for p in spec.polls:
            yield _snapshot(p)

    def resolve(self, handle: WindowHandle) -> Resolution:
        spec = self._spec_for(handle.window_id)
        return Resolution(coinbase_side=spec.coinbase_side, official_side=spec.official_side)

    def _spec_for(self, window_id: str) -> WindowSpec:
        for s in self.specs:
            if s.window_id == window_id:
                return s
        raise KeyError(window_id)


def dense_window_spec(window_id: str, resolved_side: str, polls: List[PollSpec],
                      title: Optional[str] = None) -> WindowSpec:
    """A WindowSpec from a caller-supplied poll sequence (any length/shape).

    ``default_window_specs()`` below only has 2 polls/window (open + late) — enough
    for single-entry strategies, but too sparse to exercise anything that needs
    several intermediate polls (e.g. an early-exit strategy watching for a trigger
    across many polls after entry). This is a generically useful capability for any
    such strategy's tests, not specific to one — the actual scenario data (candle
    shapes, ask/bid sequences) stays local to whichever test file needs it.
    """
    return WindowSpec(
        window_id=window_id,
        title=title or f"Bitcoin Up or Down - dense ({window_id})",
        coinbase_side=resolved_side,
        official_side=resolved_side,
        polls=polls,
    )


def default_window_specs() -> List[WindowSpec]:
    """Four windows: three resolve Up, one resolves Down.

    Each has an open poll (sr=300) and a late poll (sr=30). Asks are ~55c on the
    favored side and ~47c on the other, so 'always up' clearly beats 'always down'
    over these four windows.
    """
    def w(wid, resolved, spot_delta):
        open_price = 50000.0
        spot = open_price + spot_delta
        return WindowSpec(
            window_id=wid,
            title=f"Bitcoin Up or Down - July 20 ({wid})",
            coinbase_side=resolved,
            official_side=resolved,
            polls=[
                simple_poll(0, 300, open_price, spot, up_ask=0.55, down_ask=0.47),
                simple_poll(1, 30, open_price, spot, up_ask=0.56, down_ask=0.46),
            ],
        )

    return [
        w("win_up_1", "Up", +40.0),
        w("win_up_2", "Up", +25.0),
        w("win_down_1", "Down", -30.0),
        w("win_up_3", "Up", +15.0),
    ]
