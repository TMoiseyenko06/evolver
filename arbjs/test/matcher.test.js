import assert from 'node:assert/strict';
import { test } from 'node:test';

import { combinedSimilarity, matchOutcomes } from '../src/matcher.js';

const titled = (title) => ({ title });

test('a reworded title still matches above threshold', () => {
    const poly = [titled('Will Bitcoin close above 100000 on Dec 31')];
    const kalshi = [titled('Will Bitcoin close above 100000 on Dec 31?')];
    const [match] = matchOutcomes(poly, kalshi, 0.7);
    assert.equal(match.kalshi, kalshi[0]);
    assert.ok(match.similarity >= 0.7);
});

test('unrelated titles do not match', () => {
    const poly = [titled('Will Bitcoin close above 100000 on Dec 31')];
    const kalshi = [titled('Will the Lakers win the 2026 NBA championship')];
    assert.deepEqual(matchOutcomes(poly, kalshi, 0.7), []);
});

test('each kalshi outcome is used at most once', () => {
    const poly = [titled('Will Bitcoin close above 100000'), titled('Will Bitcoin close above 100000')];
    const kalshi = [titled('Will Bitcoin close above 100000')];
    const matches = matchOutcomes(poly, kalshi, 0.7);
    assert.equal(matches.length, 1);
});

test('blocking never drops a pair the full scan would have matched', () => {
    const titles = [
        'Will Bitcoin close above 100000 on Dec 31',
        'Will Ethereum close above 5000 on Dec 31',
        'Will the Fed cut rates in March 2026',
        'Fed cuts rates March 2026',
        'Will Bitcoin close above 90000 on Dec 31',
    ];
    const poly = titles.map(titled);
    const kalshi = titles.map((t) => titled(t.replace('Will ', '').replace('?', '')));
    const threshold = 0.6;

    // Brute-force the same greedy pairing without the inverted index.
    const used = new Set();
    const expected = [];
    for (const p of poly) {
        let best = null;
        let bestScore = 0;
        let bestIndex = -1;
        kalshi.forEach((k, i) => {
            if (used.has(i)) return;
            const score = combinedSimilarity(p.title, k.title);
            if (score > bestScore && score >= threshold) {
                bestScore = score;
                best = k;
                bestIndex = i;
            }
        });
        if (best) {
            expected.push([p.title, best.title, bestScore]);
            used.add(bestIndex);
        }
    }

    const actual = matchOutcomes(poly, kalshi, threshold)
        .map((m) => [m.polymarket.title, m.kalshi.title, m.similarity]);
    assert.deepEqual(actual, expected);
});
