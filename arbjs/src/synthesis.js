/**
 * Synthesis REST client — the JS counterpart of `polybot/synthesis.py`.
 *
 * One account, one API key, both venues: market data comes from the unified
 * `GET /api/v1/markets?venue=…` listing and the batch orderbook endpoint, and
 * orders go through `POST /api/v1/wallet/{segment}/{wallet_id}/order`.
 *
 * Response shapes are tolerated the same way the Python client tolerates them
 * (envelope peeling + field-name variants), because Synthesis wraps some
 * responses in `{success, response: …}` and some not.
 */

const DEFAULT_BASE_URL = 'https://synthesis.trade';

export class SynthesisError extends Error {}

/** Peel a `{success, response: {...}}` (or data/result) envelope. */
export function unwrap(data) {
    let node = data;
    for (let depth = 0; depth < 5; depth++) {
        if (!node || typeof node !== 'object' || Array.isArray(node)) break;
        const inner = ['response', 'data', 'result']
            .map((key) => node[key])
            .find((value) => value && typeof value === 'object');
        if (inner === undefined) break;
        node = inner;
    }
    return node;
}

/** Coerce a payload into the list of items it carries. */
export function asList(body) {
    if (Array.isArray(body)) return body;
    if (body && typeof body === 'object') {
        for (const key of ['events', 'markets', 'data', 'results', 'orderbooks']) {
            if (Array.isArray(body[key])) return body[key];
        }
        return [body];
    }
    return [];
}

export function num(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
}

function levels(raw, descending) {
    const out = [];
    if (Array.isArray(raw)) {
        for (const level of raw) {
            if (Array.isArray(level) && level.length >= 2) out.push([num(level[0]), num(level[1])]);
            else if (level && typeof level === 'object') out.push([num(level.price), num(level.size)]);
        }
    } else if (raw && typeof raw === 'object') {
        for (const [price, size] of Object.entries(raw)) out.push([num(price), num(size)]);
    }
    return out
        .filter(([price, size]) => price > 0 && size > 0)
        .sort((a, b) => (descending ? b[0] - a[0] : a[0] - b[0]));
}

/** Convert a Synthesis orderbook (price->size maps, or level lists) to sorted levels. */
export function parseOrderbook(entry) {
    const body = unwrap(entry);
    const book = body && typeof body === 'object' && body.orderbook ? body.orderbook : body;
    if (!book || typeof book !== 'object') return { asks: [], bids: [] };
    return { asks: levels(book.asks, false), bids: levels(book.bids, true) };
}

/**
 * Normalize one batch-orderbook entry into `[bookKey, {asks, bids}]` pairs.
 *
 * The two venues answer in different shapes, and are addressed by different ids:
 *
 * - **Polymarket** is requested by `token_id` and returns one flat book per
 *   token: `{orderbook: {token_id, bids, asks}}`.
 * - **Kalshi** is requested by `market_id` — its token ids return nothing at all —
 *   and returns BOTH sides in one entry: `{orderbook: {market_id, yes: {bids,
 *   asks}, no: {bids, asks}}}`. Those become two books keyed `market_id:yes`
 *   and `market_id:no`.
 *
 * The `bookKey` each produces is what callers look books up by, so the rest of
 * the code never has to care which venue a quote came from.
 */
export function normalizeBookEntry(entry) {
    const body = unwrap(entry);
    const book = (body && typeof body === 'object' && body.orderbook) || body;
    if (!book || typeof book !== 'object') return [];

    if (book.yes || book.no) {
        const marketId = String(pick(book, ['market_id', 'marketId'], ''));
        if (!marketId) return [];
        return ['yes', 'no']
            .filter((side) => book[side])
            .map((side) => [`${marketId}:${side}`, {
                asks: levels(book[side].asks, false),
                bids: levels(book[side].bids, true),
            }]);
    }

    const token = String(pick(book, ['token_id', 'tokenId', 'asset_id', 'id'], ''));
    return token ? [[token, { asks: levels(book.asks, false), bids: levels(book.bids, true) }]] : [];
}

function pick(body, keys, fallback = null) {
    for (const key of keys) {
        if (body && body[key] !== undefined && body[key] !== null) return body[key];
    }
    return fallback;
}

/** Map a create-order response into a flat result; the raw body is kept for auditing. */
export function parseOrder(data) {
    const body = unwrap(data);
    const order = body && typeof body === 'object' ? body : {};
    const status = String(pick(order, ['status', 'state'], ''));
    return {
        orderId: String(pick(order, ['order_id', 'id', 'orderID', 'orderId'], '')),
        tokenId: String(pick(order, ['token_id', 'tokenId'], '')),
        side: String(pick(order, ['side'], '')),
        status,
        shares: num(pick(order, ['shares', 'size', 'filled_size', 'matched_size', 'quantity'])),
        price: num(pick(order, ['price', 'avg_price', 'average_price', 'fill_price'])),
        fee: parseFee(pick(order, ['fee', 'fees'])),
        matched: ['MATCHED', 'FILLED', 'COMPLETE', 'COMPLETED'].includes(status.toUpperCase()),
        raw: data,
    };
}

/**
 * Read a market's settlement out of a `GET /api/v1/{venue}/market/{id}` response.
 *
 * The body is `{event, market}`. `winner_token_id` names the side that paid $1;
 * when it's absent on a resolved market, a side priced at ~1 is the fallback.
 */
export function parseResolution(payload) {
    const body = unwrap(payload);
    const market = (body && typeof body === 'object' && body.market) || body || {};
    return {
        resolved: Boolean(market.resolved),
        winnerTokenId: String(market.winner_token_id || ''),
        leftTokenId: String(market.left_token_id || ''),
        rightTokenId: String(market.right_token_id || ''),
        leftPrice: num(market.left_price),
        rightPrice: num(market.right_price),
    };
}

