# evolver

An evolutionary strategy-search system for Polymarket's 5-minute **Bitcoin
Up/Down** markets. It asks an LLM (via OpenRouter) to write trading strategies as
Python code, forward-tests a population of them on live markets with paper money,
ranks them, retires the losers, and asks the LLM to breed replacements — forever.

```
python -m evolver run          # the eternal loop
python -m evolver leaderboard  # lifetime rankings, generations survived
python -m evolver show NAME     # a strategy's code, lineage, full stat history
python -m evolver replay NAME   # re-score a strategy against archived windows
python -m evolver tune --strategy NAME   # fine-tune ONE strategy's parameters
python -m evolver calibrate --yes  # place real $-stake orders vs paper (Synthesis)
python -m evolver synthesis-markets  # list the 5-min markets Synthesis is showing
python -m evolver reset --yes   # wipe db + runs/ + strategies/  (add --keep-strategies to keep them)
```

---

## Where is `polybot`?

The task framed evolver as living "inside my existing polybot project" and asked
to reuse `polybot.candles` / `polybot.polymarket` / `polybot.fees` / `polybot.db`.
**This repository was empty at start — there was no `polybot` package to import.**
Rather than block, I built `polybot/` fresh as a sibling package implementing the
*exact behaviors* the task specified, and `evolver/` imports from it (it does not
reimplement discovery, fees, or resolution). If you drop your real `polybot` in,
evolver only depends on this small surface:

| module | what evolver uses |
|---|---|
| `polybot.fees` | `fee(shares, price)`, `fee_per_share`, `breakeven(ask)` |
| `polybot.candles` | `closed_1m_candles`, `five_minute_candle`, `spot` (Coinbase Exchange) |
| `polybot.polymarket` | `discover_windows`, `token_map`, `order_book`, `official_outcome` |
| `polybot.db` | `connect`, `apply_schema` |

Everything that touches the network lives in `polybot`, so the whole engine runs
offline against a mock market in tests.

---

## How it works

1. **Generate.** `OpenRouterClient` (key from `OPENROUTER_API_KEY`, model
   configurable, default `anthropic/claude-fable-5`) is prompted to emit
   `population_size` (=10) strategies as Python `class Strategy` blocks.
2. **Forward-test.** All strategies run simultaneously over one **generation = 50
   resolved windows**. Every strategy sees the *same* data each poll: one shared
   candle feed, one shared book snapshot, one shared resolution. Each gets its own
   $500 paper bankroll and a fixed $10 stake. Fills are simulated by walking the
   real ask book; the taker fee `0.0312 * shares * min(p, 1-p)` is applied.
3. **Rank & cull.** Top 5 survive, bottom 5 retire; the LLM is asked for 5
   replacements (mutations of winners + genuinely novel ideas).
4. **Loop.** Survivors carry bankroll and **cumulative** stats forward.
   `generations_survived` is tracked as a first-class metric.

### The strategy contract

```python
class Strategy:
    NAME = "short_unique_name"
    DESCRIPTION = "one paragraph: the hypothesis and when it trades"
    def decide(self, ctx) -> dict | None: ...
```

`ctx` exposes `candles` (CLOSED 1-min candles, newest last — the forming candle is
never included), `window_open_price`, `seconds_remaining`,
`books = {"Up": {"asks": [(p, sz)…], "bids": […]}, "Down": {…}}`, `spot`,
`fee(shares, price)`, and `breakeven(ask)`. Return `None` to pass or
`{"side": "Up"|"Down"}` to buy $10 at the ask. `decide()` is called once at window
open and once per 10-second poll; **max one entry per strategy per window**;
positions are held to resolution.

---

## Watching it run

After **every resolved 5-minute window**, `run` prints a live status board so you
can watch the population evolve window by window — what each strategy did, its
running bankroll (each starts at **$500**, risking a fixed **$10** per trade), and
its lifetime record:

```
gen 1 · window 3/50 · win_down_1 · resolved Down
  strategy             this window               bankroll  life P&L trades  hit% gens
  momentum             Down@0.47 WIN   +10.96      526.82    +26.82      3 100.0    0
  always_up            Up@0.55 LOSS  -10.26        505.60     +5.60      3  66.7    0
  passer               pass                        500.00     +0.00      0   0.0    0
  crasher              retired                     500.00     +0.00      0   0.0    0
  always_down          Down@0.47 WIN   +10.96      490.34     -9.66      3  33.3    0
```

Set `Config.live_window_reports = False` to silence it. The end-of-generation
markdown report in `runs/gen{G}_report.md` is still written regardless.

