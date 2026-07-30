"""A single, hand-written trading idea pulled out of the LLM-generated population.

One evolved strategy (``stale_quote_sweeper``) built a sound fair-value model:
estimate a "fair" probability of Up from recent realized volatility (scaled by
sqrt-time-to-close) and compare it to the market's actual ask, entering only when
the two disagree by more than a fee-adjusted margin. This package extracts that
idea into first-class, hand-written code (not LLM-generated, no AST sandbox — this
is trusted code we wrote and can read) and adds what the evolved-strategy contract
can't support: **early exit**.

Live Synthesis calibration this session showed real fees are effectively $0, so
closing a position early (crossing the spread) is cheap, which makes it worth
locking in a profit once the mispricing closes, or cutting a trade whose thesis has
stalled or reversed, instead of always holding to the coin-flip resolution.

Modules:
    config    -- MispricingParams (the model/exit thresholds; NOT in evolver.config)
    model     -- pure fair-value math, ported from stale_quote_sweeper
    signals   -- entry_signal + the three exit triggers (gap_closed/time_expired/adverse_move)
    runner    -- run_window: one symmetric poll loop, identical live/mock/archived
    backtest  -- validate against ALREADY-COLLECTED evolver windows (read-only, zero risk)
    paper     -- live paper-trading loop (arb/paper.py style: in-memory, no SQLite)
    reporting -- board/summary formatting shared by paper.py and backtest.py

Explicitly out of scope: real-money exit execution. See README.md.
"""
