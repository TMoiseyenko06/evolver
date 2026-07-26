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
| **field** (multi-outcome) | Buy YES on EVERY outcome of a one-winner event (e.g. every golfer in a tournament): `Σ ask_Yes < 1`. | Single venue, no basis risk — **but only if the field is COMPLETE.** A missing outcome that wins pays you $0. |
| **cross-venue** | The SAME event on both venues: buy cheapest Yes + cheapest No across them. | Only a true arb if both venues **resolve the event identically** (same reference price, window, tie-break). Matching titles ≠ matching settlement. |

All reduce to the same math: find the cheapest way to buy every mutually-exclusive
outcome; if that total (plus fees) is under $1, the difference is locked profit per
set — since exactly one outcome pays $1.

## Usage

```bash
# both-sides arb within single markets, across a whole venue (realistic fills)
python -m arb intra --venue polymarket --min-edge 0.005
python -m arb intra --venue kalshi

# multi-outcome field arb: buy every outcome of a one-winner event for < $1
python -m arb field --venue kalshi --min-edge 0.005

# same event priced apart across Polymarket & Kalshi
python -m arb cross --min-similarity 0.6 --min-edge 0.005

# inspect how a venue names its markets (to design matching)
python -m arb sample --venue kalshi --n 40

# PAPER-TRADE intra-market arb on a loop, realistic fills, cumulative P&L
python -m arb paper --venue polymarket --bankroll 500 --min-edge 0.005 --interval 30
python -m arb paper --venue both
```

Needs `SYNTHESIS_API_KEY` (and optionally `SYNTHESIS_BASE_URL`, default
`https://synthesis.trade`) in `.env` or the environment. `--min-edge 0.005` = 0.5%
net edge per $1 set; `--top N` caps rows; `--max-markets` bounds how many to scan.

## Realistic fills (same model as the evolver)

Every scan and paper trade prices legs at the **executable** ask, not the displayed
one, reusing `evolver.engine.executable_price`: a leg's ask can't fill below
`1 − sibling_best_bid` (the other outcome of the same market). Displayed liquidity
at cheap prices is largely phantom, so without this a 0.06 ask would manufacture a
fake arb; the clamp lifts it to the real executable price and the fake arb
disappears. `intra`/`cross`/`paper` use realistic fills by default.

## Paper trading

`python -m arb paper` scans on an interval and paper-buys any **intra-market** arb
that clears the fee/edge bar on executable prices, sized to real book depth
(`per_arb_cap` shares max), then realizes the locked P&L when the market matures.

Intra-market arb is a guaranteed lock — buying `n` shares of both outcomes costs
`net_cost·n` and pays exactly `n` at resolution (one side wins), so P&L = `n·edge`,
no resolution lookup needed. The board tracks bankroll, deployed capital, open
locked profit, realized P&L, and counts. It's an honest measurement of how much
locked arb is actually capturable after realistic fills, fees, and depth — expect
close to zero on efficient venues, which is itself the answer.

`paper` also trades **field** arbs by default (disable with `--no-field`). A field
lock's paper payout assumes the field was complete (one bought outcome wins) — the
detector guards this with the `Σ mid ≈ 1` completeness heuristic, but on real money
you must confirm no outcome is missing. Cross-venue arb is **not** paper-traded (its
P&L depends on both venues resolving identically, which we can't simulate yet) —
`cross` still reports those candidates.

## Field (multi-outcome) arb — how it's detected

Kalshi lists a tournament as one Yes/No market *per contestant* (`3M Open Winner -
Hideki Matsuyama`), and Polymarket does the same for "who wins" events. `field`
groups markets by event and, for events that are **all Yes/No with ≥3 outcomes**,
checks whether `Σ executable_YES_ask < 1`. Two guards:
- **Completeness** (`detect.field_arb`): requires `Σ YES_mid` in `[0.90, 1.6]`. A
  complete, fairly-priced field sums to ~1 (+overround); a much smaller sum means
  outcomes are missing from the book — and a missing winner would pay you $0. Still a
  **human-confirm** candidate: verify the field is exhaustive before trusting it.
- **Realistic fills**: uses executable asks, so a phantom cheap contestant can't fake
  a field lock.

Honest expectation: on liquid books `Σ YES_ask` almost always exceeds $1 (the
overround/vig), so field locks are rare — but they're cheap to scan and are the
arb type your data actually contains.

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
