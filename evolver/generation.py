"""Generation orchestration: run windows, resolve, rank, carry forward, evolve.

This ties the pieces together:
  * :func:`run_window`      — feed one window's polls to every live strategy,
                              log decisions (passes included), simulate fills.
  * :func:`resolve_window`  — score fills against the reconciled outcome.
  * :func:`run_generation`  — do that for N resolved windows.
  * :func:`rank_and_cull`   — top-5 survive (by *this generation's* net P&L),
                              carry bankroll + lifetime stats forward, retire the
                              rest; auto-retired (crashy) strategies always fall
                              to the bottom.
  * :func:`evolve`          — ask OpenRouter for replacements, validate/repair,
                              reject near-duplicates.
  * :func:`run_loop`        — the eternal loop.

WHY per-generation P&L drives culling (documented in README): all strategies see
identical data each generation, so ranking them head-to-head on that shared data
is the fair comparison and gives new strategies a real shot against entrenched
survivors. Lifetime stats + generations-survived (the noise-robust signal the
spec calls out) drive the *leaderboard*, not the cull.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
from typing import Dict, List, Optional, Tuple

from .config import Config
from .context import Ctx
from .engine import score_trade, simulate_fill
from .market import MarketProvider, Resolution, WindowHandle, hash_candles
from .models import Decision, Fill, PollSnapshot, TradeResult, WindowData
from .openrouter import extract_code_blocks, parse_lineage
from .prompts import SYSTEM_PROMPT, evolution_prompt, repair_prompt, seed_prompt
from .replay import action_vector, agreement
from .reporting import format_window_status
from .sandbox import SandboxError
from .strategy import LoadedStrategy
from .store import Store

log = logging.getLogger("evolver")

# Guards strategy P&L/stat mutations shared between the trader thread and the
# background resolution worker.
_stats_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# One window
# --------------------------------------------------------------------------- #
def run_window(
    strategies: List[LoadedStrategy],
    poll_snapshots,
    config: Config,
) -> Tuple[List[Decision], Dict[str, Fill], List[PollSnapshot]]:
    """Run every live strategy across one window's polls.

    Returns (decisions, fills_by_strategy, collected_snapshots). Each strategy
    may enter at most once; after entering (or being retired) it is not called
    again this window. Fills are unscored until resolution.
    """
    for s in strategies:
        s.start_window()

    decisions: List[Decision] = []
    fills: Dict[str, Fill] = {}
    snaps: List[PollSnapshot] = []

    for snap in poll_snapshots:
        snaps.append(snap)
        for strat in strategies:
            if strat.retired or strat.entered_this_window:
                continue
            ctx = Ctx.from_snapshot(snap)  # fresh copy per strategy — untrusted code
            before = strat.failure_count
            action = strat.decide(ctx, config.decide_timeout_seconds, config.max_failures)
            err = strat.last_error if strat.failure_count > before else None
            decisions.append(Decision(strat.name, snap.poll_index, action, err))
            if action is None:
                continue
            side = action["side"]
            with _stats_lock:  # consistent read vs. the background resolver's writes
                affordable = strat.bankroll >= config.stake
            if not affordable:
                # Can't afford the stake — treat as a forced pass, logged.
                decisions[-1] = Decision(strat.name, snap.poll_index, None, "insufficient bankroll")
                continue
            asks = snap.books.get(side, {}).get("asks", [])
            other = "Down" if side == "Up" else "Up"
            comp_bids = snap.books.get(other, {}).get("bids", []) if config.use_cross_book_fill else None
            fill = simulate_fill(side, asks, config.stake, comp_bids,
                                 config.slippage_coeff, config.slippage_exp)
            if fill is None:
                decisions[-1] = Decision(strat.name, snap.poll_index, action, "empty book / no fill")
                continue
            fills[strat.name] = fill
            strat.entered_this_window = True

    return decisions, fills, snaps


def resolve_window(
    strategies: List[LoadedStrategy],
    fills: Dict[str, Fill],
    resolution: Resolution,
    window_id: str,
) -> Tuple[List[TradeResult], Optional[str], bool]:
    """Score fills against the reconciled outcome; update bankroll + stats.

    Immediate score is the Coinbase 5m side; the official Gamma outcome wins when
    present. If they disagree we flag the mismatch (the trades are scored against
    the official side, i.e. any that would have been scored the other way are
    effectively flipped).
    """
    coinbase = resolution.coinbase_side
    official = resolution.official_side
    resolved = official or coinbase
    mismatch = official is not None and coinbase is not None and official != coinbase

    trades: List[TradeResult] = []
    if resolved is None:
        return trades, resolved, mismatch

    for strat in strategies:
        fill = fills.get(strat.name)
        if fill is None:
            continue
        result = score_trade(strat.name, window_id, fill, resolved)
        strat.bankroll += result.net_pnl
        strat.gen.record(result)
        strat.lifetime.record(result)
        trades.append(result)
    return trades, resolved, mismatch


# --------------------------------------------------------------------------- #
# One generation
# --------------------------------------------------------------------------- #
def _resolve_and_record(strategies, market, store, config, generation, seq, handle, decisions, fills, snaps):
    """Resolve one traded window (blocking wait), score, persist, print the board.

    Returns True if the window resolved (counts toward the generation).
    """
    resolution = market.resolve(handle)  # waits for official settlement
    with _stats_lock:
        trades, resolved, mismatch = resolve_window(strategies, fills, resolution, handle.window_id)

    candle_hash = hash_candles(snaps[-1].candles, snaps[-1].window_open_price) if snaps else ""
    window = WindowData(
        window_id=handle.window_id,
        condition_id=handle.condition_id,
        title=handle.title,
        start_iso=handle.start.isoformat() if hasattr(handle.start, "isoformat") else str(handle.start),
        end_iso=handle.end.isoformat() if hasattr(handle.end, "isoformat") else str(handle.end),
        token_map=handle.token_map,
        polls=snaps,
        coinbase_side=resolution.coinbase_side,
        official_side=resolution.official_side,
        resolved_side=resolved,
        mismatch=mismatch,
        candle_state_hash=candle_hash,
    )
    store.save_window(generation, seq, window, decisions, trades)
    if resolved is None:
        log.warning("window %s did not resolve; not counting toward generation", handle.window_id)
        return False, trades, resolved, mismatch
    if mismatch:
        log.warning(
            "resolution mismatch on %s: coinbase=%s official=%s (trades scored to official)",
            handle.window_id, resolution.coinbase_side, resolution.official_side,
        )
    log.info("window %s (%s) resolved %s — %d trade(s) this window",
             handle.window_id, handle.title, resolved, len(trades))
    return True, trades, resolved, mismatch


class _ResolutionPipeline:
    """Background worker that resolves+scores+persists traded windows in FIFO order.

    Lets the trader move on to the next window while a prior one waits for
    settlement, so consecutive 5-minute windows aren't missed.
    """

    def __init__(self, strategies, market, store, config):
        self._args = (strategies, market, store, config)
        self._q: "queue.Queue" = queue.Queue()
        self._count_lock = threading.Lock()
        self.resolved_count = 0
        # A POOL of workers so one slow/stuck market only ties up one thread while
        # the others keep resolving later windows (no head-of-line blocking).
        n = max(1, getattr(config, "resolution_workers", 1))
        self._threads = [
            threading.Thread(target=self._run, name=f"resolver-{i}", daemon=True) for i in range(n)
        ]
        for t in self._threads:
            t.start()

    def submit(self, item) -> None:
        self._q.put(item)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                break
            try:
                self._process(item)
            except Exception as exc:  # noqa: BLE001 — never kill the worker
                log.error("resolution worker error: %s", exc)
            finally:
                self._q.task_done()

    def _process(self, item) -> None:
        strategies, market, store, config = self._args
        generation, seq, handle, decisions, fills, snaps = item
        counted, trades, resolved, mismatch = _resolve_and_record(
            strategies, market, store, config, generation, seq, handle, decisions, fills, snaps
        )
        if not counted:
            return
        with self._count_lock:
            self.resolved_count += 1
            num = self.resolved_count
        if config.live_window_reports:
            with _stats_lock:
                board = format_window_status(
                    generation, num, config.windows_per_generation,
                    handle.window_id, resolved, mismatch, strategies, trades, title=handle.title,
                )
            print(board, flush=True)

    def join(self) -> None:
        self._q.join()

    def stop(self) -> None:
        for _ in self._threads:
            self._q.put(None)
        for t in self._threads:
            t.join(timeout=5)


def run_generation(
    strategies: List[LoadedStrategy],
    market: MarketProvider,
    store: Store,
    config: Config,
    generation: int,
) -> None:
    """Forward-test all strategies over ``windows_per_generation`` resolved windows.

    Trading is real-time and sequential (windows are consecutive), but resolution
    runs in a background worker so the trader starts the next window immediately
    instead of blocking on settlement. The worker is drained before ranking.
    """
    for s in strategies:
        s.start_generation()
    store.start_generation(generation)

    target = config.windows_per_generation
    pipeline = _ResolutionPipeline(strategies, market, store, config)
    seq = 0
    try:
        # Trade `target` windows back-to-back; resolution happens in the worker.
        for _ in range(target):
            handle = market.next_window()
            decisions, fills, snaps = run_window(strategies, market.poll_snapshots(handle), config)
            pipeline.submit((generation, seq, handle, decisions, fills, snaps))
            seq += 1
            if not config.overlap_resolution:
                pipeline.join()  # sequential mode: settle before the next window
        pipeline.join()
        # Top up only if some windows failed to resolve (rare — settlement waits).
        while pipeline.resolved_count < target:
            handle = market.next_window()
            decisions, fills, snaps = run_window(strategies, market.poll_snapshots(handle), config)
            pipeline.submit((generation, seq, handle, decisions, fills, snaps))
            seq += 1
            pipeline.join()
    finally:
        pipeline.stop()


# --------------------------------------------------------------------------- #
# Ranking + carry-forward
# --------------------------------------------------------------------------- #
def risk_adjusted_score(stats: "Stats", config: Config) -> float:
    """Sharpe-style survival score: mean per-trade P&L divided by its volatility.

    Rewards CONSISTENT positive P&L and penalises high-variance (longshot) returns,
    so a strategy that only profits via rare jackpots ranks below a steady earner.
    Two guards keep it robust:

    - ``risk_vol_floor`` is blended into the denominator so a strategy with 1-2
      identical trades (zero measured volatility) can't post an infinite score.
    - a small-sample shrinkage ``trades/(trades+risk_trade_prior)`` pulls low-trade
      strategies toward 0, so a couple of lucky trades can't top the board.

    A strategy that never traded scores ``-inf`` — it can't survive on merit, which
    stops do-nothing strategies from surviving a losing generation by default.
    """
    n = stats.trades
    if n == 0:
        return float("-inf")
    denom = math.sqrt(stats.pnl_std ** 2 + config.risk_vol_floor ** 2)
    sharpe = stats.mean_pnl / denom
    shrink = n / (n + config.risk_trade_prior)
    return sharpe * shrink


def ranking_key(strategy: LoadedStrategy, config: Config):
    """Sort key for survival: lifetime risk-adjusted score, then lifetime edge.

    Uses LIFETIME (not single-generation) stats so one unlucky 50-window generation
    can't cull a strategy with a proven track record. The Sharpe-style ratio is fair
    across strategy ages (it doesn't inflate with trade count), so newer strategies
    aren't disadvantaged purely for being young.
    """
    return (risk_adjusted_score(strategy.lifetime, config), strategy.lifetime.tiebreak)


def rank_and_cull(
    strategies: List[LoadedStrategy], config: Config
) -> Tuple[List[LoadedStrategy], List[LoadedStrategy]]:
    """Return (survivors, retirees).

    Alive strategies rank by lifetime RISK-ADJUSTED P&L (Sharpe-style: mean per-trade
    P&L over its volatility; tiebreak: hit% minus avg breakeven). The top
    ``survivors`` live on with cumulative bankroll/stats and an incremented
    generations-survived counter. Everyone else — plus any auto-retired (crashy)
    strategy — is retired.
    """
    alive = [s for s in strategies if not s.retired]
    failed = [s for s in strategies if s.retired]
    alive.sort(key=lambda s: ranking_key(s, config), reverse=True)

    n_survive = min(config.survivors, len(alive))
    survivors = alive[:n_survive]
    retirees = alive[n_survive:] + failed

    for s in survivors:
        s.generations_survived += 1
    for s in retirees:
        if not s.retired:
            s.retire(f"culled: rank below top {config.survivors} by risk-adjusted P&L")
    return survivors, retirees


# --------------------------------------------------------------------------- #
# Evolution: generating strategies
# --------------------------------------------------------------------------- #
def _unique_name(name: str, taken: set, generation: int) -> str:
    if name not in taken:
        return name
    candidate = f"{name}_g{generation}"
    i = 2
    while candidate in taken:
        candidate = f"{name}_g{generation}_{i}"
        i += 1
    return candidate


def _build_from_source(
    source: str,
    generation: int,
    config: Config,
    client,
    taken_names: set,
) -> Optional[LoadedStrategy]:
    """Validate (with one repair attempt) and construct a LoadedStrategy, or None."""
    lineage = parse_lineage(source)
    try:
        strat = LoadedStrategy.create(source, generation, config, lineage=lineage)
    except SandboxError as exc:
        log.info("validation failed (%s); requesting one repair", exc)
        repaired = _repair(client, source, str(exc), config)
        if repaired is None:
            return None
        try:
            strat = LoadedStrategy.create(repaired, generation, config, lineage=parse_lineage(repaired))
        except SandboxError as exc2:
            log.info("repair still invalid (%s); skipping slot", exc2)
            return None
    strat.name = _unique_name(strat.name, taken_names, generation)
    return strat


def _repair(client, source: str, error: str, config: Config) -> Optional[str]:
    for _ in range(max(1, config.max_repair_attempts)):
        try:
            resp = client.chat(SYSTEM_PROMPT, repair_prompt(source, error))
        except Exception as exc:  # noqa: BLE001
            log.warning("repair call failed: %s", exc)
            return None
        blocks = extract_code_blocks(resp)
        if blocks:
            return blocks[0]
    return None


def _is_duplicate(
    candidate: LoadedStrategy,
    existing: List[LoadedStrategy],
    recent_windows,
    config: Config,
) -> bool:
    """True if candidate's decisions match any existing strategy on >threshold of windows."""
    if not recent_windows or not existing:
        return False
    try:
        cand_vec = action_vector(candidate.source, recent_windows, config)
    except SandboxError:
        return False
    for other in existing:
        try:
            other_vec = action_vector(other.source, recent_windows, config)
        except SandboxError:
            continue
        if agreement(cand_vec, other_vec) > config.duplicate_threshold:
            log.info("rejecting %s: >%.0f%% identical to %s",
                     candidate.name, config.duplicate_threshold * 100, other.name)
            return True
    return False


def generate_batch(
    client,
    store: Store,
    config: Config,
    generation: int,
    n_needed: int,
    system: str,
    user: str,
    kind: str,
    existing: List[LoadedStrategy],
    taken_names: set,
) -> List[LoadedStrategy]:
    """Call OpenRouter and turn its reply into up to ``n_needed`` valid strategies."""
    response = client.chat(system, user)
    prompt_id = store.save_prompt(generation, kind, system, user, response)
    recent_windows = store.recent_windows(config.diversity_lookback_windows)

    new: List[LoadedStrategy] = []
    for block in extract_code_blocks(response):
        if len(new) >= n_needed:
            break
        strat = _build_from_source(block, generation, config, client, taken_names)
        if strat is None:
            continue
        if _is_duplicate(strat, existing + new, recent_windows, config):
            replacement = _request_replacement(client, config, generation, strat, taken_names)
            if replacement is None or _is_duplicate(replacement, existing + new, recent_windows, config):
                log.info("skipping duplicate slot for %s", strat.name)
                continue
            strat = replacement
        strat.bankroll = config.starting_bankroll
        store.save_strategy(strat, prompt_id)
        taken_names.add(strat.name)
        new.append(strat)
    return new


def _request_replacement(
    client, config: Config, generation: int, dup: LoadedStrategy, taken_names: set
) -> Optional[LoadedStrategy]:
    user = (
        f"The strategy '{dup.name}' is a near-duplicate of an existing strategy "
        f"(>{config.duplicate_threshold*100:.0f}% identical decisions). Produce ONE "
        f"genuinely different strategy as a single ```python code block, with a new "
        f"unique NAME and a distinct edge thesis. Start with the `# lineage:` comment."
    )
    try:
        resp = client.chat(SYSTEM_PROMPT, user)
    except Exception as exc:  # noqa: BLE001
        log.warning("replacement call failed: %s", exc)
        return None
    blocks = extract_code_blocks(resp)
    if not blocks:
        return None
    return _build_from_source(blocks[0], generation, config, client, taken_names)


def seed_population(client, store: Store, config: Config) -> List[LoadedStrategy]:
    """Generation 1: create ``population_size`` strategies from scratch.

    One reply rarely contains a large batch in full (blocks that fail the sandbox or
    duplicate each other), so we keep asking for the remaining shortfall — same
    approach as :func:`evolve` — until the population is seeded or we run out of
    attempts.
    """
    taken = set(store.all_strategy_sources().keys())
    strategies: List[LoadedStrategy] = []
    for attempt in range(max(1, config.max_breed_attempts)):
        n_needed = config.population_size - len(strategies)
        if n_needed <= 0:
            break
        batch = generate_batch(
            client, store, config, generation=1, n_needed=n_needed,
            system=SYSTEM_PROMPT, user=seed_prompt(n_needed),
            kind="seed", existing=strategies, taken_names=taken,
        )
        strategies.extend(batch)
        if not batch:
            log.warning("seed attempt %d/%d produced 0 strategies",
                        attempt + 1, config.max_breed_attempts)
    if len(strategies) < config.population_size:
        log.warning("seeded %d/%d strategies", len(strategies), config.population_size)
    for s in strategies:
        store.save_state(s, alive=True, generation=1)
    return strategies


def evolve(
    survivors: List[LoadedStrategy],
    retirees: List[LoadedStrategy],
    client,
    store: Store,
    config: Config,
    generation: int,
) -> List[LoadedStrategy]:
    """Breed replacements until the population is back up to ``population_size``.

    A single OpenRouter reply can under-deliver (blocks that fail the sandbox or are
    near-duplicates that can't be replaced), which would let the population shrink
    generation over generation. So we keep breeding — asking only for the shortfall
    each round — until the population is full or ``max_breed_attempts`` is reached.
    """
    taken = set(store.all_strategy_sources().keys())
    replacements: List[LoadedStrategy] = []
    for attempt in range(max(1, config.max_breed_attempts)):
        n_needed = config.population_size - len(survivors) - len(replacements)
        if n_needed <= 0:
            break
        user = evolution_prompt(survivors, retirees, n_needed)
        batch = generate_batch(
            client, store, config, generation=generation, n_needed=n_needed,
            system=SYSTEM_PROMPT, user=user, kind="evolution",
            existing=survivors + replacements, taken_names=taken,
        )
        replacements.extend(batch)
        if not batch:
            log.warning("evolve attempt %d/%d bred 0 new strategies",
                        attempt + 1, config.max_breed_attempts)
    have = len(survivors) + len(replacements)
    if have < config.population_size:
        log.warning("population under target after breeding: %d/%d",
                    have, config.population_size)
    return replacements
