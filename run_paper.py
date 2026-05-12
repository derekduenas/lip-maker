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
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings
from engine.lip_discovery import discover, top_n_to_quote
# from engine.sniper_select import top_n_by_ev  # archived 2026-04-29 (audit: unused)
# 2026-05-03 GOLDEN-FUNNEL: capital-aware ranker. Replaces fixed top-N with
# greedy yield-per-dollar fill. N becomes OUTPUT not INPUT — adapts to
# opportunity quality + budget. Toggle via LIP_USE_CAPITAL_ALLOC env (default true).
from engine.capital_allocator import select_optimal_portfolio
from engine.depth_probe import filter_by_depth
from engine.lip_scorer import (
    OurQuotes, ProgramParams, score_snapshot,
)
from engine.adaptive_sizer import AdaptiveSizer
from execution.kalshi_ws import KalshiWS, BookState, BookLevel
from execution.quote_manager import QuoteManager, QuoteTarget



def _hydra_skew_for(ticker: str) -> tuple:
    """HYDRA-SKEW (2026-05-06): proactive directional bias from LLM swarm.
    Reads market_skew_hint table populated by tools/hydra_skew_predictor.py.
    Returns (skew_yes_float, confidence_float) or (None, None) if no fresh hint."""
    try:
        conn = sqlite3.connect(settings.DB_PATH, timeout=5)
        now = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            "SELECT skew_yes, confidence FROM market_skew_hint "
            "WHERE ticker=? AND expires_at > ?",
            (ticker, now),
        ).fetchone()
        conn.close()
        if not row: return (None, None)
        return (float(row[0]), float(row[1]))
    except Exception:
        return (None, None)

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
        # 2026-05-03 GOLDEN-FUNNEL: per-market size FLOOR from capital_allocator.
        # Ensures we always quote enough to cross the qualify cliff. Sizer can
        # size larger if observed competition demands; never smaller than this.
        self.optimal_size_floors: dict[str, int] = {
            m["market_ticker"]: int(m.get("optimal_size_per_side", 0) or 0)
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
        # 2026-05-02 PREDATOR C1: per-market reconcile lock so concurrent book
        # updates for the same ticker can't issue overlapping API calls
        # (which would cause double-cancel/double-place races). Created on
        # first use to avoid pre-allocating for markets we never touch.
        self._reconcile_locks: dict[str, asyncio.Lock] = {}
        self._futures_cache_ts: float = 0.0
        self._fv_skip_log_ts: dict[str, float] = {}  # per-ticker log throttle
        # Blacklist cache — refreshed from market_blacklist on each call
        # older than BLACKLIST_CACHE_SEC. Prevents DB hits on every book update.
        self._blacklist: set[str] = set()
        self._blacklist_ts: float = 0.0
        # #98 (2026-04-28) Tick backoff: per-ticker rolling history of best
        # bid changes. When best moves > VOLATILITY_BACKOFF_TICKS in
        # VOLATILITY_WINDOW_SEC, we're in a hot market — back off.
        from collections import deque
        self._best_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
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

    def _minutes_until_settle(self, ticker: str) -> float | None:
        """#104 — parse close time from ticker name. Returns minutes until
        settle, or None if can't parse. Used by pre-settlement closing.

        Format examples:
          KXBRENTD-26APR2817-T103     → Apr 28 2026 17:00 ET → 21:00 UTC
          KXCORNW-26APR2417-T440      → Apr 24 2026 17:00 ET → 21:00 UTC
          KXTRUMPACT-26APR26-T3       → Apr 26 2026 (no hour, treat as 23:59)

        AUDIT FIXES 2026-04-28:
          - Use zoneinfo America/New_York for proper DST handling
            (was hardcoded +4 EDT — breaks Nov 2 2026 when EST starts)
          - Use timedelta for hour-add to handle hh ≥ 20 (was raising
            ValueError on int(hh)+4 ≥ 24, silently returning None for
            evening crypto markets like 20:00 ET close)
        """
        import re
        from datetime import timedelta
        try:
            from zoneinfo import ZoneInfo
            ET = ZoneInfo("America/New_York")
        except Exception:
            ET = None
        m = re.match(r"^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})?", ticker)
        if not m:
            return None
        yy, mmm, dd, hh = m.groups()
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        month = months.get(mmm.upper())
        if not month:
            return None
        try:
            if hh:
                if ET is not None:
                    # Construct ET-local time, convert to UTC (DST-aware)
                    et_close = datetime(2000 + int(yy), month, int(dd),
                                        int(hh), 0, tzinfo=ET)
                    close_utc = et_close.astimezone(timezone.utc)
                else:
                    # Fallback if zoneinfo unavailable — use timedelta
                    # to avoid the +4 overflow bug for hh ≥ 20.
                    base = datetime(2000 + int(yy), month, int(dd),
                                    0, 0, tzinfo=timezone.utc)
                    close_utc = base + timedelta(hours=int(hh) + 4)
            else:
                # No hour suffix — treat as end of named day (23:59 UTC)
                close_utc = datetime(2000 + int(yy), month, int(dd),
                                     23, 59, tzinfo=timezone.utc)
            mins = (close_utc - datetime.now(timezone.utc)).total_seconds() / 60
            return mins
        except Exception:
            return None

    def _pre_settlement_cancel(self, ticker: str) -> int:
        """#104 — T-X minutes before close, cancel all quotes on this market.

        Rationale (Apr 27 audit): Friday W-series weekly commodities lose
        $60-90 in last 30 min as informed flow piles in for settlement.
        Last 30 min of rebate accrual is tiny ($1-2/market) vs typical
        adverse fill cost ($5-30/market). Net: cancel quotes early.

        Triggers when minutes_until_settle < PRE_SETTLEMENT_CANCEL_MIN AND
        > 0 (don't process already-settled markets).
        """
        mins = self._minutes_until_settle(ticker)
        if mins is None or mins <= 0:
            return 0
        if mins > settings.PRE_SETTLEMENT_CANCEL_MIN:
            return 0
        # Within the cancel window — drop all quotes
        n_resting = len(self.qm.resting.get(ticker, []))
        if n_resting == 0:
            return 0
        # Throttle log
        now_ts = time.time()
        last = self._fv_skip_log_ts.get(f"presettle:{ticker}", 0)
        if now_ts - last > 600:  # log once per 10 min per ticker
            _log.info(f"pre_settlement_cancel {ticker}: T-{mins:.1f}min, "
                      f"cancelling {n_resting} resting orders")
            self._fv_skip_log_ts[f"presettle:{ticker}"] = now_ts
        self.qm.cancel_all(market_ticker=ticker)
        return n_resting

    def _cancel_zombie_quotes(self, ticker: str, best_yes_cents: int,
                              best_no_cents: int, book_age_sec: float) -> int:
        """Task #114: cancel resting orders that fell ≥ZOMBIE_GAP cents below best.

        When the book moves but our reprice is blocked (safety gate, WS lag,
        stale book between updates), our resting price can drift far from best.
        At that point we score 0 (DF^big_gap → 0) AND occupy capacity that
        a fresh quote at best could be earning on.

        Heartbeat-cadence cleanup: if our resting price < best - threshold,
        cancel. Reprice loop will re-place at best on next book update.

        Audit fix R1 (2026-04-28): require fresh book before cancelling.
        A stale book may show transient wide-spread state; cancelling on
        that would kill perfectly-placed quotes whose price was at the
        prior best.
        """
        if book_age_sec > settings.STALE_DATA_PULL_SECONDS:
            return 0  # don't trust stale book data for cancellation decisions
        gap_threshold = settings.ZOMBIE_GAP_CENTS
        cancelled = 0
        for o in list(self.qm.resting.get(ticker, [])):
            best = best_yes_cents if o.side == "yes" else best_no_cents
            if best is None:
                continue
            if o.price_cents < best - gap_threshold:
                if self.qm._cancel_order(o):
                    cancelled += 1
                    _log.warning(f"zombie cancel {ticker} {o.side} "
                                 f"@{o.price_cents}¢ vs best {best}¢ "
                                 f"(gap {best - o.price_cents}¢)")
        return cancelled

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
            # #102 (2026-04-28): UNRELIABLE/UNKNOWN futures = no fair-value
            # to lean on. Apply a price-conviction gate instead: if either
            # side bids ≥ UNRELIABLE_FUTURES_MAX_BID, the orderbook itself
            # is telling us the market has directional consensus we can't
            # verify. KXCOFFEEW lost -$61 on Apr 25 because we quoted NO
            # at 60-70c without any signal Coffee was actually moving up.
            limit = settings.UNRELIABLE_FUTURES_MAX_BID
            if yes_bid >= limit:
                return (f"unreliable_futures_skip[{prefix}] yes_bid={yes_bid}c "
                        f">= {limit}c, no fair-value to verify")
            if no_bid >= limit:
                return (f"unreliable_futures_skip[{prefix}] no_bid={no_bid}c "
                        f">= {limit}c, no fair-value to verify")
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

    def _is_volatile(self, ticker: str, best_yes_cents: int,
                     best_no_cents: int) -> bool:
        """#98: detect rapid best-bid movement on either side.

        Tracks last 20 (ts, best_yes, best_no) tuples per ticker. Returns
        True when the range (max-min) on either side over the last
        VOLATILITY_WINDOW_SEC exceeds VOLATILITY_BACKOFF_TICKS cents.

        When True, the caller should skip this reprice cycle so we don't
        cancel/replace into a fast-moving book where adverse selection
        risk is high (informed traders move price first, hit our stale
        replacement second).
        """
        now = time.time()
        hist = self._best_history[ticker]
        hist.append((now, best_yes_cents, best_no_cents))
        cutoff = now - settings.VOLATILITY_WINDOW_SEC
        recent = [h for h in hist if h[0] >= cutoff]
        if len(recent) < 3:
            return False
        yes_range = max(h[1] for h in recent) - min(h[1] for h in recent)
        no_range  = max(h[2] for h in recent) - min(h[2] for h in recent)
        return (yes_range >= settings.VOLATILITY_BACKOFF_TICKS or
                no_range  >= settings.VOLATILITY_BACKOFF_TICKS)

    def _quote_target_for(self, book: BookState) -> QuoteTarget | None:
        """Compute our desired quote using ADAPTIVE sizing to target 25% share."""
        p = self.params_by_ticker.get(book.market_ticker)
        if p is None:
            return None
        best_yes = book.best_yes_bid()
        best_no  = book.best_no_bid()
        if best_yes is None or best_no is None:
            return None

        # #104 (2026-04-28) Pre-settlement skip: don't re-quote in last
        # X min before close. Heartbeat already cancelled; this prevents
        # a book update from immediately triggering a fresh placement.
        mins_until = self._minutes_until_settle(book.market_ticker)
        if mins_until is not None and 0 < mins_until <= settings.PRE_SETTLEMENT_CANCEL_MIN:
            return None

        # #98 Tick backoff: skip reprice when best is moving fast. Don't
        # chase a flickering market — protects against being the slow
        # replacement quote that informed flow picks off. Throttle log so
        # we can tell volatility-skips apart from other skip-reasons.
        if self._is_volatile(book.market_ticker, best_yes.price_cents,
                             best_no.price_cents):
            now_ts = time.time()
            last = self._fv_skip_log_ts.get(f"vol:{book.market_ticker}", 0)
            if now_ts - last > 300:
                _log.info(f"volatility_skip[{book.market_ticker}] "
                          f"best moved >{settings.VOLATILITY_BACKOFF_TICKS}c "
                          f"in last {settings.VOLATILITY_WINDOW_SEC}s")
                self._fv_skip_log_ts[f"vol:{book.market_ticker}"] = now_ts
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
        # 2026-05-03 GOLDEN-FUNNEL: enforce qualify-cliff floor from capital_allocator.
        # Sizer's adaptive output may be sub-qualifying; floor lifts it to where
        # rebate is actually earned. Capped by safety gates downstream.
        floor = self.optimal_size_floors.get(book.market_ticker, 0)
        if floor > size:
            size = floor

        # 2026-04-27: per-series size multiplier (audit: KXBRENTD/CORNW/COPPERD/
        # GOLDW/COCOAW are net-positive, deserve 2x cap allocation). Cap by
        # _passes_safety per-market USD limit anyway.
        series = book.market_ticker.split("-", 1)[0] if book.market_ticker else ""
        mult = settings.SIZE_MULTIPLIER_BY_SERIES.get(
            series, settings.DEFAULT_SIZE_MULTIPLIER,
        )
        if mult != 1.0:
            size = int(size * mult)

        # #97 (2026-04-28) Active inventory skew: when we hold a net position,
        # bias quote sizes to absorb the offsetting side and let the long
        # side bleed off naturally. Reduces time-to-flat from minutes
        # (passive) to seconds (active recirculation).
        # NOTE 2026-05-06: HYDRA-skew layer reverted — was costing /mo API
        # without proven value. Pure inventory-skew restored. Helper function
        # _hydra_skew_for() retained for future use but no longer called.
        yes_size_override = None
        no_size_override = None
        self.qm._refresh_inventory(book.market_ticker)
        inv = self.qm.inventory.get(book.market_ticker)
        if inv and inv.net_yes_contracts != 0:
            net = inv.net_yes_contracts
            skew_amount = min(int(size * settings.INVENTORY_SKEW_FRACTION),
                              abs(net))
            min_size = settings.MIN_QUOTE_SIZE_CONTRACTS
            if net > 0:
                # Long YES — boost NO bid to absorb shorts, shrink YES bid
                no_size_override  = size + skew_amount
                yes_size_override = max(min_size, size - skew_amount)
            else:
                # Long NO — boost YES bid to absorb longs, shrink NO bid
                yes_size_override = size + skew_amount
                no_size_override  = max(min_size, size - skew_amount)

        return QuoteTarget(
            market_ticker=book.market_ticker,
            yes_bid_cents=best_yes.price_cents,
            no_bid_cents=best_no.price_cents,
            size_contracts=size,
            yes_size_override=yes_size_override,
            no_size_override=no_size_override,
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
            # 2026-05-03 UNIT-FIX: persist RAW our_score (not normalized 0-2).
            # Was: r.our_total_score (= sum of normalized shares, max 2.0)
            # Now: actual sum of (size × DF^N) raw units, comparable with total_score.
            # Without this, our_score/total_score produced meaningless ~0.001 ratios
            # that EST_REBATE_1H interpreted as 0.1% share when real share was ~50%.
            persist_our_score = (
                (r.our_yes_normalized * (r.yes_total_qualifying_score or 0)) +
                (r.our_no_normalized  * (r.no_total_qualifying_score  or 0))
            )
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

        # 2026-05-02 PREDATOR C1: offload reconcile to thread executor.
        # Was: sync HTTP POST + sqlite I/O inside async WS callback,
        # blocking event loop 200-500ms per call. Result: 0 fills/hr in
        # observation — competing MMs reprice during our blocked window
        # and pick us off. Now: WS keeps draining book updates for OTHER
        # markets while this one's API call resolves in the threadpool.
        # Per-market lock prevents concurrent reconciles on the SAME
        # ticker (would double-cancel/double-place).
        ticker = book.market_ticker
        lock = self._reconcile_locks.get(ticker)
        if lock is None:
            lock = asyncio.Lock()
            self._reconcile_locks[ticker] = lock
        async with lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.qm.reconcile, target)
        self.reconciles[ticker] += 1

    async def heartbeat_snapshot_loop(self, ws, interval_sec: int = 30):
        """Periodically snapshot all quoted markets even if book hasn't updated.
        This ensures lip_snapshots is populated across ALL active markets, not
        just the noisy ones. Critical for honest share-estimation across the
        full paper portfolio.
        """
        import sqlite3
        from engine.lip_scorer import OurQuotes, score_snapshot
        last_resync = 0.0
        RESYNC_INTERVAL_SEC = 300  # 5 min — cheap (one Kalshi API call)
        # 2026-05-02 PREDATOR K4: hourly refresh of per-market target_share
        # from realized 7d snapshot share so sizer chases REAL capacity not
        # a hardcoded 0.35.
        last_target_share_refresh = 0.0
        TARGET_SHARE_REFRESH_SEC = 3600  # 1 hour
        while True:
            try:
                await asyncio.sleep(interval_sec)
                now = time.time()
                # Refresh blacklist once per heartbeat cycle
                self._refresh_blacklist()
                # 2026-05-02 PREDATOR K1: drain requote queue from capital_reaper.
                # Each entry = (venue, ticker) cancelled by the reaper. We
                # force-trigger an immediate book-update reconcile on those
                # tickers so the snapshot gap closes within ~30s instead of
                # waiting up to 30 min for the next periodic_discover.
                try:
                    from cross_venue.requote_queue import drain as _drain_requote
                    requotes = _drain_requote(venue_filter="kalshi")
                    if requotes:
                        _log.info(f"K1 drained {len(requotes)} reaper-cancelled "
                                  f"tickers; forcing immediate reconcile")
                        for entry in requotes:
                            tkr = entry.get("key", "")
                            book = ws.books.get(tkr)
                            if tkr and book is not None and tkr in self.params_by_ticker:
                                # bypass last_score_ts throttle so on_book_update
                                # actually fires instead of returning early
                                self.last_score_ts[tkr] = 0
                                await self.on_book_update(book)
                except Exception as e:
                    _log.warning(f"K1 requote drain failed: {e}")
                # 2026-04-28: periodic re-sync of self.qm.resting against
                # live Kalshi orders. Cures sizer drift where filled/cancelled
                # orders weren't purged from memory → safety gate inflated cap.
                if now - last_resync >= RESYNC_INTERVAL_SEC:
                    last_resync = now
                    # PREDATOR C1: periodic_resync makes Kalshi API calls →
                    # offload from heartbeat loop so other markets keep firing.
                    await asyncio.get_running_loop().run_in_executor(
                        None, self.qm.periodic_resync
                    )
                # K4: hourly refresh of per-market sizer targets from DB
                if now - last_target_share_refresh >= TARGET_SHARE_REFRESH_SEC:
                    last_target_share_refresh = now
                    try:
                        n = await asyncio.get_running_loop().run_in_executor(
                            None, self.sizer.update_target_shares_from_db,
                            settings.DB_PATH,
                        )
                        if n:
                            _log.info(f"K4 refreshed sizer target_share for {n} markets")
                    except Exception as e:
                        _log.warning(f"K4 sizer target refresh failed: {e}")
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
                    # #104 (2026-04-28): pre-settlement cancel — kill quotes
                    # in last X min before close to avoid adverse-fill bleed
                    # on Friday W-series settlements.
                    self._pre_settlement_cancel(tkr)
                    # Task #114: zombie sweep — cancel any resting order that
                    # drifted ≥ZOMBIE_GAP_CENTS below best. Frees capital,
                    # fresh quote will be placed at best on next book update.
                    book_age = now - (book.last_update_ts or now)
                    self._cancel_zombie_quotes(tkr, best_yes.price_cents,
                                               best_no.price_cents, book_age)
                    # Construct augmented book with our hypothetical quote
                    size_yes = self.sizer.size_for(tkr, "yes", params.target_size)
                    size_no  = self.sizer.size_for(tkr, "no",  params.target_size)
                    size = min(size_yes, size_no)
                    # GOLDEN-FUNNEL qualify-cliff floor (heartbeat path)
                    floor = self.optimal_size_floors.get(tkr, 0)
                    if floor > size:
                        size = floor
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
                        # GOLDEN-FUNNEL UNIT-FIX: persist RAW score, not normalized.
                        persist_our_score = (
                            (r.our_yes_normalized * (r.yes_total_qualifying_score or 0)) +
                            (r.our_no_normalized  * (r.no_total_qualifying_score  or 0))
                        )
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

                # NEXUS port (2026-04-30): Tiered breaker awareness in heartbeat log.
                # quote_manager._passes_safety has BINARY halt-at-MAX_DAILY_LOSS gate;
                # we ALSO emit a tier indicator so operator sees pressure building
                # before the binary trip. No behavior change in this loop — the
                # binary safety still runs. This is observability + early warning.
                try:
                    daily_pnl = self.qm._daily_realized_pnl() if hasattr(self.qm, '_daily_realized_pnl') else 0
                    cap = settings.MAX_DAILY_LOSS_USD
                    if cap > 0 and daily_pnl < 0:
                        loss_pct = abs(daily_pnl) / cap * 100
                        if loss_pct >= 100:
                            tier = "🔴 HALT"
                        elif loss_pct >= 60:
                            tier = "🟠 throttle"
                        elif loss_pct >= 30:
                            tier = "🟡 warn"
                        else:
                            tier = "🟢 normal"
                        if loss_pct >= 30:  # only log when worth attention
                            _log.info(f"BREAKER_TIER {tier}: daily_pnl=${daily_pnl:.2f} "
                                      f"({loss_pct:.0f}% of cap ${cap:.0f})")
                except Exception:
                    pass

                # NEXUS V4 D-misc port (2026-04-30): Money-Print line for Kalshi.
                # Theoretical max: sum(reward_per_day_usd) of markets we currently quote.
                # Calibration factor: empirical haircut (Kalshi calibrated_predictor
                # showed actual ≈ 0.20-0.30x of theoretical).
                try:
                    KALSHI_CALIB = 0.25
                    qm_summary = self.qm.summary() if hasattr(self.qm, "summary") else {}
                    # Kalshi summary key is total_gross_usd (not gross_usd)
                    cap_deployed = (qm_summary.get("total_gross_usd")
                                    or qm_summary.get("gross_usd") or 0)
                    quoted_tickers = set(self.qm.resting.keys()) if hasattr(self.qm, "resting") else set()
                    proj_max = sum(
                        m["reward_per_day_usd"] for m in self.markets
                        if m["market_ticker"] in quoted_tickers
                    )
                    proj_real = proj_max * KALSHI_CALIB
                    yield_pct = (proj_real / cap_deployed * 100) if cap_deployed > 0 else 0.0
                    _log.info(
                        f"💰 MONEY_PRINT: cap=${cap_deployed:.2f} "
                        f"proj_daily=${proj_real:.2f} (theo_max=${proj_max:.0f}, calib={KALSHI_CALIB:.2f}) "
                        f"proj_monthly=${proj_real*30:.0f} "
                        f"yield={yield_pct:.2f}%/d"
                    )
                except Exception:
                    pass
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


def _compute_saturated_tickers(saturation_threshold: float = 0.80) -> set[str]:
    """Return tickers where existing position OR resting orders consume
    >= threshold * per-market cap, so the ranker can skip re-surfacing them.

    Two saturation dimensions checked:
      1. market_exposure_dollars (live position) >= threshold × per-market cap
      2. market_exposure_dollars >= threshold × bankroll_share cap

    Either trip → ticker is saturated → ranker should pick a fresh market
    instead. Defensive: any API failure returns empty set (safe default).
    """
    try:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        positions = c.get("/portfolio/positions", params={"limit": 200}).get("market_positions", [])
        balance = float(c.get_balance())
    except Exception as e:
        _log.warning(f"saturation check failed (returning empty set): {e}")
        return set()

    bankroll_cap = balance * settings.MAX_BANKROLL_SHARE_PCT
    saturated: set[str] = set()
    for p in positions:
        exp = float(p.get("market_exposure_dollars", 0))
        if exp <= 0:
            continue
        ticker = p.get("ticker", "")
        series = ticker.split("-", 1)[0]
        per_mkt_cap = settings.MAX_GROSS_PER_MARKET_BY_SERIES.get(
            series, settings.MAX_GROSS_PER_MARKET_USD
        )
        if exp >= per_mkt_cap * saturation_threshold or exp >= bankroll_cap * saturation_threshold:
            saturated.add(ticker)
    if saturated:
        _log.info(f"saturated tickers ({len(saturated)} markets at >={int(saturation_threshold*100)}% cap): "
                  f"{sorted(saturated)[:10]}{'...' if len(saturated)>10 else ''}")
    return saturated


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
    # 2026-04-28: rolled back sniper experiment (was too restrictive — only
    # 1 market passed filter on a single bad day). Restored top_n_to_quote
    # which uses priority-weighted ranking + #119 winner multipliers.
    # Sniper scanner remains available as analysis tool (tools/competitor_density.py).
    # 2026-05-01 PREDATOR: pass saturated tickers so ranker skips markets
    # where existing positions already exhaust per-market cap. Without this
    # the ranker keeps surfacing the same stuck markets at top-N.
    saturated = _compute_saturated_tickers()
    use_capital_alloc = os.getenv("LIP_USE_CAPITAL_ALLOC", "true").lower() == "true"
    if use_capital_alloc:
        markets = select_optimal_portfolio(
            budget_usd=settings.MAX_TOTAL_GROSS_USD,
            exclude_tickers=saturated,
        )
        _log.info(f"capital-aware ranker: {len(markets)} markets, "
                  f"E[net]=${sum(m.get('expected_net_per_day',0) for m in markets):.2f}/d")
        # Sprint 4 #2: pre-deploy depth gate (reject <5% projected share)
        if os.getenv("DEPTH_GATE_ENABLED", "true").lower() == "true":
            try:
                from execution.kalshi_auth import KalshiClient as _KC_dg
                _dg_client = _KC_dg()
                pre_n = len(markets)
                markets = filter_by_depth(
                    markets, _dg_client,
                    min_share=float(os.getenv("DEPTH_GATE_MIN_SHARE", "0.05")),
                    exit_gate_enabled=os.getenv("EXIT_GATE_ENABLED", "true").lower() == "true",
                    min_exit_contracts=int(os.getenv("MIN_EXIT_CONTRACTS", "50")),
                    min_exit_price_cents=int(os.getenv("MIN_EXIT_PRICE_CENTS", "5")),
                )
                _log.info(f"depth_gate: {pre_n} -> {len(markets)} markets "
                          f"({100*(pre_n-len(markets))/max(1,pre_n):.0f}% rejected)")
            except Exception as e:
                _log.warning(f"depth_gate skipped due to error: {e}")
    else:
        markets = top_n_to_quote(top_n, exclude_tickers=saturated)
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
                # PREDATOR: refresh saturated set per cycle so newly-saturated
                # markets get filtered + freshly-drained ones get re-included.
                saturated = _compute_saturated_tickers()
                if use_capital_alloc:
                    fresh = select_optimal_portfolio(
                        budget_usd=settings.MAX_TOTAL_GROSS_USD,
                        exclude_tickers=saturated,
                    )
                    # Sprint 4 #2: depth gate on refresh
                    if os.getenv("DEPTH_GATE_ENABLED", "true").lower() == "true":
                        try:
                            from execution.kalshi_auth import KalshiClient as _KC_dg2
                            _dg_client2 = _KC_dg2()
                            pre_n = len(fresh)
                            fresh = filter_by_depth(
                                fresh, _dg_client2,
                                min_share=float(os.getenv("DEPTH_GATE_MIN_SHARE", "0.05")),
                                exit_gate_enabled=os.getenv("EXIT_GATE_ENABLED", "true").lower() == "true",
                                min_exit_contracts=int(os.getenv("MIN_EXIT_CONTRACTS", "50")),
                                min_exit_price_cents=int(os.getenv("MIN_EXIT_PRICE_CENTS", "5")),
                            )
                            _log.info(f"depth_gate refresh: {pre_n} -> {len(fresh)}")
                        except Exception as e:
                            _log.warning(f"depth_gate refresh skipped: {e}")
                else:
                    fresh = top_n_to_quote(top_n, exclude_tickers=saturated)
                current_tickers = set(runner.params_by_ticker.keys())
                fresh_tickers = {m["market_ticker"] for m in fresh}
                new_tickers = fresh_tickers - current_tickers
                if new_tickers:
                    _log.info(f"periodic_discover: {len(new_tickers)} new markets to subscribe")
                    for m in fresh:
                        tkr = m["market_ticker"]
                        if tkr in new_tickers:
                            runner.params_by_ticker[tkr] = ProgramParams(
                                market_ticker=tkr,
                                target_size=int(m["target_size"]),
                                discount_factor=float(m["discount_factor"]),
                                period_reward_usd=float(m["reward_per_day_usd"]),
                            )
                            runner.optimal_size_floors[tkr] = int(
                                m.get("optimal_size_per_side", 0) or 0
                            )
                            runner.markets.append(m)
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
