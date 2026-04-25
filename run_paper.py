"""LIP Maker — end-to-end paper runner.

Ties everything together:
  - LIP discovery (refresh active programs)
  - Top-N market selection
  - WebSocket orderbook subscription
  - Per-second scoring simulation with our intended quotes
  - Quote manager reconciliation (paper mode — logs intent only)
  - Periodic summary with estimated $/day

Quote strategy (MVP): JOIN the best bid on each side.
  yes_bid_cents = current best yes bid
  no_bid_cents  = current best no bid
  size          = min(QUOTE_SIZE_AS_FRACTION_OF_TARGET × target_size, DEFAULT_QUOTE_SIZE_CONTRACTS)

This is the simplest LIP-qualifying strategy. Adverse-selection risk exists
(informed flow hits our quotes) but we measure it via the paper week.

Run in background for 7 days to collect data before flipping to live.

Usage:
    PYTHONPATH=. venv/bin/python run_paper.py --duration 604800  # 7 days
    PYTHONPATH=. venv/bin/python run_paper.py --duration 300     # 5-min smoke test
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings
from engine.lip_discovery import discover, top_n_to_quote
from engine.lip_scorer import (
    OurQuotes, ProgramParams, score_snapshot,
)
from engine.adaptive_sizer import AdaptiveSizer
from execution.kalshi_ws import KalshiWS, BookState, BookLevel
from execution.quote_manager import QuoteManager, QuoteTarget

_log = logging.getLogger("lip_maker")


class PaperRunner:
    def __init__(self, markets: list[dict]):
        self.markets = markets
        self.params_by_ticker = {
            m["market_ticker"]: ProgramParams(
                market_ticker=m["market_ticker"],
                target_size=int(m["target_size"]),
                discount_factor=float(m["discount_factor"]),
                period_reward_usd=float(m["reward_per_day_usd"]),
            )
            for m in markets
        }
        # 2026-04-21: paper flag now controlled by LIP_PAPER env via settings.
        # When settings.PAPER_MODE is False, QuoteManager hits real Kalshi.
        self.qm = QuoteManager(paper=settings.PAPER_MODE)
        # 2026-04-22: target_share 0.25→0.35 — toxicity filter V2 provides
        # adverse-selection backstop; higher target = more rebate on winners.
        self.sizer = AdaptiveSizer(target_share=0.35)
        # Stats
        self.snapshots_scored = defaultdict(int)
        self.snapshots_valid = defaultdict(int)
        self.our_score_sum   = defaultdict(float)   # sum of our snapshot scores (for payout est)
        self.last_score_ts   = defaultdict(float)
        self.reconciles      = defaultdict(int)
        self.start_time      = time.time()
        self._last_persist_key: dict[str, int] = {}
        self._snapshot_persist_failures: int = 0  # Architect audit: track silent drops
        # Futures fair-value cache (Quant audit): {prefix: (price, fetched_ts)}
        # Refreshed every 60s to match futures-feed.timer cadence.
        self._futures_cache: dict[str, tuple[float, float]] = {}
        self._futures_cache_ts: float = 0.0
        self._fv_skip_log_ts: dict[str, float] = {}  # per-ticker log throttle
        # Blacklist cache — refreshed from market_blacklist on each call
        # older than BLACKLIST_CACHE_SEC. Prevents DB hits on every book update.
        self._blacklist: set[str] = set()
        self._blacklist_ts: float = 0.0
        self._blacklist_last_action: dict[str, float] = {}   # ticker → last-cancel ts

    BLACKLIST_CACHE_SEC = 10  # refresh cache every 10s

    def _refresh_blacklist(self) -> None:
        """Pull active blacklist from DB. Called lazily, cached."""
        now = time.time()
        if now - self._blacklist_ts < self.BLACKLIST_CACHE_SEC:
            return
        self._blacklist_ts = now
        try:
            conn = sqlite3.connect(settings.DB_PATH)
            try:
                rows = conn.execute(
                    "SELECT ticker FROM market_blacklist WHERE datetime(expires_at) > datetime('now')"
                ).fetchall()
                self._blacklist = {r[0] for r in rows}
            finally:
                conn.close()
        except Exception as e:
            _log.debug(f"blacklist refresh failed: {e}")

    def _is_blacklisted(self, ticker: str) -> bool:
        """Check cache; caller is responsible for calling _refresh_blacklist periodically."""
        return ticker in self._blacklist

    def _handle_blacklisted(self, ticker: str) -> None:
        """Cancel resting orders + log. Throttled to avoid spamming cancel_all."""
        now = time.time()
        last = self._blacklist_last_action.get(ticker, 0)
        if now - last < 30:  # don't re-cancel within 30s
            return
        self._blacklist_last_action[ticker] = now
        self.qm.cancel_all(market_ticker=ticker)
        _log.info(f"blacklist: cancelled quotes for {ticker}")

    def _actually_resting(self, ticker: str, target) -> bool:
        """Check if we have qualifying two-sided quotes resting on this market.

        2026-04-25 (Phantom-snapshot fix v2). Permissive: just checks
        existence of two-sided resting orders at min-quote size. Doesn't
        require exact price match because books move constantly between
        target computation and actual placement — strict price-match
        rejected too many legitimate resting cases.

        Phase 2 (post-Apr-28): use ACTUAL resting prices/sizes in scorer
        instead of target, for true precision.
        """
        orders = self.qm.resting.get(ticker, [])
        yes = next((o for o in orders if o.side == "yes"), None)
        no  = next((o for o in orders if o.side == "no"),  None)
        if not (yes and no):
            return False
        if yes.size_contracts < settings.MIN_QUOTE_SIZE_CONTRACTS:
            return False
        if no.size_contracts < settings.MIN_QUOTE_SIZE_CONTRACTS:
            return False
        return True

    FUTURES_CACHE_SEC = 60   # match futures-feed.timer cadence

    def _refresh_futures_cache(self) -> None:
        """Pull latest futures prices from DB. 60s cache."""
        now = time.time()
        if now - self._futures_cache_ts < self.FUTURES_CACHE_SEC:
            return
        self._futures_cache_ts = now
        try:
            conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
            try:
                rows = conn.execute(
                    """SELECT kalshi_prefix, price FROM futures_prices
                       WHERE id IN (SELECT MAX(id) FROM futures_prices GROUP BY kalshi_prefix)"""
                ).fetchall()
                for prefix, price in rows:
                    self._futures_cache[prefix] = (float(price), now)
            finally:
                conn.close()
        except Exception as e:
            _log.warning(f"futures cache refresh failed: {e}")

    def _fair_value_skip(self, ticker: str, yes_bid: int, no_bid: int) -> str | None:
        """Quant audit: skip markets where futures fair-value strongly disagrees
        with our quote price on the LOSING side. Adverse-selection protection.

        Logic: if futures clearly indicates YES will settle (futures > strike by
        >3% of strike), our NO bid is dangerous — informed traders hit our NO at
        high prices knowing it settles worthless. Skip if no_bid > 30c. Mirror
        for NO direction. Only fires for "exact" or "close" confidence prefixes.
        """
        try:
            from engine.futures_feed import FUTURES_MAP
        except Exception:
            return None
        prefix = next((p for p in FUTURES_MAP if ticker.startswith(p + "-")), None)
        if not prefix:
            return None
        confidence = FUTURES_MAP[prefix].get("confidence", "unknown")
        if confidence not in ("exact", "close"):
            return None
        import re
        m = re.search(r"-T([\d.]+)$", ticker)
        if not m:
            return None
        strike = float(m.group(1))
        self._refresh_futures_cache()
        cached = self._futures_cache.get(prefix)
        if cached is None:
            return None
        futures_price = cached[0]
        threshold = max(0.03 * abs(strike), 1.0)
        delta = futures_price - strike
        if delta > threshold and no_bid > 30:
            return (f"futures_skip[{prefix}] futures={futures_price:.2f} "
                    f">strike={strike} (Δ={delta:+.2f}) NO worthless, "
                    f"no_bid={no_bid}c too high")
        if delta < -threshold and yes_bid > 30:
            return (f"futures_skip[{prefix}] futures={futures_price:.2f} "
                    f"<strike={strike} (Δ={delta:+.2f}) YES worthless, "
                    f"yes_bid={yes_bid}c too high")
        return None

    def _quote_target_for(self, book: BookState) -> QuoteTarget | None:
        """Compute our desired quote using ADAPTIVE sizing to target 25% share."""
        p = self.params_by_ticker.get(book.market_ticker)
        if p is None:
            return None
        best_yes = book.best_yes_bid()
        best_no  = book.best_no_bid()
        if best_yes is None or best_no is None:
            return None

        # Quant audit: futures fair-value adverse-selection gate. Skip
        # entirely if futures clearly disagrees with our quote on the
        # losing side — informed flow will pick us off otherwise.
        skip_reason = self._fair_value_skip(
            book.market_ticker, best_yes.price_cents, best_no.price_cents
        )
        if skip_reason:
            # Throttle: log first skip per ticker, then silent for 5 min
            now_ts = time.time()
            last = self._fv_skip_log_ts.get(book.market_ticker, 0)
            if now_ts - last > 300:
                _log.info(skip_reason)
                self._fv_skip_log_ts[book.market_ticker] = now_ts
            return None

        # Adaptive size: target 25% of qualifying score per side.
        # Use the MIN of yes-side and no-side sizes so our two-sided quote
        # is balanced (prevents inventory skew from the start).
        size_yes = self.sizer.size_for(book.market_ticker, "yes", p.target_size)
        size_no  = self.sizer.size_for(book.market_ticker, "no",  p.target_size)
        size = min(size_yes, size_no)

        return QuoteTarget(
            market_ticker=book.market_ticker,
            yes_bid_cents=best_yes.price_cents,
            no_bid_cents=best_no.price_cents,
            size_contracts=size,
        )

    async def on_book_update(self, book: BookState):
        """Called by WS on every book change."""
        now = time.time()
        # Throttle scoring to ~1/sec per market
        if now - self.last_score_ts[book.market_ticker] < 1.0:
            return
        self.last_score_ts[book.market_ticker] = now

        # Blacklist gate — check before doing any work. If blacklisted (macro
        # blackout, pre-live audit, etc.) cancel existing quotes + skip reprice.
        self._refresh_blacklist()
        if self._is_blacklisted(book.market_ticker):
            self._handle_blacklisted(book.market_ticker)
            return

        params = self.params_by_ticker.get(book.market_ticker)
        if params is None:
            return

        # Compute our target quote
        target = self._quote_target_for(book)
        if target is None:
            return

        # Simulate: what would we score if we had these quotes resting?
        ours = OurQuotes(
            yes_bids=[BookLevel(price_cents=target.yes_bid_cents, size=target.size_contracts)],
            no_bids=[BookLevel(price_cents=target.no_bid_cents,  size=target.size_contracts)],
        )
        # IMPORTANT: to score our contribution we need to ADD our quotes to the
        # book before scoring (otherwise Kalshi sees us as part of the book).
        # In paper mode we're not on the book yet, so construct a book+us view.
        augmented = BookState(market_ticker=book.market_ticker)
        # Deep-copy the level lists so we don't mutate the source
        augmented.yes_bids = [BookLevel(l.price_cents, l.size) for l in book.yes_bids]
        augmented.no_bids  = [BookLevel(l.price_cents, l.size) for l in book.no_bids]
        # Add our simulated resting size at the target price
        for lvl in augmented.yes_bids:
            if lvl.price_cents == target.yes_bid_cents:
                lvl.size += target.size_contracts
                break
        else:
            augmented.yes_bids.insert(0, BookLevel(target.yes_bid_cents, target.size_contracts))
        augmented.yes_bids.sort(key=lambda l: -l.price_cents)
        for lvl in augmented.no_bids:
            if lvl.price_cents == target.no_bid_cents:
                lvl.size += target.size_contracts
                break
        else:
            augmented.no_bids.insert(0, BookLevel(target.no_bid_cents, target.size_contracts))
        augmented.no_bids.sort(key=lambda l: -l.price_cents)

        r = score_snapshot(augmented, ours, params)
        self.snapshots_scored[book.market_ticker] += 1

        # 2026-04-25 PHANTOM-SNAPSHOT FIX: check if we ACTUALLY have qualifying
        # quotes resting. If not, the simulated score is fantasy — persist the
        # snapshot honestly (our_score=0, snapshot_valid=0) and DON'T feed sizer.
        is_resting = self._actually_resting(book.market_ticker, target)
        if is_resting:
            persist_our_score = r.our_total_score
            persist_yes_qual  = 1 if r.yes_qualified else 0
            persist_no_qual   = 1 if r.no_qualified  else 0
            persist_valid     = 1 if r.snapshot_valid else 0
        else:
            # Phantom — record the book state but mark our contribution as zero
            persist_our_score = 0.0
            persist_yes_qual  = 0
            persist_no_qual   = 0
            persist_valid     = 0

        # Persist every snapshot to DB — throttle to 1/5s per market to cap volume.
        now_ts_key = int(now / 5)
        last_key = self._last_persist_key.get(book.market_ticker, -1)
        if now_ts_key != last_key:
            self._last_persist_key[book.market_ticker] = now_ts_key
            try:
                import sqlite3
                conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
                try:
                    conn.execute(
                        """INSERT INTO lip_snapshots
                           (market_ticker, captured_at, our_score, total_score,
                            yes_qualified, no_qualified, snapshot_valid,
                            estimated_payout_usd, was_resting)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (book.market_ticker,
                         datetime.now(timezone.utc).isoformat(),
                         persist_our_score,
                         r.yes_total_qualifying_score + r.no_total_qualifying_score,
                         persist_yes_qual,
                         persist_no_qual,
                         persist_valid,
                         (persist_our_score / 2.0) * params.period_reward_usd
                          / max(1, 86400),
                         1 if is_resting else 0),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                self._snapshot_persist_failures += 1
                _log.warning(f"snapshot persist failed for {book.market_ticker}: {e} "
                             f"(total failures this session: {self._snapshot_persist_failures})")

        # Sizer feedback — ONLY when actually resting (was learning from phantom).
        if is_resting and r.snapshot_valid:
            self.snapshots_valid[book.market_ticker] += 1
            self.our_score_sum[book.market_ticker] += r.our_total_score
            our_yes_score = r.our_yes_normalized * r.yes_total_qualifying_score \
                            if r.yes_total_qualifying_score else 0
            our_no_score  = r.our_no_normalized  * r.no_total_qualifying_score  \
                            if r.no_total_qualifying_score  else 0
            self.sizer.observe(
                book.market_ticker,
                yes_total_qual=r.yes_total_qualifying_score,
                no_total_qual=r.no_total_qualifying_score,
                our_yes_contribution=our_yes_score,
                our_no_contribution=our_no_score,
                ts=now,
            )

        # Reconcile paper quotes
        self.qm.reconcile(target)
        self.reconciles[book.market_ticker] += 1

    async def heartbeat_snapshot_loop(self, ws, interval_sec: int = 30):
        """Periodically snapshot all quoted markets even if book hasn't updated.
        This ensures lip_snapshots is populated across ALL active markets, not
        just the noisy ones. Critical for honest share-estimation across the
        full paper portfolio.
        """
        import sqlite3
        from engine.lip_scorer import OurQuotes, score_snapshot
        while True:
            try:
                await asyncio.sleep(interval_sec)
                now = time.time()
                # Refresh blacklist once per heartbeat cycle
                self._refresh_blacklist()
                for tkr, params in self.params_by_ticker.items():
                    if self._is_blacklisted(tkr):
                        self._handle_blacklisted(tkr)
                        continue
                    book = ws.books.get(tkr)
                    if book is None:
                        continue
                    best_yes = book.best_yes_bid()
                    best_no = book.best_no_bid()
                    if best_yes is None or best_no is None:
                        continue
                    # Construct augmented book with our hypothetical quote
                    size_yes = self.sizer.size_for(tkr, "yes", params.target_size)
                    size_no  = self.sizer.size_for(tkr, "no",  params.target_size)
                    size = min(size_yes, size_no)
                    ours = OurQuotes(
                        yes_bids=[BookLevel(price_cents=best_yes.price_cents, size=size)],
                        no_bids=[BookLevel(price_cents=best_no.price_cents,  size=size)],
                    )
                    augmented = BookState(market_ticker=tkr)
                    augmented.yes_bids = [BookLevel(l.price_cents, l.size) for l in book.yes_bids]
                    augmented.no_bids  = [BookLevel(l.price_cents, l.size) for l in book.no_bids]
                    # Fold our size in
                    for lvl in augmented.yes_bids:
                        if lvl.price_cents == best_yes.price_cents:
                            lvl.size += size; break
                    else:
                        augmented.yes_bids.insert(0, BookLevel(best_yes.price_cents, size))
                    for lvl in augmented.no_bids:
                        if lvl.price_cents == best_no.price_cents:
                            lvl.size += size; break
                    else:
                        augmented.no_bids.insert(0, BookLevel(best_no.price_cents, size))
                    augmented.yes_bids.sort(key=lambda l: -l.price_cents)
                    augmented.no_bids.sort(key=lambda l: -l.price_cents)

                    r = score_snapshot(augmented, ours, params)
                    self.snapshots_scored[tkr] += 1

                    # 2026-04-25 PHANTOM-SNAPSHOT FIX: heartbeat scoring needs
                    # the same gate. Synthesize a target-equivalent for the check.
                    class _T:  # lightweight target stub for _actually_resting
                        yes_bid_cents = best_yes.price_cents
                        no_bid_cents  = best_no.price_cents
                        size_contracts = size
                    is_resting = self._actually_resting(tkr, _T())
                    if is_resting:
                        persist_our_score = r.our_total_score
                        persist_yes_qual = 1 if r.yes_qualified else 0
                        persist_no_qual  = 1 if r.no_qualified  else 0
                        persist_valid    = 1 if r.snapshot_valid else 0
                    else:
                        persist_our_score = 0.0
                        persist_yes_qual = 0
                        persist_no_qual  = 0
                        persist_valid    = 0

                    if is_resting and r.snapshot_valid:
                        self.snapshots_valid[tkr] += 1
                        self.our_score_sum[tkr] += r.our_total_score

                    try:
                        conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
                        try:
                            conn.execute(
                                """INSERT INTO lip_snapshots
                                   (market_ticker, captured_at, our_score, total_score,
                                    yes_qualified, no_qualified, snapshot_valid,
                                    estimated_payout_usd, was_resting)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (tkr, datetime.now(timezone.utc).isoformat(),
                                 persist_our_score,
                                 r.yes_total_qualifying_score + r.no_total_qualifying_score,
                                 persist_yes_qual,
                                 persist_no_qual,
                                 persist_valid,
                                 (persist_our_score / 2.0) * params.period_reward_usd
                                    / max(1, 86400),
                                 1 if is_resting else 0),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    except Exception as e:
                        _log.debug(f"heartbeat persist failed {tkr}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.warning(f"heartbeat loop error: {e}")

    def print_summary(self):
        elapsed = time.time() - self.start_time
        print(f"\n=== LIP Paper Runner summary ({elapsed:.0f}s elapsed) ===")
        print(f"{'market':40s} {'snaps':>5s} {'valid':>5s} {'our_share':>9s} {'est$/day':>10s}")
        total_est_day = 0.0
        for m in self.markets:
            tkr = m["market_ticker"]
            snaps = self.snapshots_scored.get(tkr, 0)
            valid = self.snapshots_valid.get(tkr, 0)
            our_sum = self.our_score_sum.get(tkr, 0)
            avg_share = (our_sum / valid) if valid else 0.0  # per-snapshot share (0-2.0)
            # Normalize: max share per snapshot is 2.0 (both sides dominated)
            share_pct = avg_share / 2.0 if avg_share else 0
            est_day = share_pct * m["reward_per_day_usd"] * (valid / max(snaps, 1))
            total_est_day += est_day
            print(f"  {tkr[:38]:38s} {snaps:>5d} {valid:>5d} {share_pct*100:>7.1f}% ${est_day:>8.2f}")
        print(f"\n  Estimated total: ${total_est_day:.2f}/day = ~${total_est_day*30:.0f}/month")
        print(f"  Quote manager: {self.qm.summary()}")


async def main(duration_sec: int = 300, top_n: int = 50):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(settings.LOG_PATH),
            logging.StreamHandler(),
        ],
    )

    # Refresh programs
    _log.info("refreshing LIP programs...")
    discover(save=True)
    markets = top_n_to_quote(top_n)
    if not markets:
        _log.warning("no enrolled markets")
        return
    _log.info(f"quoting top-{len(markets)} markets, total pool ${sum(m['reward_per_day_usd'] for m in markets):.2f}/day")

    runner = PaperRunner(markets)
    ws = KalshiWS()
    await ws.connect()
    ws.on_update(runner.on_book_update)

    # 2026-04-22 (Architect audit): purge stale resting state on reconnect.
    # Without this, post-disconnect resting orders are stale and reconcile()
    # skips placement → silent dark periods on affected markets.
    async def _on_ws_reconnect(tickers: list[str]) -> None:
        for t in tickers:
            runner.qm.reset_for_market(t)
        _log.warning(f"WS reconnect: purged resting state for {len(tickers)} tickers")
    ws.on_reconnect(_on_ws_reconnect)

    await ws.subscribe_orderbook([m["market_ticker"] for m in markets])

    # Run with periodic summaries
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except NotImplementedError:
        pass

    ws_task = asyncio.create_task(ws.run())
    # Heartbeat snapshot loop — ensures ALL quoted markets get regular data
    # regardless of book update frequency. Critical for go-live gate validation.
    hb_task = asyncio.create_task(runner.heartbeat_snapshot_loop(ws, interval_sec=30))

    # 2026-04-22: Periodic discover + resubscribe so new daily markets (Kalshi
    # rotates tickers at 21:00 UTC for daily commodities) auto-enroll mid-run.
    # Prior behavior: discover() only at startup → 8+ hrs of unquoted new
    # markets per day. Fix = ~30-min refresh.
    async def _periodic_discover(interval_sec: int = 1800):
        """Refresh LIP programs + subscribe newly enrolled markets."""
        await asyncio.sleep(interval_sec)  # initial wait; startup already did it
        while not stop.is_set():
            try:
                discover(save=True)
                fresh = top_n_to_quote(top_n)
                current_tickers = set(runner.params_by_ticker.keys())
                fresh_tickers = {m["market_ticker"] for m in fresh}
                new_tickers = fresh_tickers - current_tickers
                if new_tickers:
                    _log.info(f"periodic_discover: {len(new_tickers)} new markets to subscribe")
                    # Update runner state
                    for m in fresh:
                        tkr = m["market_ticker"]
                        if tkr in new_tickers:
                            runner.params_by_ticker[tkr] = ProgramParams(
                                market_ticker=tkr,
                                target_size=int(m["target_size"]),
                                discount_factor=float(m["discount_factor"]),
                                period_reward_usd=float(m["reward_per_day_usd"]),
                            )
                            runner.markets.append(m)
                    # Subscribe new markets via existing WS
                    await ws.subscribe_orderbook(list(new_tickers))
                else:
                    _log.debug("periodic_discover: no new markets")
            except Exception as e:
                _log.warning(f"periodic_discover error: {e}")
            await asyncio.sleep(interval_sec)

    discover_task = asyncio.create_task(_periodic_discover(interval_sec=1800))

    deadline = time.time() + duration_sec
    try:
        while time.time() < deadline and not stop.is_set():
            await asyncio.sleep(60)
            runner.print_summary()
    finally:
        _log.info("shutting down — cancelling all paper quotes")
        runner.qm.cancel_all()
        hb_task.cancel()
        discover_task.cancel()
        await ws.close()
        try:
            await asyncio.wait_for(ws_task, timeout=3)
            await asyncio.wait_for(hb_task, timeout=3)
            await asyncio.wait_for(discover_task, timeout=3)
        except Exception:
            pass

    runner.print_summary()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=300,
                   help="seconds to run (default: 300 = 5 min smoke test)")
    p.add_argument("--top-n", type=int, default=100,
                   help="number of top REACHABLE markets to quote (target_size ≤ 500 filter; ~$2,900/day pool at top-100)")
    a = p.parse_args()
    asyncio.run(main(duration_sec=a.duration, top_n=a.top_n))
