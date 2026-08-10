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
 * Scanning the whole Synthesis universe means thousands of titles per venue, so
 * a naive N x M Levenshtein sweep is not viable. Two exact prunes (neither can
 * drop a pair that would have cleared `threshold`):
 *  1. An inverted index proposes only pairs sharing a significant word. A pair
 *     with no shared word scores jaccard 0, capping the blend at 0.4; a pair
 *     sharing only stop words lands far below any usable threshold.
 *  2. `jaccard * 0.6 + 0.4` is the best score a pair could reach even with a
 *     perfect edit distance, so anything under `threshold` skips the Levenshtein.
 */
export function matchOutcomes(polymarketOutcomes, kalshiOutcomes, threshold = 0.7) {
    const matches = [];
    const usedKalshiIndices = new Set();

    const index = new Map();
    kalshiOutcomes.forEach((outcome, i) => {
        for (const token of significantTokens(outcome.title)) {
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
