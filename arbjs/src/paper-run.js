/**
 * Entry point for the paper trader: `npm run paper`.
 *
 * Places no orders and needs no wallet — it only reads market data, so it runs
 * with or without an API key. State is persisted between runs so a position can
 * be carried until the underlying event actually resolves.
 */

import { loadDotenv } from './env.js';

loadDotenv();

const { config } = await import('../config.js');
const { ArbitrageBot } = await import('./bot.js');
const { PaperBook, printBoard, runPaper } = await import('./paper.js');

const bot = new ArbitrageBot({ ...config, dryRun: true });
const book = PaperBook.load(config.paperStatePath, config.paperBankrollCents);

console.log('ARBJS PAPER TRADER — simulated fills on live prices, no orders placed\n');
console.log(`bankroll $${(book.startBankrollCents / 100).toFixed(2)}`
    + ` · max $${(config.paperMaxArbCostCents / 100).toFixed(2)} per arb (both legs combined)`
    + ` · min edge ${config.minProfitCents}¢/set · scan every ${config.pollIntervalSeconds}s`);
console.log(`state: ${config.paperStatePath} (${book.positions.length} position(s) carried over)\n`);

process.on('SIGINT', () => {
    book.stop = true;
    book.save(config.paperStatePath);
    console.log('\n\nStopped. Final book:');
    printBoard(book);
    process.exit(0);
});

await runPaper(bot, book, {
    maxArbCostCents: config.paperMaxArbCostCents,
    intervalSeconds: config.pollIntervalSeconds,
    statePath: config.paperStatePath,
});
