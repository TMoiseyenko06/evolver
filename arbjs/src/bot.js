import { SynthesisClient, asList, num, unwrap } from './synthesis.js';
import { matchOutcomes } from './matcher.js';
import { executableAsk, findArbitrageOpportunities, getBestOpportunity } from './arbitrage.js';

// Synthesis normalizes both venues to left/right outcomes, but the labels differ
// per market ("Yes"/"No", "Up"/"Down"). Mapping them wrong inverts a leg and
// turns a hedge into a doubled directional bet, so unknown labels are skipped
// rather than guessed at.
const YES_LABELS = new Set(['yes', 'up', 'over', 'above']);
const NO_LABELS = new Set(['no', 'down', 'under', 'below']);

const toCents = (price) => Number((price * 100).toFixed(2));

function orientSides(left, right) {
    const l = String(left.label).trim().toLowerCase();
    const r = String(right.label).trim().toLowerCase();
    if (YES_LABELS.has(l) && NO_LABELS.has(r)) return { yes: left, no: right };
    if (NO_LABELS.has(l) && YES_LABELS.has(r)) return { yes: right, no: left };
    return null;
}

function parseMarket(market, venue, eventId) {
    if (!market || typeof market !== 'object') return null;
    const marketId = market.condition_id ?? market.conditionId ?? market.market_id ?? market.kalshi_id ?? market.id;
    if (marketId == null) return null;

    const left = { label: market.left_outcome, token: market.left_token_id, mid: num(market.left_price) };
    const right = { label: market.right_outcome, token: market.right_token_id, mid: num(market.right_price) };
    if (!left.label || !left.token || !right.label || !right.token) return null;

    const sides = orientSides(left, right);
    if (!sides) return null;

    return {
        title: String(market.title || market.question || ''),
        marketId: String(marketId),
        eventId: String(eventId || market.event_id || ''),
        platform: venue,
        endsAt: market.ends_at ?? null,
        resolved: Boolean(market.resolved),
        yesLabel: String(sides.yes.label),
        noLabel: String(sides.no.label),
        yesId: String(sides.yes.token),
        noId: String(sides.no.token),
        yesMid: toCents(sides.yes.mid),
        noMid: toCents(sides.no.mid),
        // Executable asks and bids, in cents — filled in by applyBooks once the
        // order books are fetched. Null until then: a listing mid is indicative,
        // not a price anyone will sell you.
        yesPrice: null,
        noPrice: null,
        yesBid: null,
        noBid: null,
        yesSize: 0,
        noSize: 0,
        volume: num(market.volume),
    };
}

/** Turn a unified `GET /api/v1/markets` response into per-market YES/NO outcomes. */
export function parseOutcomes(payload, venue) {
    const outcomes = [];
    for (const event of asList(unwrap(payload))) {
        if (!event || typeof event !== 'object') continue;
        const eventVenue = event.venue || venue;
        const eventId = String(event.event_id ?? event.id ?? '');
        const markets = Array.isArray(event.markets) ? event.markets : [event];
        for (const market of markets) {
            const parsed = parseMarket(market, eventVenue, eventId);
            if (parsed) outcomes.push(parsed);
        }
    }
    return outcomes;
}

/** Fill each outcome's executable ask / best bid / depth from fetched books (in place). */
export function applyBooks(outcomes, books) {
    for (const outcome of outcomes) {
        const yes = books.get(outcome.yesId) || { asks: [], bids: [] };
        const no = books.get(outcome.noId) || { asks: [], bids: [] };
        const yesAsk = yes.asks[0]?.[0] ?? null;
        const noAsk = no.asks[0]?.[0] ?? null;
        const yesBid = yes.bids[0]?.[0] ?? null;
        const noBid = no.bids[0]?.[0] ?? null;

        outcome.yesSize = yes.asks[0]?.[1] ?? 0;
        outcome.noSize = no.asks[0]?.[1] ?? 0;
        outcome.yesBid = yesBid == null ? null : toCents(yesBid);
        outcome.noBid = noBid == null ? null : toCents(noBid);

        const yesExec = yesAsk == null ? null : executableAsk(yesAsk, noBid);
        const noExec = noAsk == null ? null : executableAsk(noAsk, yesBid);
        outcome.yesPrice = yesExec == null ? null : toCents(yesExec);
        outcome.noPrice = noExec == null ? null : toCents(noExec);
    }
}

