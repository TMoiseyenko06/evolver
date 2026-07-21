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
python -m evolver reset --yes   # wipe db + runs/ + strategies/
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
   configurable, default `anthropic/claude-opus-4.8`) is prompted to emit
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

- **Discovery** hits Gamma `/markets` with active/closed/archived filters **and
  `end_date_min=now`** (without it Gamma returns months-old stale markets). The
  window is parsed from the title (`"Bitcoin Up or Down - July 20,
  3:00PM-3:05PM ET"`, America/New_York, year inferred). Discovery retries if the
  listing is late.
- **Token mapping** comes from the CLOB `/markets/{conditionId}` response, where
  each token has an explicit `"outcome"` label — **never** from Gamma's parallel
  `outcomes`/`clobTokenIds` arrays, which have shipped flipped.
- **Candles/spot** come from **Coinbase Exchange** (Binance blocks US IPs). The
  forming candle is never fed to a strategy.
- **Resolution** scores immediately from the Coinbase 5m candle (`close > open`
  → Up, tie → Down), then reconciles against Gamma's official `outcomePrices`;
  the official side wins and any mismatch is flagged/logged (trades are scored to
  the official side — i.e. mismatched paper trades are effectively flipped).

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

- **Culling ranks by *this generation's* net P&L, not lifetime.** All strategies
  see identical data each generation, so head-to-head on that shared data is the
  fair comparison and gives new strategies a real shot against entrenched
  survivors. **Lifetime** stats and **generations-survived** — the noise-robust
  signal the spec calls out ("50 windows is small enough that one generation's
  winner is often luck") — drive the human-facing `leaderboard`, not the cull.
- **Tiebreak** = `hit_fraction (0–1) − avg_breakeven (price 0–1)`, both on a
  comparable scale.
- **Fee is charged on filled shares at the volume-weighted average fill price**,
  so a strategy's `ctx.breakeven` estimate matches what it actually pays.
- **A strategy is not called again after it enters** a window (one entry max, no
  exits) — its pre-entry passes are still logged.
- **Bankroll guard**: a strategy that can't afford the $10 stake takes a forced
  pass (logged). Bankroll is updated at resolution (`+= net_pnl`).
- **Diversity**: a new strategy's per-window action vector is compared to existing
  ones over the last 50 archived windows; if it agrees on `> duplicate_threshold`
  (default 90%), it's rejected and a replacement is requested once. Skipped on the
  first generation (no archive yet).
- **Lineage convention**: each generated block starts with `# lineage: novel` or
  `# lineage: parent_a, parent_b`, parsed into the strategy's lineage.
- **Auto-retired / failed strategies always sink below survivors** regardless of
  P&L. If auto-retirements exceed the normal bottom-5, more replacements are bred
  so the population returns to size.
- **Resume**: `run` reloads the alive population from SQLite and continues at the
  next unfinished generation; an interrupted run loses nothing already persisted.
- **Default model** is `anthropic/claude-opus-4.8`; override with `EVOLVER_MODEL`
  or `Config.model`.

---

## Configuration

The CLI auto-loads a **`.env`** file (discovered by walking up from the current
directory) before reading configuration — copy `.env.example` to `.env` and fill
it in. Real environment variables take precedence over `.env` values.

| env var | meaning |
|---|---|
| `OPENROUTER_API_KEY` | required for `run` |
| `EVOLVER_MODEL` | OpenRouter model slug (default `anthropic/claude-opus-4.8`) |
| `EVOLVER_DATA_DIR` | where `evolver.sqlite`, `runs/`, `strategies/` live (default `.`) |

```bash
cp .env.example .env      # then edit OPENROUTER_API_KEY
python -m evolver run
```

All numeric knobs (population size, windows/generation, stake, bankroll, timeout,
thresholds) live in `evolver/config.py`.

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
