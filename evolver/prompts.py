"""Prompt engineering for the OpenRouter strategy-generation calls.

The system prompt teaches the model the market, the fee curve, why directional
accuracy alone loses, the EV/breakeven math, and the exact ``ctx`` interface with
a worked example. The user prompts differ for the first "seed" generation vs.
later "evolution" rounds (which include survivor/retiree code and stats).
"""

from __future__ import annotations

from typing import List

from .strategy import LoadedStrategy

WORKED_EXAMPLE = '''\
```python
# lineage: novel
import statistics

class Strategy:
    NAME = "late_fade_extreme"
    DESCRIPTION = (
        "Late-window longshot fade: in the final 60 seconds, if one side's best "
        "ask is very cheap (<=0.15) the market is nearly decided; buying the cheap "
        "side only pays off on a reversal, which is rare, so instead we buy the "
        "EXPENSIVE favorite ONLY when its breakeven still leaves positive EV given "
        "how far price has moved from the open. Passes otherwise."
    )

    def decide(self, ctx):
        if ctx.seconds_remaining > 60:
            return None
        move = ctx.spot - ctx.window_open_price
        if abs(move) < 1e-9:
            return None
        favorite = "Up" if move > 0 else "Down"
        asks = ctx.books[favorite]["asks"]
        if not asks:
            return None
        best_ask = asks[0][0]
        # Estimate win prob from how decided the move looks; require EV > 0.
        est_p = 0.5 + min(0.45, abs(move) / max(ctx.window_open_price, 1.0) * 500.0)
        if est_p - ctx.breakeven(best_ask) > 0.02:
            return {"side": favorite}
        return None
```'''

SYSTEM_PROMPT = f"""You are a quantitative strategy generator for a paper-trading research \
system. You write Python trading strategies for Polymarket's 5-minute Bitcoin \
Up/Down binary markets. Follow the contract EXACTLY.

MARKET STRUCTURE
- Each market is a 5-minute binary. It resolves on Bitcoin's price at the 5-min \
candle close vs. the candle open: close > open => "Up" wins, otherwise "Down" \
wins (an exact tie resolves Down).
- You buy shares of one side. Winning shares pay $1 each; losing shares pay $0.
- You pay the ask (the price to BUY). Prices are in dollars in [0, 1] and read \
as probabilities.

THE FEE CURVE (this is the whole game)
- Taker fee = 0.0312 * shares * min(price, 1 - price).
- Per share that is 0.0312 * min(price, 1-price): it PEAKS at price 0.50 \
(~1.56 cents/share, i.e. ~3.1% of a 50c share) and decays to ~0 at the extremes \
(a 5c or 95c share pays almost nothing in fees).
- Consequence: trading near 50c is expensive; trading the extremes is cheap.

WHY DIRECTIONAL ACCURACY ALONE LOSES MONEY
- Momentum is ALREADY priced into the ask. When the move is obvious, the obvious \
side's ask is already 0.53-0.57. Buying the obvious side costs 53-57c plus fee, \
so being "right about direction" is not enough — you routinely pay more than the \
outcome is worth.
- Edge equation (per share): EV = p - ask - fee_per_share(ask), where p is your \
TRUE win probability. Breakeven win-prob = ask + 0.0312*min(ask, 1-ask). You only \
have edge when your estimated p clears that breakeven with margin.

THE ctx INTERFACE (what decide receives)
- ctx.candles: list of FULL OHLCV dicts — every candle has keys time, open, high, \
low, close, AND volume. CLOSED 1-minute candles only, newest LAST. The forming \
candle is NEVER included. Do NOT rely on price/close alone: volume (how much BTC \
traded that minute) and the candle range (high-low) are first-class signals — use \
them (e.g. a close move on rising volume is real; a move on thin volume often \
reverts; wide-range bars mean volatility, narrow bars mean a pin).
- ctx.window_open_price: BTC price at the window open (float).
- ctx.seconds_remaining: int seconds until resolution.
- ctx.books: {{"Up": {{"asks": [(price, size), ...], "bids": [...]}}, "Down": {{...}}}}. \
asks are ascending (best/lowest ask first); sizes are share quantities.
- ctx.spot: latest BTC trade price (float).
- ctx.fee(shares, price) -> dollar fee. ctx.breakeven(ask) -> breakeven win-prob.

THE STRATEGY CONTRACT (exact)
- Output a single Python class named EXACTLY `Strategy`.
- Class attributes: NAME (short unique snake_case string) and DESCRIPTION \
(one paragraph: the hypothesis and when it trades).
- Method: def decide(self, ctx) -> dict | None. Return None to pass, or \
{{"side": "Up"}} / {{"side": "Down"}} to buy $10 at the ask.
- decide() is called once at window open and once per 10-second poll. You may \
enter at most once per window; positions are held to resolution (no exits). Use \
ctx.seconds_remaining to time your entry.

HARD SANDBOX RULES (violating these gets your strategy rejected)
- The ONLY imports allowed are `math` and `statistics`. No other imports.
- No dunder attribute access (no __class__, __globals__, etc.), no open/exec/eval, \
no os/sys/requests/network/file access of any kind.
- decide() must return within 1 second and must not raise. Guard against empty \
books/candles.

DIVERSITY IS REQUIRED
- Across a batch, produce genuinely DIFFERENT hypotheses: momentum, \
mean-reversion, order-book imbalance, volatility-regime, late-window \
favorite/longshot, time-of-day, spread/liquidity, AND volume-based ideas \
(volume-confirmed momentum, volume spike exhaustion/fade, low-volume drift or \
pin, volume divergence, wide-range vs narrow-range bars). At least some \
strategies in every batch MUST use volume and/or candle range, not price alone. \
Near-duplicates are rejected.

VOLUME IDIOM (reading candles beyond close)
- Each candle c has c["volume"], c["high"], c["low"]. Example snippets:
  vols = [c["volume"] for c in ctx.candles]; recent = statistics.mean(vols[-5:])
  last_vol = ctx.candles[-1]["volume"]; rng = ctx.candles[-1]["high"] - ctx.candles[-1]["low"]
  A last_vol well above `recent` = a conviction move; near/below = weak/faded.

OUTPUT FORMAT
- Return ONE fenced ```python code block PER strategy, nothing else between them \
that matters. Each block must be self-contained and define `class Strategy`.
- The FIRST line of each block must be a comment: `# lineage: novel` for a new \
idea, or `# lineage: parent_name_a, parent_name_b` when mutating/combining named \
parents from the provided survivors.

WORKED EXAMPLE (format + spirit; do not copy it verbatim)
{WORKED_EXAMPLE}
"""


