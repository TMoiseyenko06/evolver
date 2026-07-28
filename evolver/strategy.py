"""The runtime wrapper around one compiled strategy.

A :class:`LoadedStrategy` owns:
  * identity + provenance (name, description, source, hash, lineage, gen created);
  * a live bankroll and two :class:`~evolver.models.Stats` — one accumulating over
    the whole lifetime, one reset each generation;
  * safe execution of ``decide`` under a hard timeout, with failure counting and
    auto-retirement after ``max_failures`` raises/timeouts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .context import Ctx
from .models import Stats
from .sandbox import SandboxError, StrategyTimeout, compile_strategy, run_with_timeout


def content_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass
class LoadedStrategy:
    name: str
    description: str
    source: str
    generation_created: int
    lineage: List[str] = field(default_factory=list)
    source_hash: str = ""

    # runtime state
    bankroll: float = 0.0
    generations_survived: int = 0
    failure_count: int = 0
    retired: bool = False
    retired_reason: Optional[str] = None
    entered_this_window: bool = False
    last_error: Optional[str] = None

    lifetime: Stats = field(default_factory=Stats)
    gen: Stats = field(default_factory=Stats)

    _instance: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not self.source_hash:
            self.source_hash = content_hash(self.source)

    # --- construction ----------------------------------------------------- #
    @classmethod
    def create(
        cls,
        source: str,
        generation_created: int,
        config: Config,
        lineage: Optional[List[str]] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> "LoadedStrategy":
        """Compile ``source`` in the sandbox and build a ready-to-run strategy.

        Raises :class:`~evolver.sandbox.SandboxError` if the source is unsafe or
        does not expose the required contract.
        """
        instance = compile_strategy(source, config.allowed_imports)
        resolved_name = name or getattr(instance, "NAME", None)
        if not resolved_name or not isinstance(resolved_name, str):
            raise SandboxError("Strategy is missing a string NAME attribute")
        resolved_desc = description or getattr(instance, "DESCRIPTION", "") or ""
        strat = cls(
            name=resolved_name,
            description=str(resolved_desc),
            source=source,
            generation_created=generation_created,
            lineage=list(lineage or []),
            bankroll=config.starting_bankroll,
        )
        strat._instance = instance
        return strat

    def bind(self, config: Config) -> None:
        """(Re)compile the instance — used when loading from persistence."""
        self._instance = compile_strategy(self.source, config.allowed_imports)

    # --- per-generation lifecycle ----------------------------------------- #
    def start_generation(self) -> None:
        self.gen = Stats()

    def start_window(self) -> None:
        self.entered_this_window = False

    # --- decision --------------------------------------------------------- #
    def decide(self, ctx: Ctx, timeout: float, max_failures: int = 3) -> Optional[dict]:
        """Run ``decide`` under a timeout; returns a validated action or None.

        On any exception or timeout the failure is counted and the strategy is
        auto-retired once it reaches ``max_failures``. Returns None (a pass) on
        failure so the window can proceed.
        """
        if self.retired or self._instance is None:
            return None
        try:
            result = run_with_timeout(self._instance.decide, (ctx,), timeout)
        except StrategyTimeout as exc:
            self._register_failure(f"timeout: {exc}", max_failures)
            return None
        except BaseException as exc:  # noqa: BLE001 — untrusted code, catch everything
            self._register_failure(f"exception: {type(exc).__name__}: {exc}", max_failures)
            return None
        return self._validate_action(result, max_failures)

    def _validate_action(self, result, max_failures: int) -> Optional[dict]:
        if result is None:
            return None
        if isinstance(result, dict) and result.get("side") in ("Up", "Down"):
            action = {"side": result["side"]}
            lim = result.get("limit")
            if isinstance(lim, (int, float)) and not isinstance(lim, bool) and 0.0 < lim <= 1.0:
                action["limit"] = float(lim)  # optional LIMIT price cap
            return action
        # Malformed return value counts as a failure — the contract was violated.
        self._register_failure(f"invalid decide() return: {result!r}", max_failures)
        return None

    def _register_failure(self, reason: str, max_failures: int) -> None:
        self.failure_count += 1
        self.last_error = reason
        if self.failure_count >= max_failures:
            self.retire(f"auto-retired after {self.failure_count} failures ({reason})")

    def retire(self, reason: str) -> None:
        self.retired = True
        self.retired_reason = reason
