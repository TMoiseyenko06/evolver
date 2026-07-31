"""Command-line interface.

    python -m mispricing backtest [--recent N | --generations 3,4,5]
        # Validate against ALREADY-COLLECTED evolver windows (read-only, zero risk):
        # prints with-exits vs hold-to-resolution baseline, side by side.

    python -m mispricing paper [--max-windows N] [--report PATH]
        # Live paper-trading loop (no real money — see mispricing/README.md for why
        # real-money exit execution is explicitly out of scope for now).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from evolver.config import Config
from evolver.env import find_dotenv, load_dotenv
from evolver.market import LiveMarket
from evolver.store import Store

from . import backtest as bt
from . import paper as pp
from .config import MispricingParams
from .reporting import format_backtest_report


def _params_from_args(args) -> MispricingParams:
    return MispricingParams(
        lookback_candles=args.lookback_candles,
        entry_gap=args.entry_gap,
        fee_margin=args.fee_margin,
        time_expired_seconds=args.time_expired_seconds,
        gap_closed_min_gain=args.gap_closed_min_gain,
    )


def _config_from_args(args) -> Config:
    config = Config()
    config.stake = args.stake
    config.book_participation = args.book_participation
    config.max_slippage = args.max_slippage
    config.slippage_coeff = args.slippage_coeff
    config.slippage_exp = args.slippage_exp
    config.use_cross_book_fill = not args.no_cross_book_fill
    if args.data_dir:
        from pathlib import Path
        config.data_dir = Path(args.data_dir)
    return config


def cmd_backtest(args) -> int:
    config = _config_from_args(args)
    params = _params_from_args(args)
    store = Store(config)
    if args.generations:
        gens = [int(g) for g in args.generations.split(",")]
        windows = store.windows_for_generations(gens)
    else:
        windows = store.recent_windows(args.recent)
    store.close()

    if not windows:
        print("No archived windows found — is --data-dir pointed at a populated evolver run?",
              file=sys.stderr)
        return 1

    report = bt.run_backtest(windows, config, params)
    print(format_backtest_report(report))
    return 0


def cmd_sweep(args) -> int:
    """Rerun the backtest across several --entry-gap values against the SAME set of
    already-collected windows, to see where entries stop being net-negative before
    exits even get involved."""
    config = _config_from_args(args)
    store = Store(config)
    if args.generations:
        gens = [int(g) for g in args.generations.split(",")]
        windows = store.windows_for_generations(gens)
    else:
        windows = store.recent_windows(args.recent)
    store.close()

    if not windows:
        print("No archived windows found — is --data-dir pointed at a populated evolver run?",
              file=sys.stderr)
        return 1

    gaps = [float(g) for g in args.entry_gap.split(",")]
    print(f"=== mispricing: entry_gap sweep over {len(windows)} window(s) ===\n")
    print(f"{'entry_gap':>10}{'trades':>8}{'wins':>6}{'hit%':>7}{'net_pnl':>12}{'baseline':>12}{'delta':>10}")
    for gap in gaps:
        params = MispricingParams(
            lookback_candles=args.lookback_candles, entry_gap=gap, fee_margin=args.fee_margin,
            time_expired_seconds=args.time_expired_seconds, gap_closed_min_gain=args.gap_closed_min_gain,
        )
        report = bt.run_backtest(windows, config, params)
        we, base = report.with_exits, report.baseline_no_exits
        print(f"{gap:>10.3f}{we.trades:>8}{we.wins:>6}{we.hit_pct*100:>6.1f}%"
              f"{we.net_pnl:>+12.2f}{base.net_pnl:>+12.2f}{report.pnl_delta:>+10.2f}")
    return 0


def cmd_paper(args) -> int:
    config = _config_from_args(args)
    config.starting_bankroll = args.bankroll
    params = _params_from_args(args)
    market = LiveMarket(config)
    print(f"Paper-trading the mispricing strategy · bankroll ${args.bankroll:.0f} · "
          f"entry_gap {params.entry_gap} · exits: gap_closed/time_expired({params.time_expired_seconds}s)"
          f"/adverse_move · Ctrl-C to stop.\n")
    try:
        book = pp.run_paper(market, config, params, max_windows=args.max_windows)
    except KeyboardInterrupt:
        book = None
        print("\nInterrupted.")
    finally:
        market.close()
    if args.report and book is not None:
        pp.write_report(book, args.report)
        print(f"Report written to {args.report}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mispricing", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_model_flags(p):
        p.add_argument("--lookback-candles", type=int, default=5)
        p.add_argument("--entry-gap", type=float, default=0.08)
        p.add_argument("--fee-margin", type=float, default=0.05)
        p.add_argument("--time-expired-seconds", type=float, default=60.0)
        p.add_argument("--gap-closed-min-gain", type=float, default=0.0)

    def add_fill_flags(p):
        p.add_argument("--stake", type=float, default=10.0)
        p.add_argument("--book-participation", type=float, default=0.25)
        p.add_argument("--max-slippage", type=float, default=0.10)
        p.add_argument("--slippage-coeff", type=float, default=0.55)
        p.add_argument("--slippage-exp", type=float, default=2.0)
        p.add_argument("--no-cross-book-fill", action="store_true")
        p.add_argument("--data-dir", default=None)

    p_bt = sub.add_parser("backtest", help="validate against already-collected evolver windows")
    p_bt.add_argument("--recent", type=int, default=500, help="most recent N archived windows")
    p_bt.add_argument("--generations", default=None, help="comma-separated generation numbers")
    add_model_flags(p_bt)
    add_fill_flags(p_bt)
    p_bt.set_defaults(func=cmd_backtest)

    p_sweep = sub.add_parser("sweep", help="sweep --entry-gap values over the SAME already-collected windows")
    p_sweep.add_argument("--recent", type=int, default=500, help="most recent N archived windows")
    p_sweep.add_argument("--generations", default=None, help="comma-separated generation numbers")
    p_sweep.add_argument("--entry-gap", default="0.04,0.06,0.08,0.10,0.12,0.15",
                         help="comma-separated entry_gap values to try")
    p_sweep.add_argument("--lookback-candles", type=int, default=5)
    p_sweep.add_argument("--fee-margin", type=float, default=0.05)
    p_sweep.add_argument("--time-expired-seconds", type=float, default=60.0)
    p_sweep.add_argument("--gap-closed-min-gain", type=float, default=0.0)
    add_fill_flags(p_sweep)
    p_sweep.set_defaults(func=cmd_sweep)

    p_paper = sub.add_parser("paper", help="live paper-trading loop (no real money)")
    p_paper.add_argument("--bankroll", type=float, default=500.0)
    p_paper.add_argument("--max-windows", type=int, default=None)
    p_paper.add_argument("--report", default=None, help="write an end-of-run markdown summary here")
    add_model_flags(p_paper)
    add_fill_flags(p_paper)
    p_paper.set_defaults(func=cmd_paper)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    dotenv_path = find_dotenv()
    loaded = load_dotenv()
    if loaded:
        logging.getLogger("mispricing").info("loaded %d var(s) from %s", len(loaded), dotenv_path)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