export class ArbitrageBot {
    constructor(config) {
        this.config = config;
        this.synthesis = new SynthesisClient({
            apiKey: config.apiKey,
            walletId: config.walletId,
            baseUrl: config.baseUrl,
            timeoutMs: config.requestTimeoutMs,
        });
        this.currentPosition = null;
        this.running = false;
    }

    walletSegment(platform) {
        return platform === 'polymarket' ? this.config.polymarketWalletSegment : this.config.kalshiWalletSegment;
    }

    async listVenue(venue) {
        const outcomes = [];
        const page = this.config.pageSize;
        for (let offset = 0; outcomes.length < this.config.maxMarketsPerVenue; offset += page) {
            const payload = await this.synthesis.listMarkets({ venue, limit: page, offset });
            const parsed = parseOutcomes(payload, venue);
            for (const outcome of parsed) {
                if (outcome.resolved) continue;
                if (this.config.titleFilter && !outcome.title.toLowerCase().includes(this.config.titleFilter)) continue;
                outcomes.push(outcome);
            }
            if (parsed.length < page) break; // short page => end of the listing
        }
        return outcomes.slice(0, this.config.maxMarketsPerVenue);
    }

    async fetchMarkets() {
        const [polymarketOutcomes, kalshiOutcomes] = await Promise.all([
            this.listVenue('polymarket'),
            this.listVenue('kalshi'),
        ]);
        return { polymarketOutcomes, kalshiOutcomes };
    }

    async attachBooks(outcomes) {
        const books = await this.synthesis.fetchBooks(outcomes.flatMap((o) => [o.yesId, o.noId]));
        applyBooks(outcomes, books);
    }

    async executeTrade(platform, tokenId, side, shares, priceCents) {
        if (this.config.dryRun) {
            console.log(`   [DRY RUN] ${platform} ${side.toUpperCase()} ${shares} contracts of ${tokenId} @ ~${priceCents.toFixed(2)}¢`);
            return { success: true, orderId: 'dry-run-' + Date.now() };
        }

        const priceCap = Math.min((priceCents + this.config.slippageCapCents) / 100, 0.999);
        try {
            const order = await this.synthesis.placeMarketOrder({
                segment: this.walletSegment(platform),
                tokenId,
                side,
                shares,
                priceCap,
            });
            return { success: true, orderId: order.orderId, order };
        } catch (error) {
            console.error(`   [ERROR] ${platform} ${side} failed: ${error.message}`);
            return { success: false, error: error.message };
        }
    }

