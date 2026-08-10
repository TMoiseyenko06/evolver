/**
 * Cross-venue arbitrage math, in cents per $1 set.
 *
 * A matched pair of markets resolves to the same event, so buying YES on one
 * venue and NO on the other covers both outcomes: exactly one side pays $1
 * (100¢) at resolution. If the two legs together cost less than 100¢ *after
 * fees*, the difference is locked profit per set, whatever the outcome.
 *
 * Two things this module refuses to fake, both learned the hard way elsewhere
 * in this repo:
 *  - **Fees are part of the cost.** A 0.4¢ gross spread is a loss after the
 *    taker fee on both legs, so `totalCost` includes it.
 *  - **Displayed asks are not executable.** `executableAsk` applies the same
 *    no-arb floor as `evolver.engine.executable_price`: a resting ask cannot
 *    execute below `1 - complement_best_bid`, because a market maker bidding
 *    for the other side of the same market is implicitly offering this side
 *    there. Without it a phantom cheap ask manufactures an arb that doesn't exist.
 */

export const SLIPPAGE_COEFF = 0.55;
export const SLIPPAGE_EXP = 2.0;

// Per-share taker fee at price p (dollars), mirroring arb/fees.py.
const FEE_MODELS = {
    polymarket: (p) => 0.0312 * Math.min(p, 1 - p),
    kalshi: (p) => 0.07 * p * (1 - p),
};

export function feePerShareCents(venue, priceCents) {
    const model = FEE_MODELS[String(venue || '').toLowerCase()];
    return model ? model(priceCents / 100) * 100 : 0;
}

/** Fallback fill correction used only when the complement book is unavailable. */
export function applySlippage(price, coeff = SLIPPAGE_COEFF, exp = SLIPPAGE_EXP) {
    if (coeff <= 0 || price <= 0) return price;
    const gap = 0.5 - price;
    if (gap <= 0) return price; // buying the favorite side: the displayed book is realistic enough
    return Math.min(price + coeff * gap ** exp, 0.999);
}

/** The price a buyer actually pays for `ask`, given the complement outcome's best bid. */
export function executableAsk(ask, complementBid) {
    if (!(ask > 0)) return null;
    if (complementBid > 0 && complementBid < 1) {
        return Math.min(Math.max(ask, 1 - complementBid), 0.999);
    }
    return applySlippage(ask);
}

function buildLeg(outcome, side) {
    const price = side === 'YES' ? outcome.yesPrice : outcome.noPrice;
    if (price == null || !(price > 0)) return null; // no book on this side -> not tradeable
    return {
        platform: outcome.platform,
        marketId: outcome.marketId,
        title: outcome.title,
        side,
        tokenId: side === 'YES' ? outcome.yesId : outcome.noId,
        bookKey: side === 'YES' ? outcome.yesBookKey : outcome.noBookKey,
        bookRequestId: side === 'YES' ? outcome.yesBookRequestId : outcome.noBookRequestId,
        price,
        size: (side === 'YES' ? outcome.yesSize : outcome.noSize) || 0,
        fee: feePerShareCents(outcome.platform, price),
    };
}

function buildStrategy(type, description, polymarketLeg, kalshiLeg) {
    const grossCost = polymarketLeg.price + kalshiLeg.price;
    const fees = polymarketLeg.fee + kalshiLeg.fee;
    const totalCost = grossCost + fees;
    return {
        type,
        description,
        polymarketSide: polymarketLeg.side,
        kalshiSide: kalshiLeg.side,
        grossCost,
        fees,
        totalCost,
        profit: 100 - totalCost,
        maxSets: Math.min(polymarketLeg.size, kalshiLeg.size),
        legs: [polymarketLeg, kalshiLeg],
    };
}

export function calculateArbitrage(match) {
    const { polymarket, kalshi } = match;
    const strategies = [];

    const polyYes = buildLeg(polymarket, 'YES');
    const kalshiNo = buildLeg(kalshi, 'NO');
    if (polyYes && kalshiNo) {
        strategies.push(buildStrategy(
            'STRATEGY_1',
            `Buy YES on Polymarket (${polyYes.price.toFixed(2)}¢), Buy NO on Kalshi (${kalshiNo.price.toFixed(2)}¢)`,
            polyYes, kalshiNo,
        ));
    }

    const polyNo = buildLeg(polymarket, 'NO');
    const kalshiYes = buildLeg(kalshi, 'YES');
    if (polyNo && kalshiYes) {
        strategies.push(buildStrategy(
            'STRATEGY_2',
            `Buy YES on Kalshi (${kalshiYes.price.toFixed(2)}¢), Buy NO on Polymarket (${polyNo.price.toFixed(2)}¢)`,
            polyNo, kalshiYes,
        ));
    }

    // Stable sort keeps STRATEGY_1 ahead of an equally profitable STRATEGY_2.
    const best = strategies.filter((s) => s.profit > 0).sort((a, b) => b.profit - a.profit)[0];
    if (!best) return null;

    return {
        outcome: polymarket.title,
        similarity: match.similarity,
        ...best,
        polymarketOutcome: polymarket,
        kalshiOutcome: kalshi,
    };
}

export function findArbitrageOpportunities(matches, minProfit = 1) {
    const opportunities = [];
    for (const match of matches) {
        const arb = calculateArbitrage(match);
        if (arb && arb.profit >= minProfit) opportunities.push(arb);
    }
    opportunities.sort((a, b) => b.profit - a.profit);
    return opportunities;
}

export function getBestOpportunity(opportunities) {
    return opportunities.length > 0 ? opportunities[0] : null;
}
