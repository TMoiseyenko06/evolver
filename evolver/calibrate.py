"""Paper-vs-real calibration experiment.

Runs a driver strategy on the live market; each time it enters, it BOTH simulates
the fill (paper) and places a real MARKET order via Synthesis, against the same
book at the same instant. After each window resolves, both are scored identically
and the differences are logged. This measures how accurate the paper trade really
is (fill price, fee, and net P&L) using real money at a tiny stake.

Real orders only ever come from a :class:`~evolver.execution.SynthesisExecutor`;
the CLI gates that behind ``--yes`` + credentials + a hard stake cap.
"""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
from typing import Callable, List, Optional

from polybot.synthesis import SynthesisError

from . import colors as c
from .config import Config
from .context import Ctx
from .engine import score_trade
from .execution import Executor, PaperExecutor
from .market import MarketProvider
from .strategy import LoadedStrategy
from .store import Store

log = logging.getLogger("evolver.calibrate")

_NO_RETIRE = 10**9  # keep the single driver alive for the whole experiment

DEFAULT_PROBE_SOURCE = '''\
# lineage: novel
class Strategy:
    NAME = "probe_open_up"
    DESCRIPTION = "Calibration probe: buys Up at window open so a trade fires every window."
    def decide(self, ctx):
        return {"side": "Up"}
'''


def load_driver(
    config: Config,
    store: Store,
    strategy_file: Optional[str] = None,
    strategy_name: Optional[str] = None,
) -> LoadedStrategy:
    """Load the driver strategy from a file, the store, or the default probe."""
    if strategy_file:
        source = Path(strategy_file).read_text(encoding="utf-8")
        return LoadedStrategy.create(source, 0, config)
    if strategy_name:
        row = store.get_strategy_row(strategy_name)
        if row is None:
            raise ValueError(f"no stored strategy named '{strategy_name}'")
        return LoadedStrategy.create(row["source"], row["generation_created"], config)
    return LoadedStrategy.create(DEFAULT_PROBE_SOURCE, 0, config)


