"""Paper-trade intra-market arbitrage with REALISTIC fills.

Scans on an interval; when an intra-market arb clears the fee/edge bar on
*executable* prices (cross-book no-arb model, so phantom cheap asks don't count),
it paper-buys both sides at a size bounded by real book depth, then realizes the
locked P&L when the market matures.

Intra-market arb is a guaranteed lock: buying ``n`` shares of BOTH outcomes of one
binary market costs ``net_cost·n`` and pays exactly ``n`` at resolution (one side
wins), so P&L = ``n·(1 − net_cost) = n·edge`` — no resolution lookup needed. That
makes this an honest measurement of how much locked arb is actually capturable on
efficient markets after realistic fills, fees, and depth. (Cross-venue arb is NOT
paper-traded here — its P&L depends on both venues resolving identically, which we
can't simulate yet; the scanner still reports those as candidates.)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from polybot.synthesis import SynthesisClient

from . import scan
from .detect import _to_epoch
from .model import ArbLeg, ArbOpportunity

log = logging.getLogger("arb.paper")


@dataclass
class Position:
    market_id: str
    venue: str
    title: str
    shares: float
    cost: float                 # total paid = net_cost * shares
    edge: float                 # per-share locked edge
    ends_at: Optional[float]    # epoch seconds, or None
    opened_at: float
    legs: List[ArbLeg] = field(default_factory=list)

    @property
    def locked_pnl(self) -> float:
        return self.shares * self.edge


@dataclass
class PaperBook:
    start_bankroll: float
    bankroll: float
    realized_pnl: float = 0.0
    n_taken: int = 0
    n_resolved: int = 0
    peak_deployed: float = 0.0
    open: Dict[str, Position] = field(default_factory=dict)

    @property
    def deployed(self) -> float:
        return sum(p.cost for p in self.open.values())

    @property
    def open_locked(self) -> float:
        return sum(p.locked_pnl for p in self.open.values())


def run_paper(
    client: SynthesisClient,
    venues: List[str],
    bankroll: float = 500.0,
    interval: float = 30.0,
    min_edge: float = 0.005,
    per_arb_cap: float = 200.0,     # max shares per single arb
    max_markets: int = 1000,
    settle_delay: float = 120.0,    # wait this long past ends_at before realizing
    max_hold: float = 3600.0,       # realize positions with unknown ends_at after this
    on_cycle=None,
) -> PaperBook:
    """Run the scan→enter→resolve loop forever (Ctrl-C to stop). Returns the book."""
    book = PaperBook(start_bankroll=bankroll, bankroll=bankroll)
    while True:
        _resolve_matured(book, settle_delay, max_hold)
        for venue in venues:
            try:
                opps = scan.scan_intra(client, venue, max_markets, min_edge, realistic=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("scan %s failed: %s", venue, exc)
                continue
            for opp in opps:
                _maybe_enter(book, opp, per_arb_cap)
        _print_board(book)
        if on_cycle is not None:
            on_cycle(book)
        time.sleep(interval)


def _maybe_enter(book: PaperBook, opp: ArbOpportunity, per_arb_cap: float) -> None:
    key = opp.legs[0].market_id
    if key in book.open or opp.edge <= 0 or opp.max_size <= 0 or opp.net_cost <= 0:
        return
    shares = min(opp.max_size, per_arb_cap, book.bankroll / opp.net_cost)
    if shares < 1:
        return
    cost = opp.net_cost * shares
    if cost > book.bankroll:
        return
    book.bankroll -= cost
    book.open[key] = Position(
        market_id=key, venue=opp.legs[0].venue, title=opp.title, shares=shares, cost=cost,
        edge=opp.edge, ends_at=_to_epoch(opp.ends_at), opened_at=time.time(), legs=list(opp.legs),
    )
    book.n_taken += 1
    book.peak_deployed = max(book.peak_deployed, book.deployed)
    legs = " + ".join(f"{l.outcome}@{l.ask:.3f}[{l.venue}]" for l in opp.legs)
    log.info("ENTER %s | %.0f sets @ net %.3f (edge %+.2f%%) cost $%.2f -> locks $%.2f | %s",
             opp.title[:60], shares, opp.net_cost, opp.edge * 100, cost, shares * opp.edge, legs)


def _resolve_matured(book: PaperBook, settle_delay: float, max_hold: float) -> None:
    now = time.time()
    for key, pos in list(book.open.items()):
        matured = (pos.ends_at is not None and now >= pos.ends_at + settle_delay) or \
                  (pos.ends_at is None and now >= pos.opened_at + max_hold)
        if not matured:
            continue
        payout = pos.shares            # intra lock: exactly one side pays $1/share
        pnl = payout - pos.cost
        book.bankroll += payout
        book.realized_pnl += pnl
        book.n_resolved += 1
        del book.open[key]
        log.info("RESOLVE %s | payout $%.2f - cost $%.2f = $%+.2f | realized $%+.2f",
                 pos.title[:60], payout, pos.cost, pnl, book.realized_pnl)


def _print_board(book: PaperBook) -> None:
    equity = book.bankroll + book.deployed + book.open_locked
    print(
        f"\n=== arb paper · bankroll ${book.bankroll:.2f} · deployed ${book.deployed:.2f} "
        f"({len(book.open)} open) · realized ${book.realized_pnl:+.2f} · "
        f"open-locked ${book.open_locked:+.2f} · equity ${equity:.2f} · "
        f"taken {book.n_taken} / resolved {book.n_resolved} ===",
        flush=True,
    )
    for pos in sorted(book.open.values(), key=lambda p: p.locked_pnl, reverse=True)[:12]:
        print(f"  {pos.title[:64]:64} {pos.shares:6.0f} sets  locks ${pos.locked_pnl:+.2f} "
              f"(edge {pos.edge*100:+.2f}%)")
