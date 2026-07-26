# arb — cross-venue prediction-market arbitrage (Synthesis: Polymarket + Kalshi)

Directional trading on efficient markets has no edge after realistic fills (the
evolver proved this over thousands of paper trades). **Arbitrage** exploits a
different inefficiency: buying a complete set of a market's mutually-exclusive
outcomes for **less than $1**, which is guaranteed to pay exactly $1 at resolution —
profit independent of the outcome.

This package is **read-only**: it *measures* whether real, executable arbs exist
after fees. Execution is a separate, explicitly-gated step (not built yet, on
purpose — measure before risking capital).

## Two kinds of arb

| Kind | What | Risk |
|---|---|---|
| **intra-market** | Both sides of ONE binary market: `ask_Yes + ask_No < 1`. | Lowest — no event-matching, single venue, guaranteed lock. |
| **cross-venue** | The SAME event on both venues: buy cheapest Yes + cheapest No across them. | Only a true arb if both venues **resolve the event identically** (same reference price, window, tie-break). Matching titles ≠ matching settlement. |

Both reduce to the same math: find the cheapest way to buy every outcome; if that
total (plus fees) is under $1, the difference is locked profit per set.

## Usage

```bash
# both-sides arb within single markets, across a whole venue
python -m arb intra --venue polymarket --min-edge 0.005
python -m arb intra --venue kalshi

# same event priced apart across Polymarket & Kalshi
python -m arb cross --min-similarity 0.6 --min-edge 0.005
```

Needs `SYNTHESIS_API_KEY` (and optionally `SYNTHESIS_BASE_URL`, default
`https://synthesis.trade`) in `.env` or the environment. `--min-edge 0.005` = 0.5%
net edge per $1 set; `--top N` caps rows; `--max-markets` bounds how many to scan.

## How it works

- `polybot.synthesis.list_markets` — unified `GET /api/v1/markets?venue=…` (both
  venues, one shape); `fetch_orderbooks` — batch `POST /api/v1/markets/orderbooks`.
- `detect.parse_markets` normalizes each venue into a `Market` with two `Quote`
  outcomes; `apply_orderbooks` fills the executable **ask** for each.
- `detect.intra_market_arb` / `detect.cross_venue_arb` do the pure lock math;
  `detect.match_events` fuzzily pairs events across venues by title similarity +
  close end time (**candidates for human confirmation**, not auto-truth).
- `fees.fee_per_share` — per-venue taker fee (Polymarket `0.0312·min(p,1−p)`,
  Kalshi `~0.07·p·(1−p)`). These are defaults — **verify against real fills** and
  tune, exactly as we did for the evolver's fill model.

## Important caveats

1. **Cross-venue = basis risk until proven.** A matched title is a *candidate*. If
   the two venues use different settlement sources or windows, it is **not** an arb
   and you can lose both legs. Confirm settlement equivalence before trusting any
   cross-venue edge.
2. **Fees are estimates.** Kalshi's schedule varies by market; the coefficient is a
   placeholder. Under-estimating fees turns a "profit" into a loss.
3. **Executable ≠ displayed.** We use the best ask *and its size* (`max_size`), so
   `max_profit = edge × max_size`. Thin books mean tiny real size — a 5% edge on 3
   shares is $0.15, not a business. Same phantom-liquidity lesson as the evolver.
4. **Execution/legging.** When execution is added it must fill both legs near-
   simultaneously; a single-leg fill leaves you directionally exposed.

## Tests

```
python -m pytest tests/test_arb.py
```
Covers normalization, book parsing, fee models, intra/cross detection, the
outcome-label guard, and event matching.
