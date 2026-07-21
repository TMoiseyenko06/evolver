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
from typing import FrozenSet


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