---

## Module map

| file | responsibility |
|---|---|
| `config.py` | all tunables (population, windows, timeouts, thresholds, paths, model) |
| `models.py` | `PollSnapshot`, `WindowData`, `Fill`, `Decision`, `TradeResult`, `Stats` |
| `sandbox.py` | AST validation + restricted `exec` + hard timeout |
| `context.py` | the `ctx` object handed to `decide` (fresh, deep-copied per call) |
| `strategy.py` | `LoadedStrategy`: identity, bankroll, stats, safe `decide`, failure counting |
| `engine.py` | pure accounting: walk book, fees, resolution, scoring |
| `openrouter.py` | chat client + code-block / lineage extraction |
| `prompts.py` | system prompt (market/fees/EV/ctx + worked example) and gen/repair prompts |
| `market.py` | `MarketProvider` protocol + `LiveMarket` (Coinbase + Gamma/CLOB) |
| `generation.py` | run a window, resolve, rank/cull, evolve (validate/repair/dedup) |
| `replay.py` | deterministic re-scoring (also powers the diversity check) |
| `store.py` | SQLite schema + `strategies/` + `runs/` persistence |
| `reporting.py` | per-generation markdown reports + leaderboard formatting |
| `runner.py` | the eternal loop, seed + resume |
| `__main__.py` | the CLI |

---

## Safety: LLM code is untrusted

- **AST validation before exec** (`sandbox.validate`): rejects any import outside
  `{math, statistics}`, any dunder attribute access (`__class__`, `__globals__`,
  …), and any reference to `exec`/`eval`/`open`/`compile`/`__import__`/`os`/`sys`/
  `requests`/`socket`/`subprocess`/`getattr`/… .
- **Restricted execution**: the strategy runs with a minimal `__builtins__` (no
  `open`/`exec`/`eval`; `__import__` only admits the allowlist) — defense in depth
  even if validation were bypassed.
- **Hard timeout**: `decide()` runs under a 1-second `SIGALRM` limit (interrupts
  even a pure-Python infinite loop). Exceptions and timeouts are caught; **3
  failures auto-retire** the strategy with the reason logged.
- **One repair attempt**: code that fails validation is sent back to OpenRouter
  once with the error; if it still fails, the slot is skipped.

---

## Market plumbing (behaviors preserved from the spec)

- **Discovery** defaults to **Synthesis** (the actual trading venue): it lists
  `GET /api/v1/polymarket/markets?title=Bitcoin Up or Down`, filters to markets
  whose parsed start→end span is exactly **5 minutes** (so hourly/15-min markets
  are skipped), and reads the `Up`/`Down` token ids straight from
  `left/right_outcome`+`left/right_token_id`. It falls back to Gamma `/markets`
  (with `end_date_min=now`) if Synthesis returns nothing. Set
  `EVOLVER_MARKET_SOURCE=polymarket` to force Gamma. Order books likewise prefer
  the Synthesis `/markets/orderbooks` feed, then the WS book, then the Gamma CLOB.
  `python -m evolver synthesis-markets` prints exactly what Synthesis is listing
  so you can double-check.
- **Token mapping** comes from the CLOB `/markets/{conditionId}` response, where
  each token has an explicit `"outcome"` label — **never** from Gamma's parallel
  `outcomes`/`clobTokenIds` arrays, which have shipped flipped.
- **Candles/spot** come from **Coinbase Exchange** (Binance blocks US IPs). The
  forming candle is never fed to a strategy.
- **Live feed is WebSocket-first** (see below): during `run`, order books and spot
  stream in real time; REST is the fallback and the source for closed candles.
- **Resolution** is authoritative from Polymarket's **official** Gamma
  `outcomePrices`, and the loop **blocks until it is available** (default
  `resolution_timeout_seconds=None` = wait indefinitely, with a heartbeat log; set
  a number to cap the wait). These 5-minute markets do **not** reliably match the
  Coinbase 5m candle, which is kept only as a diagnostic and used as a fallback
  *only* if a finite timeout is set and elapses. Any Coinbase-vs-official mismatch
  is flagged/logged. This same resolution drives both the paper `run` scoring and
  `calibrate`, so strategies are always evaluated on real settlement.

---

## Live data feed (WebSocket)

For the live `run`, market data streams over WebSockets so every poll reads
always-fresh, near-zero-latency state instead of a REST snapshot up to a poll old:

- **Order books** — Polymarket CLOB market channel
  (`wss://ws-subscriptions-clob.polymarket.com/ws/market`, public): a background
  thread maintains each token's book from a `book` snapshot plus `price_change`
  deltas, re-subscribing to the new tokens each window.
