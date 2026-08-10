/**
 * Fuzzy title matching between Polymarket and Kalshi outcomes.
 *
 * Scoring is a blend of word-set overlap (Jaccard) and character edit distance
 * (Levenshtein), so it survives both reworded titles and spelling variants.
 *
 * A matched title is a CANDIDATE, never proof: two venues can word the same
 * question identically and still settle it against different reference prices
 * or windows, in which case the "arb" is an unhedged directional bet on both
 * legs. Confirm settlement equivalence before trading a pair.
 */

// Words too common to say anything about whether two titles are the same event.
const STOP_WORDS = new Set([
    'will', 'the', 'a', 'an', 'be', 'to', 'of', 'in', 'on', 'at', 'by', 'for',
    'and', 'or', 'is', 'are', 'this', 'that', 'than', 'market',
]);

export function jaccardSimilarity(str1, str2) {
    const set1 = new Set(str1.toLowerCase().split(/\s+/));
    const set2 = new Set(str2.toLowerCase().split(/\s+/));
    const intersection = new Set([...set1].filter((x) => set2.has(x)));
    const union = new Set([...set1, ...set2]);
    return intersection.size / union.size;
}

export function levenshteinDistance(str1, str2) {
    const matrix = [];
    for (let i = 0; i <= str2.length; i++) matrix[i] = [i];
    for (let j = 0; j <= str1.length; j++) matrix[0][j] = j;

    for (let i = 1; i <= str2.length; i++) {
        for (let j = 1; j <= str1.length; j++) {
            if (str2.charAt(i - 1) === str1.charAt(j - 1)) {
                matrix[i][j] = matrix[i - 1][j - 1];
            } else {
                matrix[i][j] = Math.min(matrix[i - 1][j - 1] + 1, matrix[i][j - 1] + 1, matrix[i - 1][j] + 1);
            }
        }
    }
    return matrix[str2.length][str1.length];
}

export function combinedSimilarity(str1, str2) {
    const jaccard = jaccardSimilarity(str1, str2);
    return blend(jaccard, str1, str2);
}

function blend(jaccard, str1, str2) {
    const distance = levenshteinDistance(str1.toLowerCase(), str2.toLowerCase());
    const maxLength = Math.max(str1.length, str2.length);
    const levenshtein = maxLength === 0 ? 1 : 1 - distance / maxLength;
    return jaccard * 0.6 + levenshtein * 0.4;
}

function significantTokens(title) {
    return new Set(
        String(title || '')
            .toLowerCase()
            .split(/\s+/)
            .filter((word) => word.length > 1 && !STOP_WORDS.has(word)),
    );
}

/**
 * Greedily pair each Polymarket outcome with its best unused Kalshi outcome.
 *
 * The full Synthesis universe is ~127k Polymarket and ~44k Kalshi markets, so a
 * naive N x M Levenshtein sweep (5.6 billion pairs) is not viable. Three prunes:
 *
 *  1. **An inverted index** proposes only pairs sharing a significant word. A
 *     pair with no shared word scores jaccard 0, capping the blend at 0.4 — below
 *     any usable threshold — so this drops nothing reachable.
 *  2. **Ubiquitous words are not indexed.** Tokens appearing in more than
 *     `commonTokenFraction` of the venue's titles ("above", "2026", "district")
 *     carry no signal about *which* event a title names, and indexing them makes
 *     a single title propose thousands of candidates. A real pair still shares
 *     something distinctive — a name, a number, a place. This is the one prune
 *     that could in principle miss a pair whose every shared word is ubiquitous;
 *     such a pair scores far too low to match anyway.
 *  3. **A jaccard bound skips the expensive step.** `jaccard * 0.6 + 0.4` is the
 *     best score a pair could reach even with a perfect edit distance, so
 *     anything under `threshold` never runs Levenshtein.
 */
export function matchOutcomes(polymarketOutcomes, kalshiOutcomes, threshold = 0.7, { commonTokenFraction = 0.005 } = {}) {
    const matches = [];
    const usedKalshiIndices = new Set();

    const kalshiTokens = kalshiOutcomes.map((outcome) => significantTokens(outcome.title));
    const documentFrequency = new Map();
    for (const tokens of kalshiTokens) {
        for (const token of tokens) documentFrequency.set(token, (documentFrequency.get(token) || 0) + 1);
    }
    const commonCut = Math.max(5, Math.floor(commonTokenFraction * kalshiOutcomes.length));

    const index = new Map();
    kalshiTokens.forEach((tokens, i) => {
        for (const token of tokens) {
            if (documentFrequency.get(token) > commonCut) continue;
            if (!index.has(token)) index.set(token, []);
            index.get(token).push(i);
        }
    });

    for (const polyOutcome of polymarketOutcomes) {
        const candidates = new Set();
        for (const token of significantTokens(polyOutcome.title)) {
            for (const i of index.get(token) || []) candidates.add(i);
        }

        let bestMatch = null;
        let bestScore = 0;
        let bestIndex = -1;

        for (const i of candidates) {
            if (usedKalshiIndices.has(i)) continue;
            const jaccard = jaccardSimilarity(polyOutcome.title, kalshiOutcomes[i].title);
            if (jaccard * 0.6 + 0.4 < threshold) continue;
            const score = blend(jaccard, polyOutcome.title, kalshiOutcomes[i].title);
            if (score > bestScore && score >= threshold) {
                bestScore = score;
                bestMatch = kalshiOutcomes[i];
                bestIndex = i;
            }
        }

        if (bestMatch) {
            matches.push({ polymarket: polyOutcome, kalshi: bestMatch, similarity: bestScore });
            usedKalshiIndices.add(bestIndex);
        }
    }

    return matches;
}