    /**
     * Buy both legs of `opportunity` in equal size.
     *
     * Both legs get the SAME number of contracts: a set only pays its
     * guaranteed 100¢ if the YES and NO holdings match, so sizing each leg
     * independently by its own price would leave a directional remainder.
     */
    async executeArbitrage(opportunity) {
        console.log(`\n[EXECUTE] ${opportunity.outcome}: ${opportunity.description} | Profit: ${opportunity.profit.toFixed(2)}¢/set`);

        const capitalCents = this.config.tradingMode === 'YOLO'
            ? this.config.yoloTradeAmountCents
            : this.config.tradeAmountCents;
        const sets = Math.floor(Math.min(
            capitalCents / opportunity.totalCost,
            this.config.maxSetsPerArb,
            opportunity.maxSets,
        ));
        if (sets < 1) {
            console.log(`   [SKIP] book depth ${opportunity.maxSets} / capital ${capitalCents}¢ supports < 1 set\n`);
            return false;
        }

        const results = await Promise.all(opportunity.legs.map((leg) =>
            this.executeTrade(leg.platform, leg.tokenId, 'buy', sets, leg.price)));

        const filled = opportunity.legs
            .map((leg, i) => ({ leg, result: results[i] }))
            .filter(({ result }) => result.success);

        if (filled.length === 0) {
            console.log('[ERROR] Trade execution failed — no legs filled\n');
            return false;
        }

        const legged = filled.length !== opportunity.legs.length;
        this.currentPosition = {
            opportunity,
            sets,
            legged,
            legs: filled.map(({ leg, result }) => ({
                platform: leg.platform,
                marketId: leg.marketId,
                tokenId: leg.tokenId,
                side: leg.side,
                shares: sets,
                entryPrice: leg.price,
                orderId: result.orderId,
            })),
            entryTime: Date.now(),
        };

        if (legged) {
            // One leg filled and the other didn't: the position is a naked
            // directional bet, not a hedge. Unwind now rather than at the next poll.
            console.log(`[LEGGED] only ${filled.length}/${opportunity.legs.length} legs filled — unwinding immediately`);
            await this.exitPosition();
            return false;
        }

        console.log(`[FILLED] ${sets} sets @ ${opportunity.totalCost.toFixed(2)}¢ — locks ${(sets * opportunity.profit / 100).toFixed(2)}$\n`);
        return true;
    }

    /**
     * Unrealized P&L of the open position, in cents.
     *
     * Marked at the best BID — what a sale would actually fetch — not at the ask
     * we paid, so an untouched position doesn't read as flat when the spread has
     * moved against it.
     */
    calculateCurrentPnL(outcomes) {
        if (!this.currentPosition) return 0;

        let pnl = 0;
        for (const leg of this.currentPosition.legs) {
            const now = outcomes.find((o) => o.platform === leg.platform && o.marketId === leg.marketId);
            if (!now) return 0; // position not in this cycle's matched set; can't mark it honestly
            const bid = leg.side === 'YES' ? now.yesBid : now.noBid;
            if (bid == null) return 0;
            pnl += leg.shares * (bid - leg.entryPrice);
        }
        return pnl;
    }

    shouldExitPosition(opportunities) {
        if (!this.currentPosition) return false;
        if (this.currentPosition.legged) return true;

        // Exit if the opportunity we entered is gone or no longer profitable
        const currentOpp = opportunities.find((opp) => opp.outcome === this.currentPosition.opportunity.outcome);
        if (!currentOpp || currentOpp.profit < this.config.minProfitCents) return true;

        // Exit if a BETTER opportunity exists (Rotation)
        const bestOpp = getBestOpportunity(opportunities);
        if (bestOpp && bestOpp.profit > currentOpp.profit) {
            console.log(`[ROTATION] Found better opportunity: ${bestOpp.outcome} (${bestOpp.profit.toFixed(2)}¢) > ${currentOpp.outcome} (${currentOpp.profit.toFixed(2)}¢)`);
            return true;
        }

        return false;
    }

    async exitPosition() {
        if (!this.currentPosition) return;
        const heldTime = Math.round((Date.now() - this.currentPosition.entryTime) / 1000);
        console.log(`\n[EXITING] ${this.currentPosition.opportunity.outcome} (held ${heldTime}s) - Executing SELL orders...`);

        const legs = this.currentPosition.legs;
        const results = await Promise.all(legs.map((leg) =>
            this.executeTrade(leg.platform, leg.tokenId, 'sell', leg.shares, leg.entryPrice)));

        const unsold = legs.filter((_, i) => !results[i].success);
        if (unsold.length === 0) {
            console.log('[SOLD] Position closed successfully.\n');
            this.currentPosition = null;
            return;
        }

        // Keep the unsold legs on the books — dropping them here would hide a
        // live, unhedged position from every later poll.
        console.log(`[ERROR] ${unsold.length} leg(s) still open: ${unsold.map((l) => `${l.platform}:${l.tokenId}`).join(', ')}\n`);
        this.currentPosition.legs = unsold;
        this.currentPosition.legged = true;
    }

