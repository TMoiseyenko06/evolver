# evolver

Evolutionary strategy-search for Polymarket's 5-minute Bitcoin Up/Down markets.
An LLM (via OpenRouter) writes trading strategies as Python; a population of them
is forward-tested on live markets with paper money, ranked, and bred forever.

- **Full documentation and design notes:** [`evolver/README.md`](evolver/README.md)
- **Market plumbing** (Coinbase candles, Polymarket discovery/resolution, fees)
  lives in the sibling [`polybot/`](polybot/) package and is imported, not
  reimplemented. See the README's *"Where is polybot?"* section for why it's
  vendored here.

```bash
cp .env.example .env           # then set OPENROUTER_API_KEY (auto-loaded by the CLI)
python -m evolver run          # the eternal loop
python -m evolver leaderboard  # lifetime rankings, generations survived
python -m evolver show NAME     # a strategy's code, lineage, full stat history
python -m evolver replay NAME   # re-score deterministically against archived windows
python -m evolver reset --yes

python -m pytest               # test suite (sandbox, accounting, ranking, full cycle)
```