def seed_prompt(n: int) -> str:
    """User prompt for the first generation: n strategies from scratch."""
    return (
        f"Generate {n} DISTINCT trading strategies as {n} separate ```python code "
        f"blocks, following the contract and sandbox rules exactly. Make them span "
        f"different families (momentum, mean-reversion, book-imbalance, "
        f"volatility-regime, late-window favorite/longshot, time-of-day, "
        f"spread/liquidity, and VOLUME/range-based). Each must have a unique NAME. "
        f"At least a few must use candle volume and/or range (high-low), not price "
        f"alone. Remember: buying the obvious side loses to fees, so every strategy "
        f"needs a real edge thesis, not just a direction guess. Start each block "
        f"with the `# lineage:` comment line."
    )


def _stat_line(s: LoadedStrategy) -> str:
    lt = s.lifetime
    return (
        f"- {s.name} | gens_survived={s.generations_survived} "
        f"| lifetime: trades={lt.trades} hit%={lt.hit_pct*100:.1f} "
        f"avg_breakeven={lt.avg_breakeven:.4f} net_pnl=${lt.net_pnl:+.2f} "
        f"bankroll=${s.bankroll:.2f} | lineage={s.lineage or ['novel']}"
    )


# With a large population, embedding every strategy's source would blow up the
# prompt (50 strategies ≈ tens of thousands of tokens per generation) and dilute the
# signal. Include full CODE only for the best/worst few — the ones actually worth
# mutating or learning failure from — and stats-only lines for the rest.
MAX_SOURCE_SURVIVORS = 10
MAX_SOURCE_RETIREES = 8