    async poll() {
        try {
            const { polymarketOutcomes, kalshiOutcomes } = await this.fetchMarkets();
            const matches = matchOutcomes(polymarketOutcomes, kalshiOutcomes, this.config.matchingThreshold);
            const matched = matches.flatMap((m) => [m.polymarket, m.kalshi]);
            await this.attachBooks(matched);
            // Report book coverage: a venue that serves no books can't be priced, and
            // that reads as "no arbs" forever unless it's visible.
            const priced = matched.filter((o) => o.yesPrice != null && o.noPrice != null).length;
            console.log(`[SCAN] ${polymarketOutcomes.length} polymarket / ${kalshiOutcomes.length} kalshi markets `
                + `-> ${matches.length} matched pairs, books on ${priced}/${matched.length} sides`);

            const allOpportunities = findArbitrageOpportunities(matches, this.config.minProfitCents)
                .filter((opp) => {
                    // Skip markets where any side is at or below the threshold: a
                    // near-zero quote is usually a stale book, not a real offer.
                    const prices = [
                        opp.polymarketOutcome.yesPrice, opp.polymarketOutcome.noPrice,
                        opp.kalshiOutcome.yesPrice, opp.kalshiOutcome.noPrice,
                    ];
                    return prices.every((p) => p != null && p > this.config.minPriceThreshold);
                })
                .map((opp) => ({
                    ...opp,
                    totalVolume: (opp.polymarketOutcome.volume || 0) + (opp.kalshiOutcome.volume || 0),
                }))
                .sort((a, b) => b.totalVolume - a.totalVolume); // First rank by volume

            // Then take top N and re-sort by profit
            const topOpportunities = allOpportunities
                .slice(0, this.config.topNOpportunities)
                .sort((a, b) => b.profit - a.profit);

            const currentPnL = this.calculateCurrentPnL(matched);
            console.log(`[CURRENT PnL: ${currentPnL.toFixed(2)}¢]`);

            if (topOpportunities.length > 0) {
                console.log(`[TOP ${topOpportunities.length} OPPORTUNITIES BY PROFIT (FROM TOP VOLUME MARKETS)]`);
                topOpportunities.forEach((opp, i) => {
                    console.log(`  ${i + 1}. ${opp.outcome}: ${opp.description} | Profit: ${opp.profit.toFixed(2)}¢ | Depth: ${opp.maxSets} | Vol: $${(opp.totalVolume || 0).toLocaleString()}`);
                });
                console.log('');
            }

            if (this.shouldExitPosition(topOpportunities)) await this.exitPosition();
            if (!this.currentPosition && topOpportunities.length > 0) {
                await this.executeArbitrage(getBestOpportunity(topOpportunities));
            }
        } catch (error) {
            console.error('[ERROR]', error.message);
            console.error(error.stack);
        }
    }

    async start() {
        console.log(`[BOT STARTED] Polling every ${this.config.pollIntervalSeconds}s | Min profit: ${this.config.minProfitCents}¢ | Mode: ${this.config.tradingMode} | Dry run: ${this.config.dryRun ? 'YES' : 'NO'}\n`);
        this.running = true;
        // Self-scheduling rather than setInterval: a scan slower than the interval
        // would otherwise overlap with the next one and double-enter a position.
        const loop = async () => {
            console.log(`[${new Date().toLocaleTimeString()}]`);
            await this.poll();
            if (this.running) this.pollTimer = setTimeout(loop, this.config.pollIntervalSeconds * 1000);
        };
        await loop();
    }

    stop() {
        this.running = false;
        if (this.pollTimer) clearTimeout(this.pollTimer);
        console.log('\n[BOT STOPPED]');
    }
}