- **Spot** — Coinbase Exchange `ticker`
  (`wss://ws-feed.exchange.coinbase.com`, public).
- **REST fallback** — if a stream is disconnected or its last update is older than
  `ws_staleness_seconds`, that poll falls back to the existing REST path, so a
  book/spot is never missing. Closed 1-min candles and resolution always use REST.

The WS layer lives in `polybot/streaming.py` (socket I/O kept thin; all book
parsing/mutation is pure, unit-tested functions). Total WS failure is non-fatal —
the run simply degrades to REST. Set `Config.use_websocket = False` to force REST.

Requires the **`websocket-client`** package (`pip install websocket-client` — note
the hyphen; the unrelated `websocket` package is *not* it). If it isn't installed
the streams disable themselves and everything falls back to REST automatically, so
it's effectively optional — installing it just cuts latency.

Polls fire at a **constant, drift-free cadence** (anchored to `open + k·poll_interval`
on a monotonic clock), with one guaranteed final poll `final_poll_lead_seconds`
before close so late-window strategies still act in the closing seconds.

---

## Parameter fine-tuning (`tune`)

The main loop searches over strategy *structures* (LLM-written logic), which is
noisy. `tune` instead fixes one strategy's logic and searches over its numeric
*parameters* — a continuous space that converges much faster on 50-window samples.

```bash
python -m evolver tune --strategy open_reversion_early --variants 30 --keep 10
# or from a file, or a manual template:
python -m evolver tune --strategy-file examples/foo.py
python -m evolver tune --template t.py --params ranges.json   # no LLM
```

- **Parameterize:** one OpenRouter call turns the strategy into a template with
  `{{param}}` placeholders + ranges (`min/max/type`), e.g. `ask_cap ∈ [0.30,0.55]`,
  `enter_after ∈ [30,170]`. (Or supply `--template`/`--params` yourself — no LLM.)
- **Search:** the population is N variants of the *same* logic with different
  parameter sets. Gen 1 samples the space; gen 2+ keeps the top K by net P&L and
  breeds the rest by nudging survivors' params (small Gaussian steps) plus a little
  fresh exploration. Pure numeric mutation — **no LLM after setup**.
- **Forward-test** all N over the same 50 windows via the normal engine (shared
  candles/books/Synthesis resolution, background overlap), rank, mutate, repeat.
- Runs in its own `tune_{name}/` data dir (never mixes with the main run); the
  template + ranges are saved, and each generation writes `runs/tune_{name}_gen{G}.md`
  with the ranked variants and best parameters. `--generations N` to bound it.

---

## Calibration: how accurate is the paper trade? (`calibrate`)