def evolution_prompt(
    survivors: List[LoadedStrategy],
    retirees: List[LoadedStrategy],
    n_needed: int,
    max_source_survivors: int = MAX_SOURCE_SURVIVORS,
    max_source_retirees: int = MAX_SOURCE_RETIREES,
) -> str:
    """User prompt for later generations: mutate winners + invent novel ideas.

    Only the first ``max_source_*`` survivors/retirees contribute full source; the
    remainder appear as stat lines so the prompt stays a sane size on big populations.
    """
    parts: List[str] = []
    parts.append(
        f"This is an evolutionary run. Below are the SURVIVORS (top performers to "
        f"learn from) and the RETIREES (culled — learn from their failure). Produce "
        f"exactly {n_needed} NEW strategies as {n_needed} ```python code blocks."
    )
    parts.append("\n=== SURVIVORS (code + lifetime stats) ===")
    for i, s in enumerate(survivors):
        parts.append(_stat_line(s))
        if i < max_source_survivors:
            parts.append(f"```python\n{s.source.strip()}\n```")
    if len(survivors) > max_source_survivors:
        parts.append(f"(code shown for the top {max_source_survivors} survivors; "
                     f"{len(survivors) - max_source_survivors} more listed by stats only)")
    parts.append("\n=== RETIREES (code + stats + why they failed) ===")
    for i, s in enumerate(retirees):
        parts.append(_stat_line(s))
        parts.append(f"failure_analysis: {failure_analysis(s)}")
        if i < max_source_retirees:
            parts.append(f"```python\n{s.source.strip()}\n```")
    parts.append("\n" + _diversity_directive(survivors))
    parts.append(
        f"\nProduce a MIX: some mutations/combinations of the survivors (set "
        f"`# lineage:` to the parent NAME(s)) and some genuinely NOVEL approaches "
        f"unlike anything above (`# lineage: novel`). Avoid near-duplicates of the "
        f"survivors — a new strategy whose TRADES match an existing one on >85% "
        f"of the windows it acts on will be rejected. Each new strategy needs a "
        f"unique NAME and a real edge thesis (beat the fee, don't just guess "
        f"direction). Include at least one idea that keys off candle VOLUME and/or "
        f"range, not price alone."
    )
    return "\n".join(parts)


# Family keywords for detecting population concentration.
_REVERSION_KW = ("revert", "reversion", "fade", "pin", "reversal", "overshoot",
                 "exhaust", "pullback", "mean")
_MOMENTUM_KW = ("momentum", "trend", "breakout", "continue", "accel", "burst", "follow")


def _diversity_directive(survivors: List[LoadedStrategy]) -> str:
    """Warn the model when survivors over-concentrate in one family (esp. reversion).

    A monoculture of mean-reversion strategies gets wiped out together the moment
    the market trends, so when survivors skew that way, demand non-reversion families.
    """
    if not survivors:
        return ""
    def _is(s, kws):
        text = (s.name + " " + (s.description or "")).lower()
        return any(k in text for k in kws)
    rev = sum(1 for s in survivors if _is(s, _REVERSION_KW))
    mom = sum(1 for s in survivors if _is(s, _MOMENTUM_KW))
    note = [f"POPULATION MIX: {rev}/{len(survivors)} survivors are mean-reversion/fade "
            f"style, {mom}/{len(survivors)} are momentum/trend."]
    if rev >= max(2, (len(survivors) + 1) // 2):
        note.append(
            "WARNING: the population is OVER-CONCENTRATED in mean-reversion/fade — a "
            "single trending session wipes them ALL out at once (this is happening). "
            "Do NOT breed more reversion/fade/pin strategies. The MAJORITY of your new "
            "strategies MUST be OTHER families: momentum / trend-continuation, order-book "
            "pressure/imbalance, breakout/expansion, and time-of-day. The population must "
            "survive BOTH trending and ranging markets — deliberately add strategies that "
            "PROFIT when price keeps moving (the opposite of the current survivors)."
        )
    return "\n".join(note)


def failure_analysis(s: LoadedStrategy) -> str:
    """A short, mechanical diagnosis of why a retiree underperformed."""
    if s.retired and s.retired_reason and "auto-retired" in s.retired_reason:
        return s.retired_reason
    lt = s.lifetime
    if lt.trades == 0:
        return "never traded (too conservative / entry conditions never met)"
    reasons = []
    if lt.net_pnl < 0:
        reasons.append(f"net loss ${lt.net_pnl:+.2f}")
    if lt.hit_pct < lt.avg_breakeven:
        reasons.append(
            f"hit% {lt.hit_pct*100:.1f} below avg breakeven {lt.avg_breakeven*100:.1f} "
            f"(paid more in price+fee than it won)"
        )
    if lt.trades > 0 and lt.avg_breakeven > 0.5:
        reasons.append("traded expensive near-50c/favorite prices where fees bite")
    return "; ".join(reasons) or "underperformed peers this generation"


def repair_prompt(source: str, error: str) -> str:
    """One-shot repair prompt sent back when a candidate fails validation."""
    return (
        "The following strategy failed sandbox validation and must be fixed. "
        f"ERROR: {error}\n\nReturn a corrected version as a SINGLE ```python code "
        "block that obeys ALL sandbox rules (only math/statistics imports, no "
        "dunders, no open/exec/eval/os/sys/network, must define class Strategy with "
        "NAME/DESCRIPTION/decide, first line `# lineage:` comment). Original:\n\n"
        f"```python\n{source.strip()}\n```"
    )