/** Did `tokenId` win? Returns true/false, or null when the market hasn't settled. */
export function tokenWon(resolution, tokenId) {
    if (!resolution || !resolution.resolved) return null;
    const token = String(tokenId);
    if (resolution.winnerTokenId) return resolution.winnerTokenId === token;
    if (resolution.leftTokenId === token) return resolution.leftPrice >= 0.99;
    if (resolution.rightTokenId === token) return resolution.rightPrice >= 0.99;
    return null;
}

export function parseFee(fee) {
    if (fee === null || fee === undefined) return 0;
    if (typeof fee === 'number' || typeof fee === 'string') return num(fee);
    if (typeof fee === 'object') {
        for (const key of ['amount', 'total', 'usdc', 'value', 'fee']) {
            if (fee[key] !== undefined) return num(fee[key]);
        }
        return Object.values(fee).reduce((sum, value) => sum + num(value), 0);
    }
    return 0;
}

export class SynthesisClient {
    constructor({ apiKey = '', walletId = '', baseUrl = DEFAULT_BASE_URL, timeoutMs = 30000 } = {}) {
        this.apiKey = apiKey;
        this.walletId = walletId;
        this.baseUrl = (baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, '');
        this.timeoutMs = timeoutMs;
    }

    headers() {
        const headers = { 'Content-Type': 'application/json' };
        if (this.apiKey) headers['X-API-KEY'] = this.apiKey; // market data is public; only send when set
        return headers;
    }

    async request(method, path, { params = {}, body } = {}) {
        const url = new URL(this.baseUrl + path);
        for (const [key, value] of Object.entries(params)) {
            if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
        }
        let response;
        try {
            response = await fetch(url, {
                method,
                headers: this.headers(),
                body: body === undefined ? undefined : JSON.stringify(body),
                signal: AbortSignal.timeout(this.timeoutMs),
            });
        } catch (error) {
            throw new SynthesisError(`${method} ${url.pathname} failed: ${error.message}`);
        }
        if (!response.ok) {
            const text = (await response.text().catch(() => '')).slice(0, 800);
            throw new SynthesisError(`${method} ${url.pathname} -> ${response.status}: ${text}`);
        }
        return response.json();
    }

    /** Unified market listing across venues. `venue` is 'polymarket' | 'kalshi' | undefined. */
    async listMarkets({ venue, limit = 250, offset = 0, sort = 'volume', order = 'DESC', live } = {}) {
        return this.request('GET', '/api/v1/markets', {
            params: { venue, limit, offset, sort, order, live: live === undefined ? undefined : String(!!live) },
        });
    }

    async fetchOrderbooks(tokenIds) {
        return this.request('POST', '/api/v1/markets/orderbooks', { body: tokenIds.map(String) });
    }

    /**
     * Batch-fetch books, returning Map(bookKey -> {asks, bids}).
     *
     * `ids` are Polymarket token ids and/or Kalshi market ids — see
     * {@link normalizeBookEntry} for the two shapes and the keys they produce.
     */
    async fetchBooks(ids, batchSize = 100) {
        const unique = [...new Set(ids.filter(Boolean).map(String))];
        const books = new Map();
        for (let i = 0; i < unique.length; i += batchSize) {
            const chunk = unique.slice(i, i + batchSize);
            let payload;
            try {
                payload = await this.fetchOrderbooks(chunk);
            } catch (error) {
                console.warn(`   [WARN] orderbook batch of ${chunk.length} failed: ${error.message}`);
                continue;
            }
            for (const entry of asList(unwrap(payload))) {
                if (!entry || typeof entry !== 'object') continue;
                for (const [key, book] of normalizeBookEntry(entry)) books.set(key, book);
            }
        }
        return books;
    }

    /**
     * One market by id, the only place resolution can be read.
     *
     * Resolved markets drop out of `GET /api/v1/markets` entirely, so a settled
     * position can't be found by re-listing — it has to be looked up directly.
     */
    async getMarket(venue, marketId) {
        return this.request('GET', `/api/v1/${venue}/market/${encodeURIComponent(marketId)}`);
    }

    walletPath(segment, suffix = '') {
        return `/api/v1/wallet/${segment}/${this.walletId}${suffix}`;
    }

    /**
     * Place a MARKET order for `shares` contracts of `tokenId`.
     *
     * `priceCap` (0<p<=1) is the MARKET slippage guard from the Synthesis API
     * reference — "never pay above this". It is only meaningful for a BUY, so a
     * SELL is sent without one rather than guessing the bound's direction.
     */
    async placeMarketOrder({ segment, tokenId, side, shares, priceCap }) {
        if (!this.apiKey || !this.walletId) {
            throw new SynthesisError('SYNTHESIS_API_KEY / SYNTHESIS_WALLET_ID not configured');
        }
        if (!segment) throw new SynthesisError('no wallet segment configured for this venue');
        const body = {
            token_id: String(tokenId),
            side: side.toUpperCase(),
            type: 'MARKET',
            amount: String(shares),
            units: 'SHARES',
        };
        if (side.toUpperCase() === 'BUY' && priceCap != null) body.price = String(priceCap);
        return parseOrder(await this.request('POST', this.walletPath(segment, '/order'), { body }));
    }

    /** Preflight one venue's wallet endpoint so a wrong host/key/segment fails fast. */
    async checkReachable(segment) {
        if (!this.apiKey || !this.walletId) return { ok: false, detail: 'SYNTHESIS_API_KEY / SYNTHESIS_WALLET_ID not set' };
        try {
            await this.request('GET', this.walletPath(segment, '/balance'));
            return { ok: true, detail: 'ok' };
        } catch (error) {
            return { ok: false, detail: error.message };
        }
    }
}
