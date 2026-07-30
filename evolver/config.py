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
    population_size: int = 50
    survivors: int = 25
    windows_per_generation: int = 50
    starting_bankroll: float = 500.0
    stake: float = 10.0

    # --- live polling ---
    poll_interval_seconds: int = 10
    window_seconds: int = 300
    # Print a per-strategy status board after every resolved 5-minute window.
    live_window_reports: bool = True
    # Resolve windows in a background worker so the trader starts the next window
    # immediately instead of blocking on settlement (avoids missing windows).
    overlap_resolution: bool = True
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

    # --- fill realism ---
    # The paper fill walks the DISPLAYED ask book, but displayed liquidity at cheap
    # "longshot" prices is largely phantom/stale: live calibration showed a 0.06
    # displayed fill actually executing near 0.17. Correct it two ways:
    #
    # 1. use_cross_book_fill (preferred, exact): reconstruct the true executable price
    #    per window from the COMPLEMENT side's book via no-arbitrage. On a binary
    #    Up+Down=$1, a maker bidding `cb` for the other side implicitly offers this
    #    side at 1-cb, so the ask can't execute below 1 - best_complement_bid. This is
    #    exact for each window (not an aggregate), needs no fitting, and reproduces the
    #    calibration (Up bid ~0.83 -> Down fills ~0.17). Falls back to (2) when the
    #    complement book is missing.
    # 2. slippage curve (fallback): worsen price by slippage_coeff*(0.5-price)^exp for
    #    sub-0.50 entries. Aggregate approximation used only when (1) can't apply.
    #
    # Both stop the sim (and thus evolver/tuner rankings) from paying strategies for
    # fills the market never gives — the distortion that made longshot strategies look
    # artificially profitable. `calibrate` uses the same model, so its real−paper P&L
    # residual measures accuracy (centre on 0). Set use_cross_book_fill=False and
    # slippage_coeff=0 for the raw idealised walk.
    use_cross_book_fill: bool = True
    slippage_coeff: float = 0.55
    slippage_exp: float = 2.0

    # --- selection / culling ---
    # Survival is ranked by a RISK-ADJUSTED (Sharpe-style) score on LIFETIME stats:
    # mean per-trade P&L divided by its volatility, so consistent earners beat
    # high-variance longshot strategies, and one unlucky generation can't cull a
    # proven strategy. A strategy that never traded scores below everyone.
    risk_vol_floor: float = 2.0     # $ volatility floor: avoids div-by-zero and absurd
                                    # scores from 1-2 identical trades.
    risk_trade_prior: float = 10.0  # small-sample shrinkage: trust ≈ trades/(trades+prior),
                                    # so a couple of lucky trades can't top the board.

    # --- diversity ---
    duplicate_threshold: float = 0.85  # reject a new strategy whose TRADES are >85% identical
    diversity_lookback_windows: int = 50
    max_repair_attempts: int = 1
    max_diversity_retries: int = 2
    # A single OpenRouter reply can under-deliver (invalid/duplicate blocks). Retry
    # breeding up to this many times per generation so the population always refills
    # to population_size instead of silently shrinking.
    max_breed_attempts: int = 3

    # --- market ---
    product: str = "BTC-USD"
    # Resolution: the official Polymarket outcome is authoritative and we WAIT for
    # it (these 5-min markets do NOT reliably match the Coinbase 5m candle, which
    # is only a diagnostic). `resolution_timeout_seconds=None` waits indefinitely;
    # set a number to cap the wait and fall back to the Coinbase estimate after it.
    # Wait this long for the official outcome, then fall back to the Coinbase
    # estimate so one never-resolving market can't freeze the run. Normal
    # settlement is seconds-to-minutes, so this only fires on genuinely stuck
    # markets. Set to None to wait indefinitely.
    resolution_timeout_seconds: Optional[float] = 1200.0
    resolution_poll_seconds: float = 10.0
    resolution_heartbeat_seconds: float = 60.0
    # Resolve windows concurrently so a slow/stuck market doesn't block the rest.
    resolution_workers: int = 6

    # --- live order execution (Synthesis) — used only by `calibrate` ---
    synthesis_api_key: str = field(default_factory=lambda: os.environ.get("SYNTHESIS_API_KEY", ""))
    synthesis_wallet_id: str = field(default_factory=lambda: os.environ.get("SYNTHESIS_WALLET_ID", ""))
    synthesis_base_url: str = field(
        default_factory=lambda: os.environ.get("SYNTHESIS_BASE_URL", "https://synthesis.trade")
    )
    # Pull market discovery + order books from Synthesis (the actual trading
    # venue) instead of Polymarket Gamma/CLOB. Falls back to Gamma if Synthesis
    # returns nothing. Set EVOLVER_MARKET_SOURCE=polymarket to force Gamma.
    use_synthesis_market: bool = field(
        default_factory=lambda: os.environ.get("EVOLVER_MARKET_SOURCE", "synthesis").lower() != "polymarket"
    )
    live_stake: float = 1.0        # real USDC per calibration order
    max_live_stake: float = 5.0    # hard safety cap; refuse to place above this
    calibration_trades: int = 12   # default number of real trades to collect
    # Absolute price cap on a MARKET buy: the order is sent with a guard of
    # `best_ask + max_slippage`, so a thin/moved book can't fill us far above the
    # price the strategy decided on. Live calibration at $5 showed an order intended
    # at 0.490 filling at 0.689 (~20c) under the old blanket 0.98 cap — a badly
    # negative-EV fill. With this set, that order simply doesn't fill and we skip the
    # window. Set to None to disable the dynamic cap and use `order_slippage_cap`.
    max_slippage: Optional[float] = 0.10
    order_slippage_cap: float = 0.98  # fallback max price on a MARKET buy (0<p<=1)

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
