/**
 * Entry point for the cross-venue arbitrage bot.
 */

import { loadDotenv } from './env.js';

loadDotenv();

const { config } = await import('../config.js');
const { ArbitrageBot } = await import('./bot.js');

async function main() {
    console.clear();
    console.log('CROSS-VENUE PREDICTION MARKET ARBITRAGE BOT - synthesis.trade\n');

    if (!config.apiKey) {
        console.warn('Warning: SYNTHESIS_API_KEY not set — market data may be public, but set it if you get 401s.');
    }

    if (!config.dryRun) {
        if (!config.apiKey || !config.walletId) {
            console.error('Error: SYNTHESIS_API_KEY and SYNTHESIS_WALLET_ID are required for live trading');
            console.error('   Unset ARBJS_LIVE to run in dry-run mode without credentials');
            process.exit(1);
        }
        if (!config.kalshiWalletSegment) {
            console.error('Error: SYNTHESIS_KALSHI_WALLET_SEGMENT is not set, so the Kalshi leg cannot be placed');
            console.error('   Every opportunity here is cross-venue — a Polymarket-only fill is an unhedged bet');
            process.exit(1);
        }
        console.log('LIVE TRADING ENABLED — orders will be placed with real money.\n');
    }

    const bot = new ArbitrageBot(config);

    process.on('SIGINT', () => {
        bot.stop();
        process.exit(0);
    });

    try {
        await bot.start();
    } catch (error) {
        console.error('Fatal error:', error.message);
        console.error(error.stack);
        process.exit(1);
    }
}

main();
