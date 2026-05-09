"""Thesis builder — the actual money math.

For each MentionMarket + CorpusPackage, produces a TermThesis with:
  - Base rate from prior events (with Laplace smoothing at extremes)
  - Context adjustment (Claude NLP when available, heuristic otherwise)
  - Adjusted probability = sigmoid(logit(base_rate) + context_delta)
  - Edge per side (YES and NO)
  - Kelly sizing w/ confidence weighting
  - Classification: HOMERUN | HIGH | STANDARD | SKIP
  - Human-readable reasoning

Output: list of TermThesis sorted by edge, plus event-level summary.

Dependencies are INJECTED (base_rate_fn, context_scorer_fn, market_price_fn)
so tests can provide deterministic mocks.
"""
from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.corpus_compiler import CorpusPackage
from prep.term_expander import MentionMarket

_log = logging.getLogger(__name__)


# ── Sizing thresholds (matches brief in MEMORY.md) ──────────────────
CONVICTION_THRESHOLDS = {
    "HOMERUN":   {"min_edge": 0.25, "min_confidence": 0.80, "kelly_frac": 1.00, "max_pct": 0.15},
    "HIGH":      {"min_edge": 0.15, "min_confidence": 0.60, "kelly_frac": 0.50, "max_pct": 0.08},
    "STANDARD":  {"min_edge": 0.08, "min_confidence": 0.40, "kelly_frac": 0.25, "max_pct": 0.04},
}


@dataclass
class TermThesis:
    """A single thesis — one term, one market, one position recommendation."""
    ticker:            str
    word_raw:          str
    word_variants:     list[str]
    base_rate:         float              # raw frequency from corpus
    adjusted_prob:     float              # after context scoring
    context_delta:     float              # logit delta applied
    confidence:        float              # 0-1, derived from corpus depth
    corpus_n:          int                # # of prior events in base rate
    yes_bid:           Optional[int]      # cents or None (illiquid)
    yes_ask:           Optional[int]
    edge_yes:          float
    edge_no:           float
    best_side:         str                # "yes", "no", or "skip"
    best_edge:         float
    conviction:        str                # HOMERUN | HIGH | STANDARD | SKIP
    kelly_fraction:    float              # suggested pct of bankroll
    recommended_usd:   float              # $ to deploy given bankroll
    reasoning:         str
    mention_count_recent: int             # how many of last N events mentioned

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "word": self.word_raw,
            "base_rate": round(self.base_rate, 3),
            "adjusted_prob": round(self.adjusted_prob, 3),
            "context_delta": round(self.context_delta, 3),
            "confidence": round(self.confidence, 2),
            "corpus_n": self.corpus_n,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "edge_yes": round(self.edge_yes, 3),
            "edge_no": round(self.edge_no, 3),
            "best_side": self.best_side,
            "best_edge": round(self.best_edge, 3),
            "conviction": self.conviction,
            "kelly_fraction": round(self.kelly_fraction, 3),
            "recommended_usd": round(self.recommended_usd, 2),
            "reasoning": self.reasoning,
        }


# ── Math primitives ─────────────────────────────────────────────────
def _logit(p: float) -> float:
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x > 50:  return 0.999
    if x < -50: return 0.001
    return 1.0 / (1.0 + math.exp(-x))


def _laplace_smooth(k: int, n: int) -> float:
    """Additive-one smoothing — avoids 0.0 or 1.0 base rates.
    P_smoothed = (k+1) / (n+2). At k=0,n=10: 0.083; at k=10,n=10: 0.917."""
    return (k + 1) / (n + 2)


def _corpus_confidence(n: int) -> float:
    """Map corpus size to confidence weight 0-1."""
    if n >= 40: return 1.0
    if n >= 20: return 0.85
    if n >= 10: return 0.65
    if n >= 5:  return 0.40
    if n >= 3:  return 0.20
    return 0.0


