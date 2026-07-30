"""Command-line arbitrage scanner (read-only).

    python -m arb intra --venue polymarket   # both-sides arb within single markets
    python -m arb intra --venue kalshi
    python -m arb cross                       # same event priced apart across venues

Nothing here places an order. It reports whether real, executable arbs exist after
fees — measure before risking capital.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List

from polybot.synthesis import SynthesisClient

from . import match as matcher
from . import detect, paper, scan
from .model import ArbOpportunity

try:  # reuse the evolver .env loader if present (same repo)
    from evolver.env import load_dotenv
except Exception:  # noqa: BLE001
    def load_dotenv():  # type: ignore
        return {}


def _client() -> SynthesisClient:
    return SynthesisClient(
        api_key=os.environ.get("SYNTHESIS_API_KEY", ""),
        wallet_id=os.environ.get("SYNTHESIS_WALLET_ID", ""),
        base_url=os.environ.get("SYNTHESIS_BASE_URL", "https://synthesis.trade"),
    )


def _fmt(opp: ArbOpportunity, sim: float | None = None) -> str:
    if len(opp.legs) <= 4:
        legs = "  +  ".join(f"{leg.outcome}@{leg.ask:.3f} [{leg.venue}]" for leg in opp.legs)
    else:
        legs = f"{len(opp.legs)} legs, Σask {opp.gross_cost:.3f} [{opp.legs[0].venue}]"
    tag = f" · match~{sim:.0%}" if sim is not None else ""
    if opp.sum_mid is not None:
        tag += f" · Σmid {opp.sum_mid:.3f} (field completeness)"
    return (
        f"[{opp.kind}] edge {opp.edge*100:+.2f}%/set · net ${opp.net_cost:.3f} "
        f"(gross {opp.gross_cost:.3f} + fee {opp.fees:.3f}) · size {opp.max_size:.0f} "
        f"· maxprofit ${opp.max_profit:.2f}{tag}\n    {legs}\n    {opp.title[:90]}"
    )


def cmd_intra(args) -> int:
    opps = scan.scan_intra(_client(), args.venue, args.max_markets, args.min_edge)
    print(f"\n{args.venue}: {len(opps)} intra-market arb(s) with edge > {args.min_edge*100:.2f}%\n")
    for o in opps[: args.top]:
        print(_fmt(o) + "\n")
    if not opps:
        print("(none — markets are internally consistent after fees)")
    return 0


def cmd_cross(args) -> int:
    results = scan.scan_cross(_client(), max_markets=args.max_markets, min_shared=args.min_shared,
                              min_edge=args.min_edge)
    print(f"\ncross-venue: {len(results)} candidate arb(s) with edge > {args.min_edge*100:.2f}%\n")
    for o, sim in results[: args.top]:
        print(_fmt(o, sim) + "\n")
    if results:
        print("NOTE: cross-venue arbs are only real if BOTH venues resolve the event\n"
              "identically (same reference price, window, tie-break). Confirm the two\n"
              "markets above are truly the same question before trusting the edge.")
    else:
        print("(no cross-venue arbs found among matched events)")
    return 0


def _llm():
    """Build an OpenRouter client from env, or None if no key / import fails."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        from evolver.openrouter import OpenRouterClient
        return OpenRouterClient(
            api_key=key,
            model=os.environ.get("EVOLVER_MODEL", "anthropic/claude-fable-5"),
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    except Exception:  # noqa: BLE001
        return None


def cmd_match(args) -> int:
    """Match the SAME event across Polymarket & Kalshi (semantic/LLM), show any spread."""
    llm = None if args.no_llm else _llm()
    if llm is None and not args.no_llm:
        print("(no OPENROUTER_API_KEY — falling back to fuzzy, UNCONFIRMED matches)\n", file=sys.stderr)
    matches = matcher.match_venues(_client(), llm, target=args.target, min_shared=args.min_shared,
                                   max_pairs=args.max_pairs, min_confidence=args.min_confidence)
    print(f"\n{len(matches)} matched event(s):\n")
    # Fetch books once for all matched markets, then compute the cross-venue spread.
    mkts = [m.poly for m in matches] + [m.kalshi for m in matches]
    books = scan.fetch_books(_client(), [t for m in mkts for t in m.token_ids])
    detect.apply_orderbooks(mkts, books, realistic=True)
    for em in matches[: args.top]:
        opp = detect.cross_venue_arb([em.poly, em.kalshi], realistic=True)
        edge = f"edge {opp.edge*100:+.2f}%/set" if opp else "no book / no spread"
        print(f"~{em.confidence:.0%}  {edge}")
        print(f"    POLY  : {em.poly.title!r}  ({'/'.join(q.outcome for q in em.poly.quotes)})")
        print(f"    KALSHI: {em.kalshi.title!r}  ({'/'.join(q.outcome for q in em.kalshi.quotes)})")
        if em.reason:
            print(f"    why: {em.reason}")
        print()
    if not matches:
        print("(no matches — try --min-shared 1 or a larger --target)")
    else:
        print("NOTE: confirm settlement equivalence (same reference source/time) before\n"
              "trading any pair — the LLM checks this but is not infallible.")
    return 0


def cmd_field(args) -> int:
    opps = scan.scan_field(_client(), args.venue, args.max_markets, args.min_edge)
    print(f"\n{args.venue}: {len(opps)} multi-outcome field arb(s) with edge > {args.min_edge*100:.2f}%\n")
    for o in opps[: args.top]:
        print(_fmt(o) + "\n")
    if opps:
        print("NOTE: a field lock is only real if the field is COMPLETE (every possible\n"
              "outcome is in the book). Σmid≈1 suggests completeness, but confirm no\n"
              "outcome is missing before trusting it — a missing winner pays you $0.")
    else:
        print("(no field arbs — fields sum to >= $1 after fees, as expected on liquid books)")
    return 0


def cmd_sample(args) -> int:
    """Dump how a venue names its markets — so we can design event-matching on reality."""
    markets = scan.list_all(_client(), args.venue, max_markets=args.n)
    print(f"\n{args.venue}: showing {min(args.n, len(markets))} of {len(markets)} live markets\n")
    for m in markets[: args.n]:
        outs = "/".join(q.outcome for q in m.quotes)
        raw = m.raw
        tags = raw.get("tags") or raw.get("category") or raw.get("slug") or ""
        print(f"- {m.title!r}")
        print(f"    outcomes={outs}  ends_at={m.ends_at}  liq={m.liquidity:.0f} vol={m.volume:.0f}"
              f"  id={m.market_id}")
        if tags:
            print(f"    tags/slug={tags}")
    return 0


def cmd_paper(args) -> int:
    venues = ["polymarket", "kalshi"] if args.venue == "both" else [args.venue]
    print(f"Paper-trading intra-market arb on {', '.join(venues)} with REALISTIC fills.\n"
          f"bankroll ${args.bankroll:.0f} · min-edge {args.min_edge*100:.2f}% · "
          f"scan every {args.interval:.0f}s · Ctrl-C to stop.\n")
    try:
        paper.run_paper(_client(), venues, bankroll=args.bankroll, interval=args.interval,
                        min_edge=args.min_edge, per_arb_cap=args.per_arb_cap,
                        max_markets=args.max_markets, include_field=not args.no_field)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arb", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("intra", help="single-market both-sides arb across a whole venue")
    pi.add_argument("--venue", choices=["polymarket", "kalshi"], default="polymarket")
    pi.add_argument("--min-edge", type=float, default=0.0, help="min edge/set (0.005 = 0.5%%)")
    pi.add_argument("--max-markets", type=int, default=1000)
    pi.add_argument("--top", type=int, default=25)
    pi.set_defaults(func=cmd_intra)

    pc = sub.add_parser("cross", help="same event priced apart across Polymarket & Kalshi")
    pc.add_argument("--min-edge", type=float, default=0.0, help="min net edge/set (0.005=0.5%%)")
    pc.add_argument("--min-shared", type=int, default=2, help="min shared title words to pair markets")
    pc.add_argument("--max-markets", type=int, default=20000, help="markets to pull per venue (full universe)")
    pc.add_argument("--top", type=int, default=40)
    pc.set_defaults(func=cmd_cross)

    pp = sub.add_parser("paper", help="paper-trade intra-market arb with realistic fills")
    pp.add_argument("--venue", choices=["polymarket", "kalshi", "both"], default="polymarket")
    pp.add_argument("--bankroll", type=float, default=500.0)
    pp.add_argument("--min-edge", type=float, default=0.005, help="min executable edge/set (0.005=0.5%%)")
    pp.add_argument("--interval", type=float, default=30.0, help="seconds between scans")
    pp.add_argument("--per-arb-cap", type=float, default=200.0, help="max shares per single arb")
    pp.add_argument("--max-markets", type=int, default=1000)
    pp.add_argument("--no-field", action="store_true", help="only intra-market arb, skip field arbs")
    pp.set_defaults(func=cmd_paper)

    pf = sub.add_parser("field", help="multi-outcome field arb (buy every outcome of an event < $1)")
    pf.add_argument("--venue", choices=["polymarket", "kalshi"], default="kalshi")
    pf.add_argument("--min-edge", type=float, default=0.0)
    pf.add_argument("--max-markets", type=int, default=1000)
    pf.add_argument("--top", type=int, default=25)
    pf.set_defaults(func=cmd_field)

    pm = sub.add_parser("match", help="match the SAME event across Polymarket & Kalshi (semantic/LLM)")
    pm.add_argument("--target", type=int, default=3000, help="markets to pull per venue")
    pm.add_argument("--min-shared", type=int, default=2, help="min shared title words to consider a pair")
    pm.add_argument("--max-pairs", type=int, default=1500, help="cap candidate pairs sent to the LLM")
    pm.add_argument("--min-confidence", type=float, default=0.6)
    pm.add_argument("--no-llm", action="store_true", help="fuzzy only, skip LLM confirmation")
    pm.add_argument("--top", type=int, default=40)
    pm.set_defaults(func=cmd_match)

    ps = sub.add_parser("sample", help="dump how a venue names its markets (for matching design)")
    ps.add_argument("--venue", choices=["polymarket", "kalshi"], default="polymarket")
    ps.add_argument("--n", type=int, default=40)
    ps.set_defaults(func=cmd_sample)
    return p


def main(argv: List[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    load_dotenv()
    args = build_parser().parse_args(argv)
    if not os.environ.get("SYNTHESIS_API_KEY"):
        print("WARNING: SYNTHESIS_API_KEY not set — market-data endpoints may be public, "
              "but set it in .env if you get 401s.", file=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
