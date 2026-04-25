"""LIP snapshot scorer — replicates Kalshi's published Appendix A formula.

Given a book state + our own resting quotes + program parameters, computes
the score WE would earn on a single snapshot. Run once per second against
the live WebSocket feed to estimate per-second LIP accrual.

The formula, verbatim from CFTC filing Aug 2025 Appendix A (+ Feb 2026 amendment):

1. Pick qualifying cutoff on each side:
    Walk book from best price toward worse prices, accumulating size.
    Once cumulative size ≥ TargetSize, that level is the "cutoff".
    Orders AT or BETTER than the cutoff qualify; everything deeper is 0.
    If the whole book doesn't accumulate TargetSize, NO orders on that side qualify.

2. Score each qualifying bid:
    Score(bid) = DiscountFactor ^ (ReferencePrice - Price(bid)) × Size(bid)
    where ReferencePrice = best bid on that side (top of book).

3. Normalize within the snapshot (per side):
    NormalizedScore(user) = sum_user_bids_score / sum_all_bids_score
    Each side independently normalizes to 1.0.

4. Two-sided requirement (Feb 28, 2026 amendment):
    If EITHER side fails TargetSize, THE SNAPSHOT IS EXCLUDED ENTIRELY —
    no one earns anything. We can only score if both yes-side AND no-side
    hit TargetSize.

5. User's total snapshot score:
    SnapshotScore = yes_normalized + no_normalized  (max 2.0 if alone on both sides)

6. Time-period aggregation:
    TimePeriodScore(user) = sum(SnapshotScore over all snapshots)
                            / sum_all_users_TotalSnapshotScore
    Payout(user) = TimePeriodScore × TimePeriodReward

NOTE on asks: Kalshi scoring works on BID LIQUIDITY. For the YES side, the
"bids" are yes_bids (people buying YES). For the NO side, the "bids" are
no_bids (people buying NO). NOT yes_asks/no_asks — those are derivative
views of the opposite side.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from execution.kalshi_ws import BookLevel, BookState


@dataclass
class ProgramParams:
    """The LIP parameters for one market (from /incentive_programs)."""
    market_ticker:   str
    target_size:     int      # contracts
    discount_factor: float    # 0.0–1.0
    period_reward_usd: float   # total USD for the whole Time Period


@dataclass
class OurQuotes:
    """Our resting orders on one market."""
    yes_bids: list[BookLevel] = None  # our buy-YES resting limits
    no_bids:  list[BookLevel] = None  # our buy-NO resting limits

    def __post_init__(self):
        if self.yes_bids is None: self.yes_bids = []
        if self.no_bids is None:  self.no_bids = []


@dataclass
class SnapshotScore:
    """Output of scoring one snapshot."""
    market_ticker:      str
    snapshot_valid:     bool
    yes_qualified:      bool
    no_qualified:       bool
    our_yes_normalized: float   # 0.0–1.0
    our_no_normalized:  float   # 0.0–1.0
    our_total_score:    float   # sum (max 2.0)
    # Diagnostic:
    yes_cutoff_price:   Optional[int] = None
    no_cutoff_price:    Optional[int] = None
    yes_total_qualifying_score: float = 0.0
    no_total_qualifying_score:  float = 0.0


def _find_cutoff_price(bids: list[BookLevel], target_size: int) -> Optional[int]:
    """Walk bids from best down until cumulative size ≥ target_size.
    Returns the price_cents of the cutoff level, or None if book never reaches it.
    Bids must be sorted DESC by price."""
    cum = 0
    for lvl in bids:
        cum += lvl.size
        if cum >= target_size:
            return lvl.price_cents
    return None


def _score_bids(
    all_bids: list[BookLevel],
    our_bids: list[BookLevel],
    reference_price: int,
    discount_factor: float,
    cutoff_price: int,
) -> tuple[float, float]:
    """Return (our_score, total_score) for one side.

    Scoring rule: for each bid level at price ≥ cutoff_price,
      level_score = DiscountFactor^(ReferencePrice - Price) × Size
    Sum all levels to get total_score. Our levels separately to get our_score.
    """
    def level_score(price: int, size: int) -> float:
        distance_ticks = reference_price - price  # non-negative since price ≤ reference
        return (discount_factor ** distance_ticks) * size

    total = sum(level_score(l.price_cents, l.size) for l in all_bids if l.price_cents >= cutoff_price)
    ours  = sum(level_score(l.price_cents, l.size) for l in our_bids if l.price_cents >= cutoff_price)
    return ours, total


def score_snapshot(
    book: BookState,
    ours: OurQuotes,
    params: ProgramParams,
) -> SnapshotScore:
    """Score a single snapshot.

    Returns SnapshotScore with our normalized shares per side + total.
    If the snapshot is invalid (either side fails TargetSize), our_total_score = 0.
    """
    target = params.target_size
    df = params.discount_factor

    # Qualifying cutoff per side
    yes_cutoff = _find_cutoff_price(book.yes_bids, target)
    no_cutoff  = _find_cutoff_price(book.no_bids,  target)
    yes_qualified = yes_cutoff is not None
    no_qualified  = no_cutoff  is not None

    snapshot_valid = yes_qualified and no_qualified

    result = SnapshotScore(
        market_ticker=book.market_ticker,
        snapshot_valid=snapshot_valid,
        yes_qualified=yes_qualified,
        no_qualified=no_qualified,
        our_yes_normalized=0.0,
        our_no_normalized=0.0,
        our_total_score=0.0,
        yes_cutoff_price=yes_cutoff,
        no_cutoff_price=no_cutoff,
    )

    if not snapshot_valid:
        return result  # two-sided requirement failed

    # Score yes-side
    ref_yes = book.yes_bids[0].price_cents if book.yes_bids else 0
    our_yes, total_yes = _score_bids(book.yes_bids, ours.yes_bids, ref_yes, df, yes_cutoff)
    if total_yes > 0:
        result.our_yes_normalized = our_yes / total_yes
    result.yes_total_qualifying_score = total_yes

    # Score no-side
    ref_no = book.no_bids[0].price_cents if book.no_bids else 0
    our_no, total_no = _score_bids(book.no_bids, ours.no_bids, ref_no, df, no_cutoff)
    if total_no > 0:
        result.our_no_normalized = our_no / total_no
    result.no_total_qualifying_score = total_no

    result.our_total_score = result.our_yes_normalized + result.our_no_normalized
    return result


def estimated_period_payout(
    sum_our_snapshot_scores: float,
    total_snapshots_in_period: int,
    period_reward_usd: float,
) -> float:
    """Estimate our payout given aggregated snapshot scores.

    Payout(user) = TimePeriodScore(user) × TimePeriodReward
    TimePeriodScore(user) = sum_user_scores / sum_all_users_scores

    We don't know sum_all_users_scores in real time. A conservative
    approximation: assume each snapshot's total_score is ~1.0 per side
    (i.e., one market-maker fully dominates) or ~2.0 (both sides).
    Pessimistic estimate: divide by 2 × total_snapshots.
    """
    if total_snapshots_in_period <= 0:
        return 0.0
    # Pessimistic: assume total_score per snapshot = 2.0 (fully-saturated book)
    est_time_period_score = sum_our_snapshot_scores / (2.0 * total_snapshots_in_period)
    return est_time_period_score * period_reward_usd


# ── Self-tests ────────────────────────────────────────────────────────────

def _self_test():
    """Validate scorer against hand-calculated examples from the CFTC filing."""
    # Example from fiftycentdollars Substack:
    #   TargetSize = 1000, DiscountFactor = 0.9, period_reward = $100
    #   3 quoters: A has 500 at best (say 60¢), B has 300 one tick back (59¢),
    #              C has 400 two ticks back (58¢)
    #   Ref price = 60. Walk book: 500 (cum=500) + 300 (cum=800) + 400 (cum=1200) → cutoff at 58.
    #   Scores:
    #     A: 0.9^(60-60) × 500 = 1.0 × 500 = 500
    #     B: 0.9^(60-59) × 300 = 0.9 × 300 = 270
    #     C: 0.9^(60-58) × 400 = 0.81 × 400 = 324
    #   Total: 1094
    #   A's normalized: 500/1094 = 0.457 ≈ 45.7% of reward = $45.70 on YES side
    # (Two-sided requirement: assume NO side also fully quoted; skip this test detail.)
    from execution.kalshi_ws import BookLevel, BookState

    book = BookState(market_ticker="TEST")
    book.yes_bids = [
        BookLevel(price_cents=60, size=500),  # A
        BookLevel(price_cents=59, size=300),  # B
        BookLevel(price_cents=58, size=400),  # C
    ]
    # Make NO side also fully-meeting target so snapshot_valid=True
    book.no_bids = [
        BookLevel(price_cents=40, size=500),
        BookLevel(price_cents=39, size=500),
    ]

    # A's quotes
    ours = OurQuotes(yes_bids=[BookLevel(price_cents=60, size=500)])
    params = ProgramParams(market_ticker="TEST", target_size=1000,
                            discount_factor=0.9, period_reward_usd=100.0)

    r = score_snapshot(book, ours, params)
    assert r.snapshot_valid, "snapshot should be valid (both sides meet target)"
    assert r.yes_cutoff_price == 58, f"expected cutoff 58, got {r.yes_cutoff_price}"

    expected_a_share = 500 / (500 + 270 + 324)  # ≈ 0.457
    actual = r.our_yes_normalized
    assert abs(actual - expected_a_share) < 0.001, f"A's share: expected {expected_a_share:.3f}, got {actual:.3f}"
    print(f"  A (500 at best): yes_normalized = {actual:.4f} ≈ ${actual * 100:.2f} (expected ~$45.70)")

    # B's quotes
    ours = OurQuotes(yes_bids=[BookLevel(price_cents=59, size=300)])
    r = score_snapshot(book, ours, params)
    expected_b_share = 270 / (500 + 270 + 324)  # ≈ 0.247
    assert abs(r.our_yes_normalized - expected_b_share) < 0.001
    print(f"  B (300 @ 1-tick back): yes_normalized = {r.our_yes_normalized:.4f} ≈ ${r.our_yes_normalized * 100:.2f} (expected ~$24.68)")

    # Two-sided requirement test
    book_one_sided = BookState(market_ticker="TEST2")
    book_one_sided.yes_bids = book.yes_bids
    book_one_sided.no_bids = [BookLevel(price_cents=40, size=100)]  # below target
    r = score_snapshot(book_one_sided, OurQuotes(yes_bids=[BookLevel(60, 500)]), params)
    assert not r.snapshot_valid, "should be invalid when NO side fails target"
    assert r.our_total_score == 0.0
    print(f"  One-sided test: correctly excluded (total_score = {r.our_total_score})")

    print("self-test PASSED")


if __name__ == "__main__":
    _self_test()
