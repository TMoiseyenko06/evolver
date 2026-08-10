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
npm start           # dry run: scans and prints trades, places nothing
```

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
| `ARBJS_MIN_PROFIT_CENTS` | `1` | Minimum net edge per set to act on. |
| `ARBJS_TRADE_CENTS` | `500` | Capital per arb, in cents. |
| `ARBJS_MAX_SETS` | `200` | Hard cap on sets per arb, regardless of depth. |
| `ARBJS_POLL_SECONDS` | `30` | Scan interval. |

**The Kalshi wallet segment is not guessed.** Orders go to
`/api/v1/wallet/{segment}/{wallet_id}/order`; `pol` is the segment `polybot` uses
for Polymarket, and the Kalshi one is left unset until it has been confirmed
against the API. Live mode refuses to start without it, because every opportunity
here is cross-venue: filling only the Polymarket leg is an unhedged directional
bet, which is the exact opposite of the point.

## What the bot does each poll

1. **List** both venues through `GET /api/v1/markets?venue=…`, paging to
   `ARBJS_MAX_MARKETS` and dropping resolved markets.
2. **Orient** each market's outcomes into YES/NO. Synthesis returns
   `left_*`/`right_*` and the labels differ per market (`Yes`/`No`, `Up`/`Down`),
   so labels are read, never inferred from position. A pair that isn't
   complementary (`Trump`/`Harris`) is skipped rather than guessed at.
3. **Match** events across venues by title ([`matcher.js`](src/matcher.js)):
   Jaccard word overlap blended with Levenshtein distance.
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

> **Known gap: Kalshi order books.** Unauthenticated, `POST /api/v1/markets/orderbooks`
> returns books for every Polymarket token and an empty list for every Kalshi one.
> Until that resolves (an API key may be all it needs), Kalshi sides stay unpriced
> and no cross-venue pair can be entered — correct behavior, but silent, so each
> scan prints how many matched sides actually got a book:
> `books on 4/8 sides` means half the universe is unpriceable, not that the
> market is efficient.

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
| [`src/env.js`](src/env.js) | `.env` loader (why this package has no npm dependencies). |
| [`config.js`](config.js) | Defaults + env overrides. |