`calibrate` measures the paper simulation against reality using **real money at a
tiny stake**. It runs a driver strategy on the live market and, each time the
strategy enters, it BOTH simulates the fill (paper) *and* places a real MARKET
order via **[Synthesis](https://api.synthesis.trade/docs)** — against the same
book at the same instant. After each window resolves, both are scored identically
and the differences (fill price, fee, net P&L) are logged, so you see exactly how
optimistic/pessimistic the sim is.

```bash
# .env: SYNTHESIS_API_KEY=...  SYNTHESIS_WALLET_ID=...   (funded wallet)
python -m evolver calibrate --yes \
    --trades 12 --stake 1 \
    --strategy-file examples/range_position_revert.py
```

Order placement uses `POST /api/v1/wallet/pol/{wallet_id}/order`
(`type=MARKET`, `units=USDC`) with a slippage-cap price; the real fill's price,
shares, and fee come straight from the response. Output: a per-trade table plus
aggregate accuracy (mean fill-price error, fee error, and **net-P&L bias =
real − paper**, where negative means the paper trade is optimistic) at
`runs/calibration_report.md`, with every row (incl. the raw order response)
persisted to the `calibration` table.

**Safety:** real orders fire only with `--yes` **and** credentials set; every
order carries a slippage cap; the stake is capped by `max_live_stake` (default
$5); and repeated order failures abort the run. The driver defaults to a built-in
"buy Up at open" probe (trades every window); `--strategy-file` / `--strategy`
override it. Note a *selective* driver (like `range_position_revert`) may pass
many windows, so collecting N real trades can take a while — only windows where it
actually trades count toward N. The rest of evolver stays **paper-only**; this is
the one command that touches real funds.

---

## Persistence — everything repeatable

`evolver.sqlite` plus a `runs/` and `strategies/` directory (all under
`EVOLVER_DATA_DIR`, default `.`):

- **Every strategy ever created**: full source in `strategies/gen{G}_{name}.py`,
  the exact prompt sent to OpenRouter, the raw model response, the model slug,
  timestamp, parent lineage, and a SHA-256 content hash.
- **Every window**: candle-state hash, full poll snapshots (books included),
  every strategy's decision *including passes*, fills, fees, and outcomes.
- **Per generation**: a leaderboard at `runs/gen{G}_report.md` (per-strategy
  trades, hit%, avg breakeven, net P&L, bankroll, lifetime stats, lineage).
- **`replay NAME`** reconstructs the exact `ctx` from the logs and re-derives
  identical decisions, fills, and P&L — deterministic by construction, and the
  test suite asserts it reproduces the recorded lifetime P&L to the cent.

---

## Design decisions (made without asking, per the brief)

- **Culling ranks by lifetime RISK-ADJUSTED P&L (Sharpe-style).** The survival
  score is `mean per-trade P&L ÷ P&L volatility`, computed on **lifetime** stats
  (`generation.risk_adjusted_score`). This deliberately replaced ranking by a single
  generation's raw net P&L, which had three failure modes seen in practice: (1) one
  unlucky 50-window generation culled a proven strategy; (2) raw P&L rewarded
  variance — a strategy that profited only from rare longshot jackpots outranked a
  steady high-hit-rate earner; and (3) in a losing generation a *do-nothing*
  strategy (0 P&L) outranked strategies that traded and lost. Risk-adjustment fixes
  all three: consistent earners beat volatile ones, lifetime stats smooth out single
  generations, and a strategy that never traded scores `-inf`. Two guards keep it
  robust — a volatility floor (`risk_vol_floor`) stops 1–2 identical trades posting
  an infinite score, and small-sample shrinkage `trades/(trades+risk_trade_prior)`
  stops a couple of lucky trades topping the board. The Sharpe *ratio* doesn't
  inflate with trade count, so older strategies get no unfair head start.
  **Generations-survived** and lifetime totals still drive the human-facing
  `leaderboard`.
- **Tiebreak** = `hit_fraction (0–1) − avg_breakeven (price 0–1)`, both on a
  comparable scale.
- **Fee is charged on filled shares at the volume-weighted average fill price**,
  so a strategy's `ctx.breakeven` estimate matches what it actually pays.
- **Book participation cap (`book_participation`, default 0.25).** We assume only a
  fraction of each level's *displayed* size is actually executable for us — displayed
  size isn't all real or all ours (stale quotes, spoofing, competing takers), so
  sweeping 100% of a level is optimistic. This bites only on **thin** books: a $10
  order against 100 displayed shares at `0.10` now gets a **partial fill** (25 shares,
  ~$22 win) instead of the whole level (~$90 phantom win), while deep books are
  untouched (25% of thousands of shares still covers $10) — preserving the fill
  accuracy live calibration measured at normal 0.40–0.60 prices. The cross-book floor
  and the price guard do **not** cover this case: when large size is displayed *at the
  touch* the walk never rises above the best ask and the book can be perfectly
  price-consistent, yet the depth may not really be there.
- **Fills are corrected for phantom liquidity at cheap "longshot" prices.** Walking
  the *displayed* ask book overstates fills at extreme-cheap prices, because that
  displayed liquidity is largely phantom/stale — live calibration showed a `0.06`
  displayed fill actually executing near `0.17`. Two corrections (`engine.py`):
  - **Cross-book no-arb reconstruction (default, exact).** On a binary Up+Down=$1, a
    maker bidding `cb` for the *other* side is implicitly offering this side at
    `1 − cb`, so the executable ask can't sit below `1 − best_complement_bid`. The
    fill clamps to that per-window floor, computed from the complement book we already
    capture each poll. This is exact for each window (not an average), needs no
    fitting, and reproduces the calibration (Up bid `0.83` → Down fills `0.17`). Both
    sides' books (asks **and** bids) are captured in every `PollSnapshot`, so this is
    fully deterministic under `replay`.
  - **Slippage curve (fallback).** When the complement book is missing, worsen price
    by `slippage_coeff·(0.5 − price)^slippage_exp` for sub-0.50 entries.

  Same dollars fill at the worse price → fewer shares → the win pays less. Without
  this, longshot strategies looked profitable on paper for fills the market never
  gives, and the ranking rewarded a distortion. `calibrate` uses the *same* model, so
  its real−paper P&L residual measures accuracy (centre on 0). Set
  `use_cross_book_fill = False` and `slippage_coeff = 0` for the raw idealised walk.
- **A strategy is not called again after it enters** a window (one entry max, no
  exits) — its pre-entry passes are still logged.
- **Bankroll guard**: a strategy that can't afford the $10 stake takes a forced
  pass (logged). Bankroll is updated at resolution (`+= net_pnl`).
- **Diversity**: a new strategy's per-window action vector is compared to existing
  ones over the last 50 archived windows; if it agrees on `> duplicate_threshold`
  (default 85%), it's rejected and a replacement is requested once. Skipped on the
  first generation (no archive yet). Agreement is measured **only over windows
  where at least one of the two strategies trades** — two strategies that both
  pass a window aren't "agreeing" — so thematic clones that fade the same handful
  of moves are caught even if they pass most windows.
- **Monoculture guard**: the evolution prompt inspects the survivors and, when
  they over-concentrate in one family (e.g. a majority of mean-reversion/fade
  strategies — which all lose together the moment the market trends), it demands
  the majority of the next batch come from *other* families (momentum /
  trend-continuation, book-pressure, breakout, time-of-day) so the population
  survives both trending and ranging regimes.
- **Lineage convention**: each generated block starts with `# lineage: novel` or
  `# lineage: parent_a, parent_b`, parsed into the strategy's lineage.
- **Auto-retired / failed strategies always sink below survivors** regardless of
  P&L. If auto-retirements exceed the normal bottom-5, more replacements are bred
  so the population returns to size.
- **Population always refills to `population_size`**: each generation breeds
  `population_size − survivors` new strategies. Because a single OpenRouter reply
  can under-deliver (blocks that fail the sandbox or are near-duplicates), breeding
  retries up to `max_breed_attempts` (default 3), asking only for the shortfall each
  round, so the population never silently shrinks. **Note:** if `survivors ==
  population_size` nothing is culled and nothing new is bred — keep `survivors <
  population_size` (default 25 vs 50) for fresh strategies every generation. On a
  large population the evolution prompt shows **full source only for the top
  `MAX_SOURCE_SURVIVORS`/`MAX_SOURCE_RETIREES`** (10/8) and stat lines for the rest,
  so the prompt stays a sane size; seeding retries the shortfall too, since one reply
  rarely returns 50 valid blocks.
- **Resume**: `run` reloads the alive population from SQLite and continues at the
  next unfinished generation; an interrupted run loses nothing already persisted.
  If the alive count is below `population_size` (e.g. you raised the target), the
  resumed run breeds up to the target *before* running the generation rather than
  waiting a full cycle.
- **Default model** is `anthropic/claude-fable-5`; override with `EVOLVER_MODEL`
  or `Config.model`.

---

## Configuration

The CLI auto-loads a **`.env`** file (discovered by walking up from the current
directory) before reading configuration — copy `.env.example` to `.env` and fill
it in. Real environment variables take precedence over `.env` values.

| env var | meaning |
|---|---|
| `OPENROUTER_API_KEY` | required for `run` |
| `EVOLVER_MODEL` | OpenRouter model slug (default `anthropic/claude-fable-5`) |
| `EVOLVER_DATA_DIR` | where `evolver.sqlite`, `runs/`, `strategies/` live (default `.`) |

```bash
cp .env.example .env         # then edit OPENROUTER_API_KEY
python -m evolver run                 # 50 windows/generation (default)
python -m evolver run --windows 25    # shorter generations, faster turnover
python -m evolver run --generations 3 # stop after 3 generations
```

Each generation forward-tests the population over `--windows` resolved 5-minute
windows (default **50**). 50 is a deliberate trade-off — small enough that a
single generation's winner is often luck (hence lifetime stats and
generations-survived drive the leaderboard, not the per-gen cull), but large
enough to separate signal from noise. Drop it to 25 for faster iteration when
you're experimenting; keep 50 for the real run.

All numeric knobs (population size, windows/generation, stake, bankroll, timeout,
thresholds) live in `evolver/config.py`, including the live-feed settings:
`use_websocket` (default on), `ws_staleness_seconds` (REST-fallback threshold),
`poll_interval_seconds` (constant cadence), and `final_poll_lead_seconds`.

---

## Tests

```
pip install pytest
python -m pytest        # from the repo root
```

Covers the four areas the brief asks for: the **AST sandbox** (malicious code
rejected, safe code runs, timeouts caught), the **fill/fee/resolution
accounting**, the **ranking + carry-forward** logic, and a **full offline
generation cycle** with a mocked OpenRouter and mocked market data — including
persistence, deterministic replay, decision logging, and near-duplicate
rejection.
