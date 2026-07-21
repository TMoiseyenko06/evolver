"""Human-readable leaderboards: per-generation markdown reports and CLI tables."""

from __future__ import annotations

from pathlib import Path
from typing import List

from .config import Config
from .strategy import LoadedStrategy


def _fmt_lineage(lineage) -> str:
    return ", ".join(lineage) if lineage else "novel"


def write_generation_report(
    config: Config,
    generation: int,
    population: List[LoadedStrategy],
    survivors: List[LoadedStrategy],
) -> str:
    """Write ``runs/gen{G}_report.md`` and return its path."""
    survivor_names = {s.name for s in survivors}
    ranked = sorted(population, key=lambda s: (s.gen.net_pnl, s.gen.tiebreak), reverse=True)

    lines: List[str] = []
    lines.append(f"# Generation {generation} report\n")
    lines.append(f"Population: {len(population)} · Survivors: {len(survivors)}\n")
    lines.append("## This generation (ranked by net P&L)\n")
    lines.append(
        "| Rank | Strategy | Trades | Hit% | Avg BE | Net P&L | Bankroll | Result | Lineage |"
    )
    lines.append("|---:|---|---:|---:|---:|---:|---:|---|---|")
    for i, s in enumerate(ranked, 1):
        result = "SURVIVE" if s.name in survivor_names else (
            "auto-retired" if s.retired and "auto-retired" in (s.retired_reason or "") else "retired"
        )
        g = s.gen
        lines.append(
            f"| {i} | {s.name} | {g.trades} | {g.hit_pct*100:.1f} | {g.avg_breakeven:.4f} "
            f"| ${g.net_pnl:+.2f} | ${s.bankroll:.2f} | {result} | {_fmt_lineage(s.lineage)} |"
        )

    lines.append("\n## Lifetime (cumulative across generations)\n")
    lines.append(
        "| Strategy | Gens Survived | Life Trades | Life Hit% | Life Avg BE | Life Net P&L | Bankroll |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for s in sorted(population, key=lambda s: s.lifetime.net_pnl, reverse=True):
        lt = s.lifetime
        lines.append(
            f"| {s.name} | {s.generations_survived} | {lt.trades} | {lt.hit_pct*100:.1f} "
            f"| {lt.avg_breakeven:.4f} | ${lt.net_pnl:+.2f} | ${s.bankroll:.2f} |"
        )

    retired = [s for s in population if s.name not in survivor_names]
    if retired:
        lines.append("\n## Retirements\n")
        for s in retired:
            lines.append(f"- **{s.name}** — {s.retired_reason or 'culled'}")

    lines.append("")
    report = "\n".join(lines)
    path = config.runs_dir / f"gen{generation}_report.md"
    path.write_text(report, encoding="utf-8")
    return str(path)


def format_leaderboard(rows: List[dict]) -> str:
    """Format lifetime leaderboard rows (from Store.leaderboard_rows) as text."""
    if not rows:
        return "No strategies yet. Run `python -m evolver run` first."
    header = (
        f"{'#':>3}  {'STRATEGY':<24} {'ALIVE':<5} {'GENS':>4} {'TRADES':>6} "
        f"{'HIT%':>6} {'AVG_BE':>7} {'NET_PNL':>10} {'BANKROLL':>10}  LINEAGE"
    )
    out = [header, "-" * len(header)]
    for i, r in enumerate(rows, 1):
        s = r["stats"]
        out.append(
            f"{i:>3}  {r['name']:<24} {'yes' if r['alive'] else 'no':<5} "
            f"{r['generations_survived']:>4} {s.trades:>6} {s.hit_pct*100:>6.1f} "
            f"{s.avg_breakeven:>7.4f} {s.net_pnl:>+10.2f} {r['bankroll']:>10.2f}  "
            f"{_fmt_lineage(r['lineage'])}"
        )
    return "\n".join(out)
