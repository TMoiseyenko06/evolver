import assert from 'node:assert/strict';
import { test } from 'node:test';

import { applyBooks, parseOutcomes } from '../src/bot.js';
import { SynthesisClient, normalizeBookEntry, parseOrderbook, unwrap } from '../src/synthesis.js';

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

test('kalshi books are keyed by market id and split into yes/no sides', () => {
    // Kalshi answers one entry per MARKET carrying both sides; its token ids
    // return nothing from the endpoint at all.
    const [yes, no] = normalizeBookEntry({
        venue: 'kalshi',
        orderbook: {
            market_id: 'KXPRESNOMD-28-GN',
            yes: { bids: { '0.16': '68331' }, asks: { '0.17': '1836' } },
            no: { bids: { '0.83': '1836' }, asks: { '0.84': '68331' } },
        },
    });
    assert.equal(yes[0], 'KXPRESNOMD-28-GN:yes');
    assert.deepEqual(yes[1].asks, [[0.17, 1836]]);
    assert.equal(no[0], 'KXPRESNOMD-28-GN:no');
    assert.deepEqual(no[1].bids, [[0.83, 1836]]);
});

test('a kalshi market requests its book by market id, polymarket by token', () => {
    const [kalshi] = parseOutcomes([{ ...market(), market_id: 'KX-1', condition_id: undefined }], 'kalshi');
    assert.deepEqual(kalshi.bookRequestIds, ['KX-1']);
    assert.equal(kalshi.yesBookKey, 'KX-1:yes');
    assert.equal(kalshi.noBookKey, 'KX-1:no');

    const [poly] = parseOutcomes([market()], 'polymarket');
    assert.deepEqual(poly.bookRequestIds, ['tok-yes', 'tok-no']);
    assert.equal(poly.yesBookKey, 'tok-yes');
});

test('a kalshi outcome prices off its split book', () => {
    const [outcome] = parseOutcomes([{ ...market(), market_id: 'KX-1', condition_id: undefined }], 'kalshi');
    const books = new Map(normalizeBookEntry({
        orderbook: {
            market_id: 'KX-1',
            yes: { bids: { '0.60': '80' }, asks: { '0.62': '150' } },
            no: { bids: { '0.38': '150' }, asks: { '0.40': '80' } },
        },
    }));
    applyBooks([outcome], books);
    assert.equal(outcome.yesPrice, 62);
    assert.equal(outcome.noPrice, 40);
    assert.equal(outcome.yesSize, 150);
    assert.equal(outcome.yesBid, 60);
});

test('envelopes are peeled the way the python client peels them', () => {
    assert.deepEqual(unwrap({ success: true, response: { data: { a: 1 } } }), { a: 1 });
    assert.deepEqual(unwrap([{ a: 1 }]), [{ a: 1 }]);
});
