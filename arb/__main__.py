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

from . import scan
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
    legs = "  +  ".join(
        f"{leg.outcome}@{leg.ask:.3f} [{leg.venue}]" for leg in opp.legs
    )
    tag = f" · match~{sim:.0%}" if sim is not None else ""
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
    results = scan.scan_cross(_client(), args.max_markets, args.min_similarity, args.min_edge)
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
    pc.add_argument("--min-edge", type=float, default=0.0)
    pc.add_argument("--min-similarity", type=float, default=0.6, help="title-match threshold 0-1")
    pc.add_argument("--max-markets", type=int, default=1000)
    pc.add_argument("--top", type=int, default=25)
    pc.set_defaults(func=cmd_cross)
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
