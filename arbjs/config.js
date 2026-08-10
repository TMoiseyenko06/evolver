/**
 * Bot configuration. Everything is env-overridable; the defaults are the safe ones.
 *
 * Live trading is opt-in: `dryRun` stays true unless ARBJS_LIVE=true, so a
 * misconfigured run prints trades instead of placing them.
 */

const envNum = (name, fallback) => {
    const value = Number(process.env[name]);
    return Number.isFinite(value) ? value : fallback;
};

export const config = {
    // --- Synthesis ---------------------------------------------------------
    apiKey: process.env.SYNTHESIS_API_KEY || '',
    walletId: process.env.SYNTHESIS_WALLET_ID || '',
    baseUrl: process.env.SYNTHESIS_BASE_URL || 'https://synthesis.trade',
    requestTimeoutMs: envNum('SYNTHESIS_TIMEOUT_MS', 30000),
    // Wallet path segment per venue: /api/v1/wallet/{segment}/{wallet_id}/order.
    // 'pol' is the segment polybot uses for Polymarket. The Kalshi segment is
    // NOT guessed — set it once you have confirmed it, or Kalshi stays dry-run.
    polymarketWalletSegment: process.env.SYNTHESIS_POLYMARKET_WALLET_SEGMENT || 'pol',
    kalshiWalletSegment: process.env.SYNTHESIS_KALSHI_WALLET_SEGMENT || '',

    // --- Safety ------------------------------------------------------------
    dryRun: process.env.ARBJS_LIVE !== 'true',
    slippageCapCents: envNum('ARBJS_SLIPPAGE_CAP_CENTS', 1),

    // --- Universe ----------------------------------------------------------
    // 0 = no cap: page both venues to exhaustion (~127k Polymarket, ~44k Kalshi
    // markets, ~30s). Arbs hide in the thin tail, so the whole universe is the
    // default and a cap is only for quick tests.
    maxMarketsPerVenue: envNum('ARBJS_MAX_MARKETS', 0),
    pageSize: envNum('ARBJS_PAGE_SIZE', 250),
    // Substring filter on market titles, e.g. 'bitcoin' — narrows the scan to one
    // family of markets the way the old market-URL config did.
    titleFilter: (process.env.ARBJS_TITLE_FILTER || '').toLowerCase(),

    // --- Strategy ----------------------------------------------------------
    matchingThreshold: envNum('ARBJS_MATCH_THRESHOLD', 0.7),
    minProfitCents: envNum('ARBJS_MIN_PROFIT_CENTS', 1),
    minPriceThreshold: envNum('ARBJS_MIN_PRICE_CENTS', 2),
    topNOpportunities: envNum('ARBJS_TOP_N', 5),
    pollIntervalSeconds: envNum('ARBJS_POLL_SECONDS', 30),

    // --- Sizing (per arb, in cents of capital) ------------------------------
    tradingMode: process.env.ARBJS_TRADING_MODE || 'NORMAL', // NORMAL | YOLO
    tradeAmountCents: envNum('ARBJS_TRADE_CENTS', 500),
    yoloTradeAmountCents: envNum('ARBJS_YOLO_TRADE_CENTS', 1000),
    maxSetsPerArb: envNum('ARBJS_MAX_SETS', 200),

    // --- Paper trading (npm run paper) --------------------------------------
    paperBankrollCents: envNum('ARBJS_PAPER_BANKROLL_CENTS', 50000), // $500
    // Cap on ONE arbitrage: both legs combined, fees included.
    paperMaxArbCostCents: envNum('ARBJS_PAPER_MAX_ARB_CENTS', 10000), // $100
    paperStatePath: process.env.ARBJS_PAPER_STATE || 'paper-state.json',
};
