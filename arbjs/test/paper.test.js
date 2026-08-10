import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { PaperBook, markPositions, maybeEnter, positionKey, resolvePositions, sizePosition } from '../src/paper.js';

const BANKROLL = 50000; // $500
const MAX_ARB = 10000; // $100 across both legs

const opportunity = (overrides = {}) => ({
    outcome: 'Will it rain?',
    similarity: 0.9,
    description: 'Buy YES on Polymarket, Buy NO on Kalshi',
    totalCost: 88, // cents per set, fees included
    profit: 12,
    maxSets: 10000,
    endsAt: '2026-12-31T00:00:00',
    legs: [
        { platform: 'polymarket', marketId: 'poly-1', tokenId: 'p-yes', side: 'YES', price: 40 },
        { platform: 'kalshi', marketId: 'kal-1', tokenId: 'k-no', side: 'NO', price: 45 },
    ],
    ...overrides,
});

const book = () => new PaperBook({ startBankrollCents: BANKROLL });

test('one arb never costs more than the per-arb cap', () => {
    const sets = sizePosition(opportunity(), BANKROLL, MAX_ARB);
    assert.equal(sets, Math.floor(10000 / 88)); // 113 sets
    assert.ok(sets * 88 <= MAX_ARB);
});

test('sizing is capped by the remaining bankroll, not just the per-arb cap', () => {
    const sets = sizePosition(opportunity(), 4000, MAX_ARB);
    assert.equal(sets, Math.floor(4000 / 88));
});

test('sizing is capped by the thinner leg book', () => {
    assert.equal(sizePosition(opportunity({ maxSets: 12 }), BANKROLL, MAX_ARB), 12);
});

test('an unprofitable opportunity is never sized', () => {
    assert.equal(sizePosition(opportunity({ profit: -1 }), BANKROLL, MAX_ARB), 0);
});

test('entering deducts cost from the bankroll and records both legs', () => {
    const b = book();
    const position = maybeEnter(b, opportunity(), MAX_ARB);
    assert.equal(position.sets, 113);
    assert.equal(position.costCents, 113 * 88);
    assert.equal(b.bankrollCents, BANKROLL - 113 * 88);
    assert.equal(b.deployedCents, 113 * 88);
    assert.equal(b.equityCents, BANKROLL); // marked at cost until the first re-mark
    assert.equal(b.taken, 1);
    assert.equal(position.legs.length, 2);
});

test('the same standing opportunity is not entered twice', () => {
    const b = book();
    maybeEnter(b, opportunity(), MAX_ARB);
    assert.equal(maybeEnter(b, opportunity(), MAX_ARB), null);
    assert.equal(b.positions.length, 1);
});

test('concurrent arbs stay inside the bankroll and the per-arb cap', () => {
    const b = book();
    for (let i = 0; i < 8; i++) {
        maybeEnter(b, opportunity({
            legs: [
                { platform: 'polymarket', marketId: `poly-${i}`, tokenId: `p-${i}`, side: 'YES', price: 40 },
                { platform: 'kalshi', marketId: `kal-${i}`, tokenId: `k-${i}`, side: 'NO', price: 45 },
            ],
        }), MAX_ARB);
    }
    // Five arbs at the $100 cap, then the leftover cash funds a smaller sixth.
    assert.equal(b.positions.filter((p) => p.costCents > MAX_ARB / 2).length, 5);
    assert.ok(b.positions.every((p) => p.costCents <= MAX_ARB), 'no arb exceeds the per-arb cap');
    assert.ok(b.deployedCents <= BANKROLL, 'never deploys more than the bankroll');
    assert.ok(b.bankrollCents >= 0, 'cash never goes negative');
    assert.equal(b.bankrollCents + b.deployedCents, BANKROLL);
});

test('positions are marked at the bid, not at the price paid', () => {
    const b = book();
    const position = maybeEnter(b, opportunity(), MAX_ARB);
    markPositions(b, new Map([
        ['p-yes', { asks: [[0.42, 100]], bids: [[0.38, 100]] }],
        ['k-no', { asks: [[0.47, 100]], bids: [[0.43, 100]] }],
    ]));
    // Paid 40 + 45 = 85c of asks; the bids total 81c, so the mark is below cost.
    assert.equal(position.markCents, 113 * 38 + 113 * 43);
    assert.equal(position.markStale, false);
    assert.ok(b.unrealizedPnlCents < 0);
});

test('a leg with no bid is held at cost and flagged, not marked to zero', () => {
    const b = book();
    const position = maybeEnter(b, opportunity(), MAX_ARB);
    markPositions(b, new Map([['p-yes', { asks: [], bids: [[0.38, 100]] }]]));
    assert.equal(position.markStale, true);
    assert.equal(position.markCents, 113 * 38 + 113 * 45); // kalshi leg held at its entry price
});

const resolutionClient = (winners) => ({
    async getMarket(venue, marketId) {
        return { success: true, response: { market: { resolved: true, winner_token_id: winners[marketId] } } };
    },
});

test('a set where both venues agree pays exactly one leg', async () => {
    const b = book();
    const position = maybeEnter(b, opportunity(), MAX_ARB);
    // Event happened: YES won on Polymarket, so NO won on Kalshi.
    await resolvePositions(b, resolutionClient({ 'poly-1': 'p-yes', 'kal-1': 'k-yes' }));

    assert.equal(b.positions.length, 0);
    assert.equal(b.resolved, 1);
    assert.equal(b.diverged, 0);
    assert.equal(b.realizedPnlCents, position.sets * 100 - position.costCents);
    assert.equal(b.bankrollCents, BANKROLL + b.realizedPnlCents);
    assert.equal(b.equityCents, BANKROLL + position.sets * 12); // the locked edge
});

test('venues resolving the same event differently is recorded, not assumed away', async () => {
    const b = book();
    const position = maybeEnter(b, opportunity(), MAX_ARB);
    // Polymarket says YES, Kalshi ALSO says YES — so the NO leg is worthless and
    // the "hedge" paid nothing at all.
    await resolvePositions(b, resolutionClient({ 'poly-1': 'p-no', 'kal-1': 'k-yes' }));

    assert.equal(b.diverged, 1);
    assert.equal(b.realizedPnlCents, -position.costCents); // both legs lost
    assert.equal(b.bankrollCents, BANKROLL - position.costCents);
});

test('a position stays open until every leg has settled', async () => {
    const b = book();
    maybeEnter(b, opportunity(), MAX_ARB);
    await resolvePositions(b, {
        async getMarket(venue, marketId) {
            const resolved = marketId === 'poly-1';
            return { response: { market: { resolved, winner_token_id: resolved ? 'p-yes' : '' } } };
        },
    });
    assert.equal(b.positions.length, 1);
    assert.equal(b.resolved, 0);
});

test('the book survives a restart', () => {
    const dir = mkdtempSync(join(tmpdir(), 'arbjs-'));
    const path = join(dir, 'state.json');
    try {
        const b = book();
        maybeEnter(b, opportunity(), MAX_ARB);
        b.save(path);

        const reloaded = PaperBook.load(path, BANKROLL);
        assert.equal(reloaded.positions.length, 1);
        assert.equal(reloaded.bankrollCents, b.bankrollCents);
        assert.equal(reloaded.taken, 1);
        assert.equal(reloaded.positions[0].key, positionKey(opportunity()));
        // And it refuses to re-enter a position it already carries.
        assert.equal(maybeEnter(reloaded, opportunity(), MAX_ARB), null);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});
