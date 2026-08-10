/**
 * Paper-trade the cross-venue arbs the live bot would take.
 *
 * Same scan, same executable prices, same fees — only the fills are simulated.
 * The point is to measure what this strategy actually earns before any capital
 * is risked, so the accounting is deliberately unflattering:
 *
 * - **Settlement is looked up per leg, per venue.** A cross-venue "lock" only
 *   pays its guaranteed $1 if BOTH venues resolve the event the same way. Rather
 *   than assume that (which would turn every entry into a printed profit), each
 *   leg is settled against its own venue's `winner_token_id`. If the venues
 *   disagree the position pays $0 or $2 per set, and the book records it. That
 *   divergence *is* the basis risk the README warns about, measured instead of
 *   assumed.
 * - **Open positions are marked at the bid** — what a liquidation would fetch —
 *   not at the ask that was paid.
 * - **Sizing is bounded by real book depth**, so a 12-contract book can't fund a
 *   $100 position.
 *
 * All amounts are integer-ish cents internally; only the board prints dollars.
 */

import { existsSync, readFileSync, writeFileSync } from 'node:fs';

import { parseResolution, tokenWon } from './synthesis.js';

const dollars = (cents) => `$${(cents / 100).toFixed(2)}`;

/** Stable identity for a standing opportunity, so it isn't entered twice. */
export function positionKey(opportunity) {
    return opportunity.legs.map((leg) => `${leg.platform}:${leg.tokenId}`).sort().join('|');
}

/**
 * How many sets to buy: the tightest of the per-arb cap, the remaining
 * bankroll, and the depth of the thinner leg.
 */
export function sizePosition(opportunity, bankrollCents, maxArbCostCents) {
    if (!(opportunity.profit > 0) || !(opportunity.totalCost > 0)) return 0;
    const affordableCents = Math.min(maxArbCostCents, bankrollCents);
    const sets = Math.min(affordableCents / opportunity.totalCost, opportunity.maxSets);
    return Math.max(0, Math.floor(sets));
}

export class PaperBook {
    constructor({
        startBankrollCents,
        bankrollCents = startBankrollCents,
        realizedPnlCents = 0,
        positions = [],
        taken = 0,
        resolved = 0,
        diverged = 0,
        startedAt = Date.now(),
    }) {
        this.startBankrollCents = startBankrollCents;
        this.bankrollCents = bankrollCents;
        this.realizedPnlCents = realizedPnlCents;
        this.positions = positions;
        this.taken = taken;
        this.resolved = resolved;
        this.diverged = diverged;
        this.startedAt = startedAt;
    }

    static load(path, startBankrollCents) {
        if (path && existsSync(path)) {
            const saved = JSON.parse(readFileSync(path, 'utf8'));
            return new PaperBook({ ...saved, startBankrollCents: saved.startBankrollCents ?? startBankrollCents });
        }
        return new PaperBook({ startBankrollCents });
    }

    save(path) {
        if (path) writeFileSync(path, JSON.stringify(this, null, 2));
    }

    get deployedCents() {
        return this.positions.reduce((sum, p) => sum + p.costCents, 0);
    }

    /** Liquidation value of the open book, marked at the bid. */
    get markValueCents() {
        return this.positions.reduce((sum, p) => sum + (p.markCents ?? p.costCents), 0);
    }

    get unrealizedPnlCents() {
        return this.markValueCents - this.deployedCents;
    }

    /** Cash + what the open positions would fetch right now. */
    get equityCents() {
        return this.bankrollCents + this.markValueCents;
    }

    get totalPnlCents() {
        return this.equityCents - this.startBankrollCents;
    }

    /** The ids to re-request books for, to mark every open leg. */
    positionBookIds() {
        return [...new Set(this.positions.flatMap((p) => p.legs.map((leg) => leg.bookRequestId)))];
    }

    has(key) {
        return this.positions.some((p) => p.key === key);
    }
}

/** Buy `opportunity` on paper if it's new and the bankroll can carry it. */
export function maybeEnter(book, opportunity, maxArbCostCents) {
    const key = positionKey(opportunity);
    if (book.has(key)) return null;

    const sets = sizePosition(opportunity, book.bankrollCents, maxArbCostCents);
    if (sets < 1) return null;

    const costCents = sets * opportunity.totalCost;
    if (costCents > book.bankrollCents) return null;

    const position = {
        key,
        title: opportunity.outcome,
        similarity: opportunity.similarity,
        description: opportunity.description,
        sets,
        costCents,
        costPerSetCents: opportunity.totalCost,
        edgeCents: opportunity.profit,
        endsAt: opportunity.endsAt ?? null,
        openedAt: Date.now(),
        markCents: costCents,
        markStale: false,
        legs: opportunity.legs.map((leg) => ({
            platform: leg.platform,
            marketId: leg.marketId,
            tokenId: leg.tokenId,
            bookKey: leg.bookKey ?? leg.tokenId,
            bookRequestId: leg.bookRequestId ?? leg.tokenId,
            side: leg.side,
            entryPriceCents: leg.price,
        })),
    };

    book.bankrollCents -= costCents;
    book.positions.push(position);
    book.taken += 1;
    console.log(`[ENTER] ${position.title.slice(0, 60)}`);
    console.log(`        ${sets} sets @ ${opportunity.totalCost.toFixed(2)}¢ = ${dollars(costCents)}`
        + ` -> locks ${dollars(sets * opportunity.profit)} if both venues agree`);
    return position;
}

