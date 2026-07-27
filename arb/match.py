"""Cross-venue event matching: find the SAME event on Polymarket and Kalshi.

The venues name the same event completely differently — "Will Trump win the 2024
election?" vs "Presidential winner → Trump" — so string matching alone fails. The
pipeline is:

1. **Fetch broadly** from both venues (thousands of markets, not just top-volume).
2. **Block** cheaply: an inverted token index proposes candidate pairs that share
   enough significant words to be plausibly related (avoids N×M LLM calls).
3. **Confirm semantically** with an LLM: for each candidate, does it resolve the SAME
   real-world event under the SAME criteria, and which outcomes correspond? The LLM
   handles the wording gap AND — critically — rejects pairs whose settlement differs
   (different reference price / window), which is the basis-risk killer.

Without an LLM key it falls back to fuzzy scoring (unconfirmed candidates).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from polybot.synthesis import SynthesisClient

from . import detect, scan
from .model import Market

log = logging.getLogger("arb.match")


@dataclass
class EventMatch:
    poly: Market
    kalshi: Market
    confidence: float
    outcome_map: Dict[str, str]   # polymarket outcome -> kalshi outcome
    reason: str = ""
    block_score: float = 0.0


# --------------------------------------------------------------------------- #
# Fetch + block
# --------------------------------------------------------------------------- #
def fetch_broad(client: SynthesisClient, venue: str, target: int = 20000) -> List[Market]:
    """Pull the full market universe from a venue (paginated to exhaustion).

    Arbs live in thin, low-volume markets, so we pull everything rather than the
    high-volume top — see ``scan.list_all``.
    """
    return scan.list_all(client, venue, max_markets=target)


# The inverted-index blocking lives in detect (shared with scan_cross); re-export it.
candidate_pairs = detect.candidate_pairs


# --------------------------------------------------------------------------- #
# LLM confirmation
# --------------------------------------------------------------------------- #
SYSTEM_MATCH = (
    "You are matching prediction markets across two venues (Polymarket and Kalshi). "
    "Two markets MATCH only if they resolve the SAME real-world event under the SAME "
    "criteria — same underlying question, same resolution date, and (for numeric/price "
    "markets) the SAME threshold and reference source. Different wording is fine; "
    "different settlement (different price source, window, or line/threshold) is NOT a "
    "match. Be strict: a false match loses real money. For each numbered pair, decide."
)


def build_match_prompt(pairs: List[Tuple[Market, Market, float, int]]) -> str:
    lines = [
        "For each pair below, output a JSON array of objects with keys: "
        '"i" (the pair number), "match" (true/false), "confidence" (0-1), '
        '"map" (object mapping the Polymarket outcome label to the equivalent Kalshi '
        'outcome label, or {} if no match), and "reason" (short). Output ONLY the JSON.\n',
    ]
    for i, (pm, km, _, _) in enumerate(pairs):
        po = "/".join(q.outcome for q in pm.quotes)
        ko = "/".join(q.outcome for q in km.quotes)
        lines.append(
            f"[{i}] POLY: {pm.title!r} outcomes={po} ends={pm.ends_at}\n"
            f"     KALSHI: {km.title!r} outcomes={ko} ends={km.ends_at}"
        )
    return "\n".join(lines)


def parse_match_response(text: str) -> List[dict]:
    """Extract the JSON array of decisions from an LLM reply, tolerantly."""
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def llm_confirm(llm, pairs: List[Tuple[Market, Market, float, int]],
                batch: int = 15, min_confidence: float = 0.6) -> List[EventMatch]:
    """Confirm candidate pairs with the LLM in batches; keep confident matches."""
    matches: List[EventMatch] = []
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        try:
            reply = llm.chat(SYSTEM_MATCH, build_match_prompt(chunk))
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM match batch failed: %s", exc)
            continue
        for d in parse_match_response(reply):
            if not (isinstance(d, dict) and d.get("match")):
                continue
            i = d.get("i")
            if not isinstance(i, int) or not (0 <= i < len(chunk)):
                continue
            conf = float(d.get("confidence") or 0.0)
            if conf < min_confidence:
                continue
            pm, km, score, _ = chunk[i]
            matches.append(EventMatch(
                poly=pm, kalshi=km, confidence=conf,
                outcome_map=d.get("map") if isinstance(d.get("map"), dict) else {},
                reason=str(d.get("reason") or ""), block_score=score,
            ))
    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches


def match_venues(
    client: SynthesisClient, llm=None, target: int = 20000, min_shared: int = 2,
    max_pairs: int = 3000, min_confidence: float = 0.6,
) -> List[EventMatch]:
    """End-to-end: fetch both venues, block, and (if an LLM is given) confirm matches."""
    poly = fetch_broad(client, "polymarket", target)
    kalshi = fetch_broad(client, "kalshi", target)
    log.info("fetched %d polymarket, %d kalshi markets", len(poly), len(kalshi))
    pairs = candidate_pairs(poly, kalshi, min_shared, max_pairs)
    log.info("%d candidate pairs after blocking (>=%d shared words)", len(pairs), min_shared)
    if llm is None:
        # Fuzzy-only fallback: return the top blocked pairs as unconfirmed candidates.
        return [EventMatch(pm, km, score, {}, "fuzzy (unconfirmed — no LLM)", score)
                for pm, km, score, _ in pairs if score >= 0.5]
    return llm_confirm(llm, pairs, min_confidence=min_confidence)