def run_calibration(
    market: MarketProvider,
    real_executor: Executor,
    driver: LoadedStrategy,
    store: Store,
    config: Config,
    n_trades: int,
    on_record: Optional[Callable[[dict], None]] = None,
    max_consecutive_failures: int = 3,
) -> List[dict]:
    """Collect ``n_trades`` paired paper/real trades and persist each comparison."""
    paper = PaperExecutor(use_cross_book=config.use_cross_book_fill,
                          slippage_coeff=config.slippage_coeff, slippage_exp=config.slippage_exp,
                          max_slippage=config.max_slippage)
    records: List[dict] = []
    seq = 0
    consecutive_failures = 0

    while len(records) < n_trades:
        handle = market.next_window()
        driver.start_window()
        entry = None  # (side, token_id, paper_fill, real_fill, order)

        for snap in market.poll_snapshots(handle):
            ctx = Ctx.from_snapshot(snap)
            action = driver.decide(ctx, config.decide_timeout_seconds, _NO_RETIRE)
            if not action:
                continue
            side = action["side"]
            token_id = handle.token_map.get(side, "")
            asks = snap.books.get(side, {}).get("asks", [])
            other = "Down" if side == "Up" else "Up"
            comp_bids = snap.books.get(other, {}).get("bids", [])

            paper_fill = paper.fill(side, token_id, asks, config.live_stake, comp_bids)
            if paper_fill is None:
                best = min((p for p, _ in asks if p > 0), default=None)
                if best is not None and config.max_slippage is not None:
                    log.info("%s: %s fill would exceed price guard (best ask %.3f + %.3f); "
                             "skipping — no real order placed",
                             handle.window_id, side, best, config.max_slippage)
                else:
                    log.info("%s: driver entered %s but book empty; no real order placed",
                             handle.window_id, side)
                break
            try:
                real_fill = real_executor.fill(side, token_id, asks, config.live_stake)
            except SynthesisError as exc:
                consecutive_failures += 1
                log.error("real order failed (%d in a row): %s", consecutive_failures, exc)
                if consecutive_failures >= max_consecutive_failures:
                    raise
                break
            if real_fill is None:
                order = getattr(real_executor, "last_order", None)
                status = getattr(order, "status", "") or ""
                raw = json.dumps(getattr(order, "raw", {}))[:600]
                if status and status.upper() not in ("REJECTED", "CANCELED", "CANCELLED", "FAILED"):
                    # Non-empty status but 0 parsed shares => likely a PARSE issue,
                    # and the order may actually have executed. Surface it loudly.
                    log.error(
                        "%s: order returned status=%s but 0 parsed shares — POSSIBLE FILL "
                        "we failed to parse. Check Synthesis order history. raw=%s",
                        handle.window_id, status, raw,
                    )
                else:
                    log.warning("%s: real order did not fill (status=%s). raw=%s",
                                handle.window_id, status or "?", raw)
                break
            consecutive_failures = 0
            order = getattr(real_executor, "last_order", None)
            entry = (side, token_id, paper_fill, real_fill, order)
            log.info(
                "REAL ORDER filled: %s | %s $%.2f -> %.4f shares @ %.3f, fee %.4f, "
                "status=%s id=%s | %s",
                handle.title, side, config.live_stake, real_fill.shares, real_fill.avg_price,
                real_fill.fee, getattr(order, "status", ""), getattr(order, "order_id", ""),
                handle.window_id,
            )
            log.debug("raw order response: %s", json.dumps(getattr(order, "raw", {})))
            break

        if entry is None:
            continue  # strategy passed this window — don't wait for its resolution
        resolution = market.resolve(handle)
        resolved = resolution.official_side or resolution.coinbase_side
        log.info("window %s resolved %s (coinbase=%s official=%s)", handle.window_id,
                 resolved, resolution.coinbase_side, resolution.official_side)
        if resolved is None:
            continue

        side, token_id, paper_fill, real_fill, order = entry
        paper_res = score_trade(driver.name, handle.window_id, paper_fill, resolved)
        real_res = score_trade(driver.name, handle.window_id, real_fill, resolved)
        log.info("calibrated trade #%d: %s->%s | paper pnl %+.3f / real pnl %+.3f | real %s",
                 seq + 1, side, resolved, paper_res.net_pnl, real_res.net_pnl,
                 "WIN" if real_res.won else "LOSS")

        rec = {
            "seq": seq,
            "window_id": handle.window_id,
            "title": handle.title,
            "driver": driver.name,
            "side": side,
            "resolved_side": resolved,
            "paper_price": paper_fill.avg_price,
            "paper_shares": paper_fill.shares,
            "paper_fee": paper_fill.fee,
            "paper_cost": paper_fill.cost,
            "paper_net_pnl": paper_res.net_pnl,
            "paper_won": paper_res.won,
            "real_price": real_fill.avg_price,
            "real_shares": real_fill.shares,
            "real_fee": real_fill.fee,
            "real_cost": real_fill.cost,
            "real_net_pnl": real_res.net_pnl,
            "real_won": real_res.won,
            "order_id": getattr(order, "order_id", None),
            "order_status": getattr(order, "status", None),
            "raw_json": json.dumps(getattr(order, "raw", {})) if order is not None else None,
        }
        store.save_calibration(rec)
        records.append(rec)
        seq += 1
        if on_record:
            on_record(rec)

    return records


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_record_line(rec: dict) -> str:
    market = rec.get("title") or rec["window_id"]
    result = c.green("WIN") if rec.get("real_won") else c.red("LOSS")
    num = c.bold("#" + str(rec["seq"] + 1))
    paper_pnl = c.pnl(rec["paper_net_pnl"], format(rec["paper_net_pnl"], "+.3f"))
    real_pnl = c.pnl(rec["real_net_pnl"], format(rec["real_net_pnl"], "+.3f"))
    return (
        f"{num} {c.bold(market)} | {rec['side']} -> {c.cyan(rec['resolved_side'])} {result} | "
        f"price paper {rec['paper_price']:.3f} / real {rec['real_price']:.3f} "
        f"(Δ{rec['real_price']-rec['paper_price']:+.3f}) | "
        f"fee p {rec['paper_fee']:.4f}/r {rec['real_fee']:.4f} | "
        f"pnl paper {paper_pnl} / real {real_pnl} "
        f"(Δ{rec['real_net_pnl']-rec['paper_net_pnl']:+.3f})"
    )


