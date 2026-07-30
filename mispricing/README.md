# mispricing — a fair-value model with early exit

One LLM-generated strategy from the evolver population (`stale_quote_sweeper`) built
a genuinely sound idea: estimate a "fair" probability of Up from recent realized
volatility (scaled by sqrt-time-to-close) and compare it to the market's actual ask,
entering only when the two disagree by more than a fee-adjusted margin. This package
pulls that idea out into first-class, hand-written code (not LLM-generated, no AST
sandbox — this is trusted code we wrote and can read directly) and adds what the
evolved-strategy contract can't support: **early exit**.

Live Synthesis calibration showed real fees are effectively $0, so closing a position
early (crossing the spread) is cheap — worth doing once the mispricing has closed, or
to cut a trade whose thesis has stalled or reversed, instead of always holding to the
coin-flip resolution.

## The model

Given recent candles, the window's open price, current spot, and time remaining:
1. Estimate recent 1-minute volatility (`sd`) from the last few candle-to-candle returns.
2. Scale it to the time left via the square-root-of-time rule: `sigma = sd * sqrt(seconds_remaining/60)`.
3. Compute `p_up` = the probability, under a driftless random walk with that volatility,
   that price finishes above the window's open — a neutral baseline, not a momentum
   or reversion opinion.
4. Enter a side when the model's fair value clears its ask by more than `entry_gap`
   (default 0.08) AND still clears the fee-adjusted breakeven by more than
   `fee_margin` (default 0.05).

## The three exit triggers (checked in this priority order)

Recomputed with the SAME model each poll after entry — no new signal:

1. **`gap_closed`** — the ask has caught up to fair value: take the profit.
2. **`adverse_move`** — the model itself now prices the position below what was
   paid: a self-consistent stop-loss.
3. **`time_expired`** — neither has happened within `time_expired_seconds` (default
   60s) of entry: the thesis was probably wrong.

If none fire before the window ends, it falls through to normal hold-to-resolution
scoring — exactly the evolved-strategy contract's behavior.

## Usage

```bash
# Validate against ALREADY-COLLECTED evolver windows — read-only, zero risk.
# Prints with-exits vs. hold-to-resolution baseline side by side.
python -m mispricing backtest --recent 500

# Live paper-trading loop (no real money).
python -m mispricing paper --max-windows 50 --report runs/mispricing_report.md
```

Both share model flags (`--entry-gap`, `--fee-margin`, `--time-expired-seconds`,
`--gap-closed-min-gain`, `--lookback-candles`) and fill-realism flags (`--stake`,
`--book-participation`, `--max-slippage`, `--slippage-coeff`/`--slippage-exp`,
`--no-cross-book-fill`) — the same knobs the evolver itself uses, so a mispricing
backtest is directly comparable to the population's own numbers.

## Design notes

- **Entry/exit params live in `mispricing.config.MispricingParams`**, not in the
  shared `evolver.config.Config` — they're this one strategy's thresholds, not infra.
- **One symmetric loop** (`runner.run_window`) takes only an `Iterable[PollSnapshot]`,
  so the exact same decision logic runs whether fed by a live market, the test
  harness's mock market, or a plain list of archived polls — a backtest result is a
  true prediction of live behavior because it's the same code, not a parallel
  reimplementation.
- **The paper loop is in-memory only** (no SQLite), matching `arb/paper.py`'s
  precedent — an optional `--report` writes one markdown summary at the end.
- **`evolver/engine.py` gained the missing symmetric "sell into the bid" functions**
  (`walk_bid_book`, `simulate_exit_fill`, `score_exit_trade`) since nothing in the
  repo previously supported closing a position early — these are generic accounting
  additions any future early-exit strategy can reuse, not mispricing-specific.

## Out of scope (for now)

**Real-money exit execution.** `polybot.synthesis.SynthesisClient.place_market_order`
always sends `units="USDC"` (dollar-denominated); a share-quantity SELL needs a small
client extension (`units="SHARES"`), plus a `SynthesisExecutor.close(...)` method, and
price-guard rejections handled as a normal retry (not a failure) — same discipline as
the buy-side price guard. This is real, valuable follow-up work, not built here — this
package is paper/backtest only until the exit idea is validated on real historical
and live paper data.
