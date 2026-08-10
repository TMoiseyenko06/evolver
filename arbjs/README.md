# arbjs — cross-venue arbitrage bot (Node), on Synthesis

A polling bot that matches the *same event* across Polymarket and Kalshi, buys
YES on one venue and NO on the other when the pair costs less than $1 after
fees, and holds the locked set to resolution. Unlike the read-only [`arb/`](../arb)
scanner, this one **places orders** — so live trading is opt-in and off by default.

It talks to **[synthesis.trade](https://synthesis.trade)**, the same venue-agnostic
API the rest of this repo uses (`polybot/synthesis.py`): one API key, one wallet,
both venues, one market shape. There is no `pmxtjs` / per-venue SDK here.

```bash
cd arbjs
npm test            # no dependencies to install — node:test + native fetch
npm run paper       # paper-trade a $500 bankroll on live prices (start here)
npm start           # the bot itself; dry run unless ARBJS_LIVE=true
```

## Paper trading

`npm run paper` runs the identical scan and takes the same entries, but simulates
the fills and keeps a book:

```
=== arbjs paper · bankroll $300.12 · deployed $199.88 (2 open) · realized $0.00
    · unrealized $-1.40 · equity $498.60 ($-1.40 vs start) · taken 2 / resolved 0 ===
```

- **$500 bankroll**, **$100 max per arbitrage** — both legs combined, fees
  included (`ARBJS_PAPER_BANKROLL_CENTS`, `ARBJS_PAPER_MAX_ARB_CENTS`). So the
  bankroll funds ~5 concurrent positions, and sizing is the tightest of the cap,
  the cash left, and the depth of the thinner leg.
- **Marked at the bid**, not at the ask paid — an untouched position shows the
  spread it would actually cross to get out.
- **Settled per leg, per venue.** This is the part that matters. A cross-venue
  set is only a lock if *both* venues resolve the event the same way, so each leg
  is settled against its own venue's `winner_token_id` rather than assumed to net
  to $1. When exactly one leg pays, the edge was real; when the venues disagree
  the position pays $0 or $2 per set and the board counts it under `DIVERGED`.
  That number is the honest measure of basis risk on this strategy.
- **State persists** to `paper-state.json`, so positions carry across restarts —
  necessary, since most matched events resolve months out.

Resolved markets disappear from the listing entirely, so settlement is read by
looking each leg's market up directly (`GET /api/v1/{venue}/market/{id}`).

## Configuration

All of [`config.js`](config.js) is env-overridable; put values in the repo-root
`.env` (found by walking up, same as the Python CLI). The ones that matter:

| Variable | Default | What |
|---|---|---|
| `SYNTHESIS_API_KEY` | — | Market data is public; orders are not. |
| `SYNTHESIS_WALLET_ID` | — | Required for live trading. |
| `ARBJS_LIVE` | unset | `true` places **real orders**. Anything else is a dry run. |
| `SYNTHESIS_KALSHI_WALLET_SEGMENT` | — | Wallet path segment for Kalshi orders (see below). |
| `ARBJS_TITLE_FILTER` | — | Substring filter on titles, e.g. `bitcoin`, to scan one family of markets. |
| `ARBJS_MAX_MARKETS` | `0` | Markets per venue; `0` means the whole universe. |
| `ARBJS_MIN_PROFIT_CENTS` | `1` | Minimum net edge per set to act on. |
| `ARBJS_TRADE_CENTS` | `500` | Capital per arb, in cents (live mode). |
| `ARBJS_MAX_SETS` | `200` | Hard cap on sets per arb, regardless of depth (live mode). |
| `ARBJS_PAPER_BANKROLL_CENTS` | `50000` | Paper bankroll ($500). |
| `ARBJS_PAPER_MAX_ARB_CENTS` | `10000` | Paper cap per arbitrage, both legs combined ($100). |
| `ARBJS_POLL_SECONDS` | `30` | Delay between scans (a full scan itself takes ~75s). |

**The Kalshi wallet segment is not guessed.** Orders go to
`/api/v1/wallet/{segment}/{wallet_id}/order`; `pol` is the segment `polybot` uses
for Polymarket, and the Kalshi one is left unset until it has been confirmed
against the API. Live mode refuses to start without it, because every opportunity
here is cross-venue: filling only the Polymarket leg is an unhedged directional
bet, which is the exact opposite of the point.

## What the bot does each poll

1. **List** both venues through `GET /api/v1/markets?venue=…`, paging to
   exhaustion — the entire universe, ~127k Polymarket and ~44k Kalshi markets in
   about 30s. Arbs hide in the thin tail, so there is no volume cutoff.
2. **Orient** each market's outcomes into YES/NO. Synthesis returns
   `left_*`/`right_*` and the labels differ per market (`Yes`/`No`, `Up`/`Down`),
   so labels are read, never inferred from position. A pair that isn't
   complementary (`Trump`/`Harris`) is skipped rather than guessed at.
3. **Match** events across venues by title ([`matcher.js`](src/matcher.js)):
   Jaccard word overlap blended with Levenshtein distance. At full scale that's
   5.6 billion possible pairs, so candidates are blocked through an inverted
   index that ignores ubiquitous words ("above", "2026", "district") — a real
   pair always shares something distinctive. ~75s for the full cross-product.
4. **Fetch books** for the matched pairs only, and compute each side's
   **executable** ask.
5. **Price** both directions net of fees and keep the profitable one
   ([`arbitrage.js`](src/arbitrage.js)).
6. **Rank** by volume, take the top N, re-sort by profit, and enter the best one
   if flat.

## Realistic fills and fees, same as everywhere else here

Two corrections separate a real edge from a printed one, and both are the
repo's existing models ported to JS:

- **Executable, not displayed, asks.** A resting ask cannot fill below
  `1 − complement_best_bid`, because a maker bidding for the other side of the
  same market is implicitly offering this side there. Live calibration confirmed
  it: a displayed 6¢ ask executed near 17¢ while the other side was bid 83¢.
  Without this clamp a stale quote manufactures an arb that doesn't exist.
  (Mirrors `evolver.engine.executable_price`.)
- **Fees are part of the cost.** Polymarket `0.0312·min(p, 1−p)` and Kalshi
  `0.07·p·(1−p)` per share, so a sub-cent gross spread correctly reads as a loss.
  (Mirrors `arb/fees.py` — defaults to verify against real fills, not gospel.)

A market with no book on a side has no tradeable price and is skipped; listing
mids are kept only as indicative `yesMid`/`noMid`.

### The two venues address books differently

`POST /api/v1/markets/orderbooks` takes **Polymarket token ids** but **Kalshi
market ids** — passing a Kalshi token id returns an empty list, silently. The
response shapes differ too: Polymarket sends one flat book per token, Kalshi
sends one entry per market with nested `yes` and `no` books. Both are normalized
to a single `bookKey` space (`token_id` for Polymarket, `market_id:yes` /
`market_id:no` for Kalshi) so nothing downstream cares.

Getting this wrong costs you the entire Kalshi side and reads as "the market is
efficient" rather than as a bug, so every scan prints its book coverage:
`books on 93/106 sides`.

## Position handling

- **Both legs are sized identically**, in *sets*. A set only pays its guaranteed
  100¢ if the YES and NO holdings match, so size is
  `min(capital / cost_per_set, ARBJS_MAX_SETS, book depth)` and both legs get
  that number of contracts.
- **A partial fill is unwound immediately.** If one leg fills and the other
  doesn't, the position is a naked directional bet, so the filled leg is sold
  right away instead of waiting for the next poll.
- **A failed exit stays on the books.** Legs that couldn't be sold are kept in
  `currentPosition` and flagged, so a live unhedged position can't disappear
  from the bot's view.
- **P&L is marked at the bid** — what a sale would actually fetch — not at the
  ask that was paid.

## The risk this bot cannot remove

**A matched title is a candidate, not a proof.** Two venues can word the same
question identically and still settle it against a different reference price,
window, or tie-break rule. When that happens the "hedge" is two independent
directional bets that can both lose, and no amount of fill modeling helps.
Confirm settlement equivalence for a pair before trading it — `python -m arb
match` does this with an LLM confirmation step and is the better tool for
vetting pairs.

Also worth keeping in mind:

- **Depth is small.** A 5% edge on 3 contracts is $0.15, not a business.
- **Legging risk is real.** The two legs are placed in parallel, but they are
  separate venues and separate orders; they can't be atomic.
- **Rotation sells a lock.** Exiting a profitable position to chase a better one
  pays the spread twice; it's on by default because it was in the original
  design, but it is not free.

## Layout

| File | Role |
|---|---|
| [`src/synthesis.js`](src/synthesis.js) | Synthesis REST client — listings, batch order books, market orders. JS counterpart of `polybot/synthesis.py`. |
| [`src/bot.js`](src/bot.js) | Market parsing, book application, the poll loop, execution and positions. |
| [`src/arbitrage.js`](src/arbitrage.js) | Fee models, executable-ask correction, the per-pair arb math. |
| [`src/matcher.js`](src/matcher.js) | Cross-venue title matching. |
| [`src/paper.js`](src/paper.js) | Paper book: sizing, marking, per-venue settlement, persistence. |
| [`src/env.js`](src/env.js) | `.env` loader (why this package has no npm dependencies). |
| [`config.js`](config.js) | Defaults + env overrides. |