def _kelly_fraction(prob: float, price: float) -> float:
    """Classical Kelly: f* = (b*p - q) / b, where b=(1-price)/price."""
    if price <= 0 or price >= 1 or prob <= 0 or prob >= 1:
        return 0.0
    b = (1 - price) / price
    q = 1 - prob
    f = (b * prob - q) / b
    return max(0.0, f)


# ── Core builder ─────────────────────────────────────────────────────
def classify_conviction(edge: float, confidence: float) -> str:
    """Map (edge, confidence) to conviction tier name."""
    for tier, thr in CONVICTION_THRESHOLDS.items():
        if edge >= thr["min_edge"] and confidence >= thr["min_confidence"]:
            return tier
    return "SKIP"


def build_thesis(
    market:          MentionMarket,
    corpus:          CorpusPackage,
    base_rate_fn:    Callable[[str, list[str]], tuple[float, int, int]],
    context_scorer_fn: Callable[[str, float, list[dict]], tuple[float, str]],
    bankroll_usd:    float,
    kalshi_fee_rate: float = 0.07,  # 7% of P(1-P)
) -> TermThesis:
    """Build a single TermThesis for one market.

    base_rate_fn(event_type, variants) → (base_rate, k_mentioned, n_total)
    context_scorer_fn(word_raw, base_rate, recent_transcripts) → (delta, reasoning)
    """
    # Step 1: base rate with Laplace smoothing at extremes
    raw_rate, k, n = base_rate_fn(corpus.event_type, market.word_variants)
    if n == 0:
        base_rate = 0.5   # no data → coin flip prior
    elif k == 0 or k == n:
        base_rate = _laplace_smooth(k, n)
    else:
        base_rate = raw_rate

    # Step 2: context adjustment
    context_delta, reasoning = context_scorer_fn(
        market.word_raw, base_rate, corpus.recent_transcripts
    )

    # Step 3: adjusted probability
    adjusted_prob = _sigmoid(_logit(base_rate) + context_delta)
    adjusted_prob = max(0.01, min(0.99, adjusted_prob))

    # Step 4: confidence = corpus depth × recency factor (future)
    confidence = _corpus_confidence(n)

    # Count how many of the last N events mentioned the word (for thesis text)
    mention_recent = 0
    for t in corpus.recent_transcripts:
        text_lower = (t.get("raw_text") or "").lower()
        if any(v in text_lower for v in market.word_variants):
            mention_recent += 1

    # Step 5: edge
    yes_bid = market.yes_bid
    yes_ask = market.yes_ask
    # Use mid if both available, else ask (take-price), else 0.50 prior
    if yes_bid is not None and yes_ask is not None:
        mkt_price = (yes_bid + yes_ask) / 200.0
    elif yes_ask is not None:
        mkt_price = yes_ask / 100.0
    elif yes_bid is not None:
        mkt_price = yes_bid / 100.0
    else:
        mkt_price = 0.50  # illiquid — assume fair coin

    # Kalshi fee: 0.07 * P * (1-P) per contract on both buy and sell
    fee_yes = kalshi_fee_rate * mkt_price * (1 - mkt_price)
    fee_no  = kalshi_fee_rate * (1 - mkt_price) * mkt_price  # symmetric

    edge_yes = adjusted_prob - mkt_price - fee_yes
    edge_no  = (1 - adjusted_prob) - (1 - mkt_price) - fee_no
    # (edge_no simplifies to -edge_yes - fee_no + fee_yes, approximately -edge_yes)

    # Step 6: pick best side
    if edge_yes >= edge_no and edge_yes > 0:
        best_side, best_edge = "yes", edge_yes
        price = mkt_price
        prob = adjusted_prob
    elif edge_no > 0:
        best_side, best_edge = "no", edge_no
        price = 1 - mkt_price
        prob = 1 - adjusted_prob
    else:
        best_side, best_edge = "skip", max(edge_yes, edge_no)
        price, prob = mkt_price, adjusted_prob

    # Step 7: Kelly + conviction
    conviction = classify_conviction(best_edge, confidence)
    kelly_raw = _kelly_fraction(prob, price) if best_side != "skip" else 0.0

    if conviction == "SKIP":
        kelly_frac = 0.0
    else:
        thr = CONVICTION_THRESHOLDS[conviction]
        kelly_frac = min(kelly_raw * thr["kelly_frac"] * confidence, thr["max_pct"])

    recommended_usd = kelly_frac * bankroll_usd

    # Step 8: reasoning string
    reasoning_parts = [
        f"base_rate={base_rate:.1%} (k={k}/n={n})",
        f"context_delta={context_delta:+.2f}",
        f"adjusted={adjusted_prob:.1%}",
        f"confidence={confidence:.0%}",
    ]
    if yes_bid is not None or yes_ask is not None:
        reasoning_parts.append(f"mkt={mkt_price:.2f}")
    else:
        reasoning_parts.append("mkt=illiquid")
    reasoning_parts.append(f"edge={best_edge:+.1%}")
    reasoning_parts.append(f"conviction={conviction}")
    if mention_recent > 0:
        reasoning_parts.append(f"recent_mentions={mention_recent}/{len(corpus.recent_transcripts)}")

    return TermThesis(
        ticker=market.ticker,
        word_raw=market.word_raw,
        word_variants=market.word_variants,
        base_rate=base_rate,
        adjusted_prob=adjusted_prob,
        context_delta=context_delta,
        confidence=confidence,
        corpus_n=n,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        edge_yes=edge_yes,
        edge_no=edge_no,
        best_side=best_side,
        best_edge=best_edge,
        conviction=conviction,
        kelly_fraction=kelly_frac,
        recommended_usd=recommended_usd,
        reasoning=" | ".join(reasoning_parts),
        mention_count_recent=mention_recent,
    )


