"""Configuration for the evolver run.

All knobs live here so tests can shrink the generation (fewer windows, smaller
population) and swap the model without touching the engine. The OpenRouter key
comes from the ``OPENROUTER_API_KEY`` environment variable; the model slug is
configurable and defaults to an Anthropic Claude model on OpenRouter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, Optional


@dataclass
class Config:
    # --- OpenRouter ---
    openrouter_api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Default to an Anthropic Claude model slug; override via env or config.
    model: str = field(default_factory=lambda: os.environ.get("EVOLVER_MODEL", "anthropic/claude-opus-4.8"))

    # --- evolution parameters ---
    population_size: int = 10
    survivors: int = 5
    windows_per_generation: int = 50
    starting_bankroll: float = 500.0
    stake: float = 10.0

    # --- live polling ---
    poll_interval_seconds: int = 10
    window_seconds: int = 300
    # Print a per-strategy status board after every resolved 5-minute window.
    live_window_reports: bool = True
    # A guaranteed final poll lands this many seconds before window close; every
    # earlier interval is exactly `poll_interval_seconds` (drift-free, anchored).
    final_poll_lead_seconds: float = 2.0

    # --- websocket market feed (live run) ---
    use_websocket: bool = True
    pm_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"
    # A WS value older than this (or a disconnected stream) triggers a REST fallback.
    ws_staleness_seconds: float = 5.0

    # --- sandbox / safety ---
    decide_timeout_seconds: float = 1.0
    max_failures: int = 3
    allowed_imports: FrozenSet[str] = frozenset({"math", "statistics"})

    # --- diversity ---
    duplicate_threshold: float = 0.90  # reject a new strategy >90% identical
    diversity_lookback_windows: int = 50
    max_repair_attempts: int = 1
    max_diversity_retries: int = 2

    # --- market ---
    product: str = "BTC-USD"
    # Resolution: the official Polymarket outcome is authoritative and we WAIT for
    # it (these 5-min markets do NOT reliably match the Coinbase 5m candle, which
    # is only a diagnostic). `resolution_timeout_seconds=None` waits indefinitely;
    # set a number to cap the wait and fall back to the Coinbase estimate after it.
    resolution_timeout_seconds: Optional[float] = None
    resolution_poll_seconds: float = 10.0
    resolution_heartbeat_seconds: float = 60.0

    # --- live order execution (Synthesis) — used only by `calibrate` ---
    synthesis_api_key: str = field(default_factory=lambda: os.environ.get("SYNTHESIS_API_KEY", ""))
    synthesis_wallet_id: str = field(default_factory=lambda: os.environ.get("SYNTHESIS_WALLET_ID", ""))
    synthesis_base_url: str = field(
        default_factory=lambda: os.environ.get("SYNTHESIS_BASE_URL", "https://synthesis.trade")
    )
    live_stake: float = 1.0        # real USDC per calibration order
    max_live_stake: float = 5.0    # hard safety cap; refuse to place above this
    calibration_trades: int = 12   # default number of real trades to collect
    order_slippage_cap: float = 0.98  # max price to pay on a MARKET buy (0<p<=1)

    # --- paths ---
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("EVOLVER_DATA_DIR", ".")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "evolver.sqlite"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def strategies_dir(self) -> Path:
        return self.data_dir / "strategies"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.strategies_dir.mkdir(parents=True, exist_ok=True)
