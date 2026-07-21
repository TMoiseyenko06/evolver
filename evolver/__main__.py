"""Command-line interface.

    python -m evolver run          # the eternal loop
    python -m evolver leaderboard  # lifetime rankings, generations survived
    python -m evolver show NAME    # a strategy's code, lineage, full stat history
    python -m evolver replay NAME  # re-score against archived windows
    python -m evolver reset --yes  # wipe db + runs/ + strategies/
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys

from .config import Config
from .env import find_dotenv, load_dotenv
from .market import LiveMarket
from .openrouter import OpenRouterClient
from .reporting import format_leaderboard
from .replay import replay_strategy
from .runner import run_loop
from .store import Store


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(config: Config, args) -> int:
    if not config.openrouter_api_key:
        print(
            "ERROR: OPENROUTER_API_KEY is not set. Put it in a .env file "
            "(see .env.example) or export it in your environment.",
            file=sys.stderr,
        )
        return 2
    store = Store(config)
    market = LiveMarket(config)
    client = OpenRouterClient(
        api_key=config.openrouter_api_key,
        model=config.model,
        base_url=config.openrouter_base_url,
    )
    try:
        run_loop(market, client, store, config, max_generations=args.generations)
    except KeyboardInterrupt:
        print("\nInterrupted — state is persisted; rerun `python -m evolver run` to resume.")
    finally:
        market.close()
        store.close()
    return 0


def cmd_leaderboard(config: Config, args) -> int:
    store = Store(config)
    print(format_leaderboard(store.leaderboard_rows()))
    store.close()
    return 0


def cmd_show(config: Config, args) -> int:
    store = Store(config)
    row = store.get_strategy_row(args.name)
    if row is None:
        print(f"No strategy named '{args.name}'.", file=sys.stderr)
        store.close()
        return 1
    state = store.get_state_row(args.name) or {}
    lineage = json.loads(row["lineage_json"] or "[]")
    print(f"# {row['name']}")
    print(f"created: generation {row['generation_created']} · model {row['model']}")
    print(f"lineage: {', '.join(lineage) if lineage else 'novel'}")
    print(f"hash:    {row['source_hash']}")
    print(f"alive:   {bool(state.get('alive'))} · bankroll ${state.get('bankroll', config.starting_bankroll):.2f}"
          f" · generations survived {state.get('generations_survived', 0)}")
    if state.get("retired_reason"):
        print(f"retired: {state['retired_reason']}")
    print(f"\n## description\n{row['description']}")
    print("\n## stat history (per generation)")
    hist = store.stat_history(args.name)
    if hist:
        print(f"{'gen':>4} {'trades':>6} {'wins':>5} {'net_pnl':>10} {'bankroll':>10} {'survived':>8} {'rank':>5}")
        for h in hist:
            print(f"{h['generation']:>4} {h['trades']:>6} {h['wins']:>5} {h['net_pnl']:>+10.2f} "
                  f"{h['bankroll_end']:>10.2f} {'yes' if h['survived'] else 'no':>8} {h['rank']:>5}")
    else:
        print("(no completed generations yet)")
    print("\n## source")
    print(row["source"])
    store.close()
    return 0


def cmd_replay(config: Config, args) -> int:
    store = Store(config)
    row = store.get_strategy_row(args.name)
    if row is None:
        print(f"No strategy named '{args.name}'.", file=sys.stderr)
        store.close()
        return 1
    generations = store.generations_for_strategy(args.name)
    windows = store.windows_for_generations(generations) if generations else store.recent_windows(10_000)
    if not windows:
        print("No archived windows to replay against yet.")
        store.close()
        return 1
    result = replay_strategy(row["source"], windows, config)
    s = result.stats
    print(f"Replay of '{args.name}' over {len(windows)} archived windows "
          f"(generations {generations or 'all'}):")
    print(f"  trades={s.trades}  wins={s.wins}  hit%={s.hit_pct*100:.1f}")
    print(f"  avg_breakeven={s.avg_breakeven:.4f}  net_pnl=${s.net_pnl:+.2f}")
    print(f"  final bankroll=${config.starting_bankroll + s.net_pnl:.2f}")

    state = store.get_state_row(args.name)
    if state:
        recorded = json.loads(state["lifetime_json"] or "{}")
        rec_pnl = recorded.get("net_pnl", 0.0)
        match = abs(rec_pnl - s.net_pnl) < 1e-6
        print(f"  recorded lifetime net_pnl=${rec_pnl:+.2f} "
              f"({'MATCH — deterministic' if match else 'differs'})")
    store.close()
    return 0


def cmd_reset(config: Config, args) -> int:
    if not args.yes:
        print("Refusing to reset without --yes.", file=sys.stderr)
        return 2
    for path in (config.db_path, config.db_path.with_suffix(".sqlite-wal"),
                 config.db_path.with_suffix(".sqlite-shm")):
        if path.exists():
            path.unlink()
    for d in (config.runs_dir, config.strategies_dir):
        if d.exists():
            shutil.rmtree(d)
    print("Reset complete: database, runs/, and strategies/ removed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evolver", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the eternal evolution loop")
    p_run.add_argument("--generations", type=int, default=None,
                       help="stop after N generations (default: run forever)")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("leaderboard", help="lifetime rankings").set_defaults(func=cmd_leaderboard)

    p_show = sub.add_parser("show", help="show a strategy's code, lineage, stats")
    p_show.add_argument("name")
    p_show.set_defaults(func=cmd_show)

    p_replay = sub.add_parser("replay", help="re-score a strategy against archived windows")
    p_replay.add_argument("name")
    p_replay.set_defaults(func=cmd_replay)

    p_reset = sub.add_parser("reset", help="wipe all evolver state")
    p_reset.add_argument("--yes", action="store_true", help="confirm destructive reset")
    p_reset.set_defaults(func=cmd_reset)
    return parser


def main(argv=None) -> int:
    _setup_logging()
    # Load a .env file (if present) before Config reads the environment.
    dotenv_path = find_dotenv()
    loaded = load_dotenv()
    if loaded:
        logging.getLogger("evolver").info(
            "loaded %d var(s) from %s", len(loaded), dotenv_path
        )
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config()
    return args.func(config, args)


if __name__ == "__main__":
    raise SystemExit(main())