/** Re-mark every open position at the current best bid on each leg. */
export function markPositions(book, books) {
    for (const position of book.positions) {
        let value = 0;
        let stale = false;
        for (const leg of position.legs) {
            const bids = books.get(leg.bookKey)?.bids ?? [];
            const bidCents = bids.length ? bids[0][0] * 100 : null;
            if (bidCents == null) {
                stale = true; // no bid to mark against: hold this leg at cost rather than invent a price
                value += position.sets * leg.entryPriceCents;
            } else {
                value += position.sets * bidCents;
            }
        }
        position.markCents = value;
        position.markStale = stale;
    }
}

/**
 * Settle any position whose legs have both resolved, paying each leg on its own
 * venue's verdict.
 */
export async function resolvePositions(book, client) {
    for (const position of [...book.positions]) {
        let verdicts;
        try {
            verdicts = await Promise.all(position.legs.map(async (leg) => {
                const payload = await client.getMarket(leg.platform, leg.marketId);
                return tokenWon(parseResolution(payload), leg.tokenId);
            }));
        } catch (error) {
            console.warn(`   [WARN] resolution check failed for ${position.title.slice(0, 40)}: ${error.message}`);
            continue;
        }
        if (verdicts.some((won) => won === null)) continue; // not settled on every venue yet

        const wins = verdicts.filter(Boolean).length;
        const payoutCents = position.sets * 100 * wins;
        const pnlCents = payoutCents - position.costCents;

        book.bankrollCents += payoutCents;
        book.realizedPnlCents += pnlCents;
        book.resolved += 1;
        book.positions = book.positions.filter((p) => p.key !== position.key);

        console.log(`[RESOLVE] ${position.title.slice(0, 60)}`);
        console.log(`          ${wins}/${position.legs.length} legs paid · payout ${dollars(payoutCents)}`
            + ` - cost ${dollars(position.costCents)} = ${pnlCents >= 0 ? '+' : ''}${dollars(pnlCents)}`);
        if (wins !== 1) {
            // Exactly one leg should pay. Anything else means the two venues did
            // not resolve the same event the same way — the basis risk, realized.
            book.diverged += 1;
            console.log(`          [BASIS RISK] the venues disagreed — a hedged set paid ${wins} legs, not 1`);
        }
    }
}

export function printBoard(book) {
    const held = book.positions.length;
    const stale = book.positions.filter((p) => p.markStale).length;
    console.log(
        `\n=== arbjs paper · bankroll ${dollars(book.bankrollCents)} · deployed ${dollars(book.deployedCents)}`
        + ` (${held} open) · realized ${dollars(book.realizedPnlCents)} · unrealized ${dollars(book.unrealizedPnlCents)}`
        + ` · equity ${dollars(book.equityCents)} (${dollars(book.totalPnlCents)} vs start)`
        + ` · taken ${book.taken} / resolved ${book.resolved}`
        + (book.diverged ? ` / DIVERGED ${book.diverged}` : '') + ' ===',
    );
    for (const p of [...book.positions].sort((a, b) => b.costCents - a.costCents).slice(0, 12)) {
        const pnl = (p.markCents ?? p.costCents) - p.costCents;
        console.log(`  ${p.title.slice(0, 56).padEnd(56)} ${String(p.sets).padStart(5)} sets`
            + ` cost ${dollars(p.costCents).padStart(8)} mark ${dollars(p.markCents ?? p.costCents).padStart(8)}`
            + ` (${pnl >= 0 ? '+' : ''}${dollars(pnl)})${p.markStale ? ' *stale' : ''}`);
    }
    if (stale) console.log(`  * ${stale} position(s) have a leg with no bid to mark against; held at cost.`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Scan -> resolve -> mark -> enter, forever. Returns only when `book.stop` is set. */
export async function runPaper(bot, book, { maxArbCostCents, intervalSeconds, statePath }) {
    book.stop = false;
    while (!book.stop) {
        try {
            const { top } = await bot.scanOpportunities();
            await resolvePositions(book, bot.synthesis);
            markPositions(book, await bot.synthesis.fetchBooks(book.positionBookIds()));
            for (const opportunity of top) maybeEnter(book, opportunity, maxArbCostCents);
            printBoard(book);
            book.save(statePath);
        } catch (error) {
            console.error('[ERROR]', error.message);
        }
        if (book.stop) break;
        await sleep(intervalSeconds * 1000);
    }
    return book;
}