def build_event_thesis(
    markets:           list[MentionMarket],
    corpus:            CorpusPackage,
    base_rate_fn:      Callable,
    context_scorer_fn: Callable,
    bankroll_usd:      float,
    max_event_pct:     float = 0.30,   # cap total event exposure
) -> dict:
    """Build theses for all markets + enforce per-event position cap."""
    theses = [
        build_thesis(m, corpus, base_rate_fn, context_scorer_fn, bankroll_usd)
        for m in markets
    ]

    # Rank by best_edge descending, but drop SKIP
    ranked = sorted(
        [t for t in theses if t.conviction != "SKIP"],
        key=lambda t: -t.best_edge,
    )

    # Enforce event cap: if total recommended > max_event_pct × bankroll, scale down
    total_recommended = sum(t.recommended_usd for t in ranked)
    cap_usd = max_event_pct * bankroll_usd
    if total_recommended > cap_usd and total_recommended > 0:
        scale = cap_usd / total_recommended
        for t in ranked:
            t.kelly_fraction *= scale
            t.recommended_usd *= scale
        scaling_note = f"scaled by {scale:.2f}x to fit {max_event_pct*100:.0f}% event cap"
    else:
        scaling_note = "within cap"

    skipped = [t for t in theses if t.conviction == "SKIP"]

    return {
        "event_id":           corpus.event_id,
        "event_type":         corpus.event_type,
        "corpus_n":           corpus.corpus_count,
        "bankroll_usd":       bankroll_usd,
        "markets_scanned":    len(markets),
        "opportunities":      len(ranked),
        "skipped":            len(skipped),
        "total_recommended":  sum(t.recommended_usd for t in ranked),
        "scaling":            scaling_note,
        "conviction_counts":  {
            c: sum(1 for t in ranked if t.conviction == c)
            for c in ("HOMERUN", "HIGH", "STANDARD")
        },
        "theses":             [t.to_dict() for t in ranked],
    }
