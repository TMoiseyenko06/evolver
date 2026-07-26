"""Cross-venue prediction-market arbitrage over Synthesis (Polymarket + Kalshi).

Directional trading on efficient markets has no edge after realistic fills (the
evolver proved this). Arbitrage exploits a different inefficiency: buying a
complete set of a market's mutually-exclusive outcomes for less than $1 — a
guaranteed profit independent of the result. Two flavours:

- **intra-market**: both sides of ONE binary market (ask_Yes + ask_No < 1). No
  event-matching, no cross-venue resolution risk — the cleanest, safest lock.
- **cross-venue**: the SAME event listed on both venues, buying the cheapest Yes
  and cheapest No across them. Bigger, more persistent edges, but only a true arb
  when the two venues resolve the event identically (see ``detect.match_events``).

This package is read-only by design (measure before risking capital); execution is
a separate, explicitly-gated step.
"""
