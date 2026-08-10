import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    applySlippage,
    calculateArbitrage,
    executableAsk,
    feePerShareCents,
    findArbitrageOpportunities,
    getBestOpportunity,
} from '../src/arbitrage.js';

const outcome = (platform, yesPrice, noPrice, extra = {}) => ({
    title: 'Will it rain?',
    marketId: `${platform}-1`,
    platform,
    yesId: `${platform}-yes`,
    noId: `${platform}-no`,
    yesPrice,
    noPrice,
    yesSize: 100,
    noSize: 100,
    volume: 0,
    ...extra,
});

test('fee model matches the per-venue curves, in cents', () => {
    assert.ok(Math.abs(feePerShareCents('polymarket', 50) - 1.56) < 1e-9);
    assert.ok(Math.abs(feePerShareCents('kalshi', 50) - 1.75) < 1e-9);
    assert.equal(feePerShareCents('unknown-venue', 50), 0);
});

test('executable ask is floored by the complement bid', () => {
    // A 6c ask while the other side is bid 83c really executes near 17c.
    assert.ok(Math.abs(executableAsk(0.06, 0.83) - 0.17) < 1e-9);
    // A realistic ask above the floor is left alone.
    assert.equal(executableAsk(0.6, 0.5), 0.6);
    // No complement bid: fall back to the slippage curve.
    assert.equal(executableAsk(0.2, null), applySlippage(0.2));
    assert.equal(executableAsk(0, 0.5), null);
});

test('a real spread is reported net of fees', () => {
    const arb = calculateArbitrage({
        polymarket: outcome('polymarket', 40, 60),
        kalshi: outcome('kalshi', 55, 45),
        similarity: 0.9,
    });
    // Best pair is poly YES 40 + kalshi NO 45 = 85c gross.
    assert.equal(arb.type, 'STRATEGY_1');
    assert.equal(arb.grossCost, 85);
    assert.ok(arb.fees > 0);
    assert.equal(arb.totalCost, arb.grossCost + arb.fees);
    assert.equal(arb.profit, 100 - arb.totalCost);
    assert.equal(arb.maxSets, 100);
    assert.equal(arb.legs.length, 2);
});

test('fees kill a spread that is only profitable gross', () => {
    // 99.8c gross looks like +0.2c, but the two taker fees exceed it.
    const arb = calculateArbitrage({
        polymarket: outcome('polymarket', 49.9, 50.1),
        kalshi: outcome('kalshi', 50.1, 49.9),
        similarity: 0.9,
    });
    assert.equal(arb, null);
});

test('the cheaper direction wins when both are profitable', () => {
    const arb = calculateArbitrage({
        polymarket: outcome('polymarket', 60, 20),
        kalshi: outcome('kalshi', 60, 30),
        similarity: 0.9,
    });
    // poly NO 20 + kalshi YES 60 = 80c beats poly YES 60 + kalshi NO 30 = 90c.
    assert.equal(arb.type, 'STRATEGY_2');
    assert.equal(arb.polymarketSide, 'NO');
    assert.equal(arb.kalshiSide, 'YES');
});

test('a side with no book is not tradeable', () => {
    const arb = calculateArbitrage({
        polymarket: outcome('polymarket', null, 20),
        kalshi: outcome('kalshi', null, 30),
        similarity: 0.9,
    });
    assert.equal(arb, null);
});

test('depth comes from the thinner leg', () => {
    const arb = calculateArbitrage({
        polymarket: outcome('polymarket', 40, 60, { yesSize: 12 }),
        kalshi: outcome('kalshi', 55, 45, { noSize: 400 }),
        similarity: 0.9,
    });
    assert.equal(arb.maxSets, 12);
});

test('opportunities are filtered by min profit and sorted best-first', () => {
    const matches = [
        { polymarket: outcome('polymarket', 48, 52), kalshi: outcome('kalshi', 52, 48), similarity: 0.9 },
        { polymarket: outcome('polymarket', 30, 70), kalshi: outcome('kalshi', 75, 25), similarity: 0.9 },
        { polymarket: outcome('polymarket', 40, 60), kalshi: outcome('kalshi', 55, 45), similarity: 0.9 },
    ];
    const opps = findArbitrageOpportunities(matches, 1);
    assert.equal(opps.length, 2); // the 96c-gross pair is under 1c after fees
    assert.ok(opps[0].profit >= opps[1].profit);
    assert.equal(getBestOpportunity(opps), opps[0]);
    assert.equal(getBestOpportunity([]), null);
});
