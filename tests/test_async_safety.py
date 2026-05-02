"""C1: assert on_book_update doesn't block the event loop on slow reconcile.

Without the run_in_executor wrap, a 200ms sync reconcile blocks the loop for
the full duration. With the wrap, other coroutines can interleave.
"""
import asyncio
import time
from unittest.mock import MagicMock, patch
import pytest


@pytest.mark.asyncio
async def test_on_book_update_does_not_block_event_loop():
    """If reconcile is slow (200ms), a concurrent task should still tick fast."""
    from run_paper import PaperRunner

    # Build a runner with one fake market
    fake_markets = [{
        "market_ticker": "TEST-MKT",
        "target_size": 1000,
        "discount_factor": 0.5,
        "reward_per_day_usd": 50.0,
    }]
    runner = PaperRunner(fake_markets)

    # Stub out everything except the reconcile call we want to verify is offloaded
    runner._refresh_blacklist = MagicMock()
    runner._is_blacklisted = MagicMock(return_value=False)
    runner._quote_target_for = MagicMock(return_value=MagicMock(
        market_ticker="TEST-MKT",
        yes_bid_cents=40, no_bid_cents=58, size_contracts=25,
        yes_size_override=None, no_size_override=None,
    ))
    runner._maybe_persist_snapshot = MagicMock()
    runner.our_score_sum["TEST-MKT"] = 0
    runner.snapshots_scored["TEST-MKT"] = 0
    runner.snapshots_valid["TEST-MKT"] = 0

    # Make reconcile sleep 200ms synchronously (simulates slow Kalshi POST)
    def slow_reconcile(target):
        time.sleep(0.2)
    runner.qm.reconcile = slow_reconcile

    # Provide a minimal book
    from execution.kalshi_ws import BookState, BookLevel
    book = BookState(market_ticker="TEST-MKT")
    book.yes_bids = [BookLevel(price_cents=40, size=100)]
    book.no_bids = [BookLevel(price_cents=58, size=100)]

    # Force first run to bypass throttle
    runner.last_score_ts["TEST-MKT"] = 0

    # Run on_book_update concurrently with a tick-counter
    ticks = []

    async def ticker():
        for _ in range(20):
            await asyncio.sleep(0.01)  # 10ms intervals → 200ms total
            ticks.append(time.monotonic())

    t0 = time.monotonic()
    await asyncio.gather(
        runner.on_book_update(book),
        ticker(),
    )

    # If the loop was blocked by reconcile, ticker would have stalled and
    # the inter-tick gaps would be ≥200ms for one or more ticks.
    # With executor offload, all ticks complete in their normal ~10ms cadence.
    max_gap = max(ticks[i+1] - ticks[i] for i in range(len(ticks)-1)) if len(ticks) > 1 else 0
    # Allow some scheduler jitter (up to 50ms) but reject 200ms blocks.
    assert max_gap < 0.05, (
        f"event loop blocked {max_gap*1000:.0f}ms during reconcile — "
        f"executor wrap is missing or broken"
    )