def summarize(records: List[dict]) -> dict:
    """Aggregate accuracy metrics comparing real fills to the paper sim."""
    n = len(records)
    if n == 0:
        return {"n": 0}
    dprice = [r["real_price"] - r["paper_price"] for r in records]
    dfee = [r["real_fee"] - r["paper_fee"] for r in records]
    dpnl = [r["real_net_pnl"] - r["paper_net_pnl"] for r in records]
    agree = sum(1 for r in records if r["paper_won"] == r["real_won"])
    return {
        "n": n,
        "mean_abs_price_err": statistics.mean(abs(x) for x in dprice),
        "median_abs_price_err": statistics.median(abs(x) for x in dprice),
        "mean_price_bias": statistics.mean(dprice),
        "mean_abs_fee_err": statistics.mean(abs(x) for x in dfee),
        "mean_pnl_bias": statistics.mean(dpnl),          # real - paper; <0 => paper optimistic
        "rmse_pnl": (statistics.mean(x * x for x in dpnl)) ** 0.5,
        "outcome_agreement": agree / n,
        "paper_total_pnl": sum(r["paper_net_pnl"] for r in records),
        "real_total_pnl": sum(r["real_net_pnl"] for r in records),
    }


def write_report(config: Config, records: List[dict]) -> str:
    s = summarize(records)
    lines: List[str] = ["# Calibration report — paper vs. real (Synthesis)\n"]
    if s["n"] == 0:
        lines.append("_No paired trades collected._\n")
    else:
        lines.append(f"Trades: **{s['n']}**  ·  real stake ${config.live_stake:.2f} each\n")
        lines.append("## Accuracy of the paper simulation\n")
        lines.append(f"- Mean |fill-price error|: **{s['mean_abs_price_err']*100:.2f}¢** "
                     f"(median {s['median_abs_price_err']*100:.2f}¢)")
        lines.append(f"- Fill-price bias (real − paper): **{s['mean_price_bias']*100:+.2f}¢**")
        lines.append(f"- Mean |fee error|: **${s['mean_abs_fee_err']:.4f}**")
        lines.append(f"- Net-P&L bias (real − paper): **${s['mean_pnl_bias']:+.4f}/trade** "
                     f"({'paper is OPTIMISTIC' if s['mean_pnl_bias'] < 0 else 'paper is conservative'})")
        lines.append(f"- Net-P&L RMSE: **${s['rmse_pnl']:.4f}**")
        lines.append(f"- Outcome agreement: **{s['outcome_agreement']*100:.0f}%**")
        lines.append(f"- Totals — paper ${s['paper_total_pnl']:+.3f}  ·  real ${s['real_total_pnl']:+.3f}\n")
        lines.append("## Per-trade\n")
        lines.append("| # | window | side | res | paper px | real px | Δpx | paper fee | real fee "
                     "| paper pnl | real pnl | Δpnl | order |")
        lines.append("|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for r in records:
            lines.append(
                f"| {r['seq']+1} | {r['window_id']} | {r['side']} | {r['resolved_side']} "
                f"| {r['paper_price']:.3f} | {r['real_price']:.3f} | {r['real_price']-r['paper_price']:+.3f} "
                f"| {r['paper_fee']:.4f} | {r['real_fee']:.4f} "
                f"| {r['paper_net_pnl']:+.3f} | {r['real_net_pnl']:+.3f} | {r['real_net_pnl']-r['paper_net_pnl']:+.3f} "
                f"| {r.get('order_id') or '-'} |"
            )
    report = "\n".join(lines) + "\n"
    path = config.runs_dir / "calibration_report.md"
    path.write_text(report, encoding="utf-8")
    return str(path)
