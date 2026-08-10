import assert from 'node:assert/strict';
import { test } from 'node:test';

import { applyBooks, parseOutcomes } from '../src/bot.js';
import { SynthesisClient, parseOrderbook, unwrap } from '../src/synthesis.js';

const market = (overrides = {}) => ({
    condition_id: '0xabc',
    title: 'Will it rain tomorrow?',
    left_outcome: 'Yes',
    left_token_id: 'tok-yes',
    left_price: 0.62,
    right_outcome: 'No',
    right_token_id: 'tok-no',
    right_price: 0.38,
    volume: 1234,
    ...overrides,
});

test('markets are parsed out of the events envelope', () => {
    const payload = { success: true, response: [{ event_id: 'ev1', venue: 'kalshi', markets: [market()] }] };
    const [outcome] = parseOutcomes(payload, 'polymarket');
    assert.equal(outcome.platform, 'kalshi'); // the payload's venue beats the hint
    assert.equal(outcome.eventId, 'ev1');
    assert.equal(outcome.marketId, '0xabc');
    assert.equal(outcome.yesId, 'tok-yes');
    assert.equal(outcome.noId, 'tok-no');
    assert.equal(outcome.yesMid, 62);
    assert.equal(outcome.noMid, 38);
    // Listing mids are indicative only — no tradeable price until books arrive.
    assert.equal(outcome.yesPrice, null);
    assert.equal(outcome.noPrice, null);
});

test('a reversed left/right pair is oriented, not trusted by position', () => {
    const payload = [market({
        left_outcome: 'Down', left_token_id: 'tok-down', left_price: 0.45,
        right_outcome: 'Up', right_token_id: 'tok-up', right_price: 0.55,
    })];
    const [outcome] = parseOutcomes(payload, 'polymarket');
    assert.equal(outcome.yesId, 'tok-up');
    assert.equal(outcome.noId, 'tok-down');
    assert.equal(outcome.yesMid, 55);
});

test('labels that are not a complementary pair are skipped', () => {
    const payload = [market({ left_outcome: 'Trump', right_outcome: 'Harris' })];
    assert.deepEqual(parseOutcomes(payload, 'polymarket'), []);
});

test('books fill executable asks, clamped by the complement bid', () => {
    const [outcome] = parseOutcomes([market()], 'polymarket');
    const books = new Map([
        ['tok-yes', parseOrderbook({ orderbook: { asks: { '0.86': '150' }, bids: { '0.83': '80' } } })],
        // A phantom 6c No ask while Yes is bid 83c: really executable near 17c.
        ['tok-no', parseOrderbook({ orderbook: { asks: { '0.06': '40' }, bids: { '0.15': '10' } } })],
    ]);

    applyBooks([outcome], books);
    assert.equal(outcome.yesPrice, 86); // above its own floor of 1 - 0.15, left alone
    assert.equal(outcome.noPrice, 17); // clamped up from the phantom 6c
    assert.equal(outcome.yesSize, 150);
    assert.equal(outcome.noSize, 40);
    assert.equal(outcome.yesBid, 83);
});

test('a missing book leaves the side untradeable rather than falling back to the mid', () => {
    const [outcome] = parseOutcomes([market()], 'polymarket');
    applyBooks([outcome], new Map());
    assert.equal(outcome.yesPrice, null);
    assert.equal(outcome.noPrice, null);
    assert.equal(outcome.yesSize, 0);
});

test('orderbook levels are sorted best-first and zero levels dropped', () => {
    const book = parseOrderbook({
        orderbook: {
            asks: { '0.70': '5', '0.55': '10', '0.90': '0' },
            bids: { '0.40': '7', '0.52': '3' },
        },
    });
    assert.deepEqual(book.asks, [[0.55, 10], [0.7, 5]]);
    assert.deepEqual(book.bids, [[0.52, 3], [0.4, 7]]);
});

test('batch books are keyed by the token id nested inside orderbook', async (t) => {
    // The live endpoint answers {venue, orderbook: {token_id, bids, asks}} — the id
    // sits inside `orderbook`, so keying off the entry's own fields finds nothing.
    t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({
        success: true,
        response: [{
            venue: 'polymarket',
            orderbook: { condition_id: '0xabc', token_id: 'tok-yes', bids: { '0.60': '80' }, asks: { '0.62': '150' } },
        }],
    })));

    const books = await new SynthesisClient().fetchBooks(['tok-yes']);
    assert.deepEqual([...books.keys()], ['tok-yes']);
    assert.deepEqual(books.get('tok-yes').asks, [[0.62, 150]]);
});

test('envelopes are peeled the way the python client peels them', () => {
    assert.deepEqual(unwrap({ success: true, response: { data: { a: 1 } } }), { a: 1 });
    assert.deepEqual(unwrap([{ a: 1 }]), [{ a: 1 }]);
});
