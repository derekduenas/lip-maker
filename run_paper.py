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
import math
import signal
import sqlite3
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings
from engine.lip_discovery import (
    discover, discover_result, top_n_to_quote, is_active_clause, _parse_ts,
    last_complete_scan_ts,
)
# from engine.sniper_select import top_n_by_ev  # archived 2026-04-29 (audit: unused)
# 2026-05-03 GOLDEN-FUNNEL: capital-aware ranker. Replaces fixed top-N with
# greedy yield-per-dollar fill. N becomes OUTPUT not INPUT — adapts to
# opportunity quality + budget. Toggle via LIP_USE_CAPITAL_ALLOC env (default true).
from engine.account_ledger import AccountLedger
from engine.capital_allocator import select_optimal_portfolio
from engine.depth_probe import filter_by_depth
from engine.lip_scorer import (
    OurQuotes, ProgramParams, SnapshotScore, score_snapshot,
    interval_payout_usd, snapshot_share, _find_cutoff_price,
)
from engine.adaptive_sizer import AdaptiveSizer
from engine.microprice import microprice_yes  # A.1: imbalance-weighted fair value
from engine.reservation_price import (        # A.2: inventory-aware fair value
    reservation_price, realized_sigma_cents, suggest_quote_skew,
)
from execution.kalshi_ws import KalshiWS, BookState, BookLevel, FillEvent
from execution.quote_manager import QuoteManager, QuoteTarget



_log = logging.getLogger("lip_maker")


def _program_params_from_market(m: dict) -> ProgramParams:
    """Build ProgramParams in the right units (2026-09-20 audit #3).

    Discovery rows carry the TOTAL pool (`period_reward_usd`) and the exact
    window (`period_seconds`). Rows from older DBs or the capital allocator
    may only carry `reward_per_day_usd`; express that rate as a 1-day window
    so pool/period_seconds is still the correct $/sec."""
    pool = m.get("period_reward_usd")
    secs = m.get("period_seconds")
    try:
        secs_f = float(secs) if secs is not None else 0.0
    except (TypeError, ValueError):
        secs_f = 0.0
    sd = _parse_ts(m.get("start_date"))
    ed = _parse_ts(m.get("end_date"))
    start_ts = sd.timestamp() if sd is not None else None
    end_ts = ed.timestamp() if ed is not None else None
    if pool is not None and secs_f > 0:
        return ProgramParams(
            market_ticker=m["market_ticker"],
            target_size=float(m["target_size"]),
            discount_factor=float(m["discount_factor"]),
            period_reward_usd=float(pool),
            period_seconds=secs_f,
            start_ts=start_ts, end_ts=end_ts,
        )
    return ProgramParams(
        market_ticker=m["market_ticker"],
        target_size=float(m["target_size"]),
        discount_factor=float(m["discount_factor"]),
        period_reward_usd=float(m["reward_per_day_usd"]),
        period_seconds=86400.0,
        start_ts=start_ts, end_ts=end_ts,
    )


# Skip reasons from _quote_target_for that do NOT justify pulling resting
# orders: the market is still one we want to be in, we just don't want to
# re-price this instant. Everything else (fair_value, pre_settlement,
# stale_book, no_params, unknown) means our resting orders are exposure
# without a thesis and must be cancelled (2026-09-20 audit #7).
TRANSIENT_SKIP_REASONS = frozenset({"volatility", "no_best"})


@dataclass
class ScoredMarket:
    """One market's snapshot score plus how it was obtained."""
    market_ticker: str
    result: SnapshotScore
    is_resting: bool          # two-sided ACTUAL orders resting ≥ min size
    share: float              # our fraction of this snapshot's credit, 0–1 (0 unless resting+valid)
    mode: str                 # "actual" | "hypothetical" | "none"

    @property
    def raw_our_score(self) -> float:
        """Sum of (size × DF^N) raw units, comparable with total_score."""
        if not self.is_resting:
            return 0.0
        r = self.result
        return ((r.our_yes_normalized * (r.yes_total_qualifying_score or 0)) +
                (r.our_no_normalized  * (r.no_total_qualifying_score  or 0)))


@dataclass
class AccrualState:
    """Forward interval accounting for one market's LIP accrual
    (2026-09-20 review). `last_share` is the share we OBSERVED at
    `last_ts`; it is what the venue was paying us until we observed
    something different, so the NEXT row credits it over [last_ts, now).
    `last_ts is None` means the state is unknown (start, stale book,
    disconnect, gap) and the next row credits nothing."""
    program_key: Optional[float]      # program start_ts; a new window resets accrued
    accrued_usd: float = 0.0          # cumulative for this program window
    last_ts: Optional[float] = None
    last_share: float = 0.0
    breaks: int = 0


class PaperRunner:
    # Longest interval one snapshot row may claim accrual for. Rows are
    # written at ≤5s cadence from book updates and every heartbeat (30s);
    # a longer gap means we were NOT observing: the state is unknown and
    # NOTHING is claimed for it (the chain restarts at the new row).
    SNAPSHOT_MAX_INTERVAL_SEC = 60.0
    SKIP_CANCEL_THROTTLE_SEC = 30.0

    def __init__(self, markets: list[dict]):
        self.markets = markets
        self.params_by_ticker = {
            m["market_ticker"]: _program_params_from_market(m)
            for m in markets
        }
        # 2026-09-20 audit #7: last skip reason per ticker from
        # _quote_target_for, so on_book_update can decide whether resting
        # orders must be pulled. Counts per reason for the summary line.
        self._skip_reason: dict[str, str] = {}
        self.skip_counts: dict[str, int] = defaultdict(int)
        self.fill_counts: dict[str, int] = defaultdict(int)
        self._skip_cancel_ts: dict[str, float] = {}
        # 2026-09-20 review: per-market forward accrual chains.
        self._accrual: dict[str, AccrualState] = {}
        # Freshness of the last COMPLETE discovery scan (epoch). Seeded from
        # the DB so a restart does not start out trusting unverified rows.
        self.last_complete_scan_ts: float | None = last_complete_scan_ts()
        self._ensure_snapshot_schema()
        # 2026-05-03 GOLDEN-FUNNEL: per-market size FLOOR from capital_allocator.
        # Ensures we always quote enough to cross the qualify cliff. Sizer can
        # size larger if observed competition demands; never smaller than this.
        self.optimal_size_floors: dict[str, int] = {
            m["market_ticker"]: int(m.get("optimal_size_per_side", 0) or 0)
            for m in markets
        }
        # 2026-04-21: paper flag now controlled by LIP_PAPER env via settings.
        # When settings.PAPER_MODE is False, QuoteManager hits real Kalshi.
        # 2026-09-21: one shared account. Capital is reserved when an order
        # rests and released when it does not, so the portfolio cannot spend
        # money it does not have. Events go to the same store the offline
        # research reads, so paper and replay report from one history.
        self.account = AccountLedger(
            mode="paper" if settings.PAPER_MODE else "live",
            event_db_path=str(Path(settings.DB_PATH).with_name("account_events.db")),
        )
        self.qm = QuoteManager(paper=settings.PAPER_MODE, account=self.account)
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
        # A.1 (2026-05-14): microprice cache. Updated on every book in
        # _quote_target_for. Consumed by A.2 (reservation price) and A.4
        # (markout logger). Tuple of (microprice_yes_cents, ts).
        self._last_microprice: dict[str, tuple[float, float]] = {}

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

    def _ensure_snapshot_schema(self) -> None:
        """Add lip_snapshots columns introduced after the original schema
        (idempotent; silently skipped when the table does not exist yet)."""
        try:
            conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(lip_snapshots)").fetchall()}
                if not cols:
                    return
                if "was_resting" not in cols:
                    conn.execute("ALTER TABLE lip_snapshots ADD COLUMN was_resting INTEGER DEFAULT 0")
                if "our_share" not in cols:
                    conn.execute("ALTER TABLE lip_snapshots ADD COLUMN our_share REAL")
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.debug(f"lip_snapshots schema check skipped: {e}")

    def _live_resting(self, ticker: str) -> list:
        """Resting orders we believe are on the venue (cancel not yet requested)."""
        return [o for o in self.qm.resting.get(ticker, [])
                if not getattr(o, "pending_cancel", False)]

    def _actually_resting(self, ticker: str, target=None) -> bool:
        """Check if we have qualifying two-sided quotes resting on this market.

        2026-04-25 (Phantom-snapshot fix v2). Permissive: just checks
        existence of two-sided resting orders at min-quote size. Doesn't
        require exact price match because books move constantly between
        target computation and actual placement — strict price-match
        rejected too many legitimate resting cases.

        2026-09-20 audit #3: `target` is no longer consulted at all — the
        scorer now uses the ACTUAL resting prices/sizes (see _score_market),
        so this is purely the two-sided-presence gate. Parameter kept for
        call-site compatibility.
        """
        if ticker in self.qm.uncertain_markets:
            return False
        orders = self._live_resting(ticker)
        yes = next((o for o in orders if o.side == "yes"), None)
        no  = next((o for o in orders if o.side == "no"),  None)
        if not (yes and no):
            return False
        if yes.size_contracts < settings.MIN_QUOTE_SIZE_CONTRACTS:
            return False
        if no.size_contracts < settings.MIN_QUOTE_SIZE_CONTRACTS:
            return False
        return True

    @staticmethod
    def _augment(book: BookState, ours: OurQuotes) -> BookState:
        """Copy of `book` with our quotes folded in. ONLY for quotes that
        are not on the venue book (paper shadow orders, or a hypothetical
        target we have not placed yet)."""
        aug = BookState(market_ticker=book.market_ticker)
        aug.yes_bids = [BookLevel(l.price_cents, l.size) for l in book.yes_bids]
        aug.no_bids  = [BookLevel(l.price_cents, l.size) for l in book.no_bids]
        for side_levels, our_levels in ((aug.yes_bids, ours.yes_bids),
                                        (aug.no_bids,  ours.no_bids)):
            for q in our_levels:
                for lvl in side_levels:
                    if lvl.price_cents == q.price_cents:
                        lvl.size += q.size
                        break
                else:
                    side_levels.append(BookLevel(q.price_cents, q.size))
            side_levels.sort(key=lambda l: -l.price_cents)
        return aug

    def _score_market(self, book: BookState, params: ProgramParams,
                      target: QuoteTarget | None = None) -> ScoredMarket:
        """Single scoring path for book updates AND the heartbeat
        (2026-09-20 audit #3).

        - Orders resting → score what is ACTUALLY resting (price + size per
          order, per side). In live mode those orders are already part of
          the venue book, so the public book is scored as-is; adding them
          again would double-count our depth, move the cutoff and inflate
          our share. In paper mode shadow orders never reach the venue, so
          the book is augmented to simulate presence.
        - Nothing resting but a target exists → hypothetical score of the
          target (respecting per-side size overrides) against an augmented
          book. Persisted as was_resting=0 / share 0 (phantom).
        - Neither → book validity only.
        """
        ticker = book.market_ticker
        resting = self._live_resting(ticker)
        is_resting = self._actually_resting(ticker)
        if resting:
            ours = OurQuotes(
                yes_bids=[BookLevel(o.price_cents, float(o.size_contracts))
                          for o in resting if o.side == "yes"],
                no_bids=[BookLevel(o.price_cents, float(o.size_contracts))
                         for o in resting if o.side == "no"],
            )
            scored_book = self._augment(book, ours) if self.qm.paper else book
            mode = "actual"
        elif target is not None:
            yes_sz = (target.yes_size_override if target.yes_size_override is not None
                      else target.size_contracts)
            no_sz = (target.no_size_override if target.no_size_override is not None
                     else target.size_contracts)
            ours = OurQuotes(
                yes_bids=([BookLevel(target.yes_bid_cents, float(yes_sz))]
                          if target.yes_bid_cents is not None else []),
                no_bids=([BookLevel(target.no_bid_cents, float(no_sz))]
                         if target.no_bid_cents is not None else []),
            )
            scored_book = self._augment(book, ours)
            mode = "hypothetical"
        else:
            ours = OurQuotes()
            scored_book = book
            mode = "none"
        r = score_snapshot(scored_book, ours, params)
        share = snapshot_share(r) if is_resting else 0.0
        return ScoredMarket(ticker, r, is_resting, share, mode)

    def _record_score(self, scored: ScoredMarket, now: float, *, feed_sizer: bool) -> None:
        """Session stats + (book path only) sizer feedback. Only ACTUAL
        resting presence on a valid snapshot counts — never phantom scores."""
        tkr = scored.market_ticker
        r = scored.result
        self.snapshots_scored[tkr] += 1
        if not (scored.is_resting and r.snapshot_valid):
            return
        self.snapshots_valid[tkr] += 1
        self.our_score_sum[tkr] += r.our_total_score
        if not feed_sizer:
            return
        our_yes_score = (r.our_yes_normalized * r.yes_total_qualifying_score
                         if r.yes_total_qualifying_score else 0)
        our_no_score  = (r.our_no_normalized * r.no_total_qualifying_score
                         if r.no_total_qualifying_score else 0)
        self.sizer.observe(
            tkr,
            yes_total_qual=r.yes_total_qualifying_score,
            no_total_qual=r.no_total_qualifying_score,
            our_yes_contribution=our_yes_score,
            our_no_contribution=our_no_score,
            ts=now,
        )

    def _persist_snapshot(self, ticker: str, scored: ScoredMarket,
                          params: ProgramParams, now: float) -> bool:
        """Write one lip_snapshots row (throttled to 1 per 5s per market).

        2026-09-20 review (interval accounting): estimated_payout_usd on row
        N is what the interval [t_{N-1}, t_N) was worth at the share we
        OBSERVED at t_{N-1} — forward attribution from the last known state,
        never the new share applied backward. It is zero when the prior
        state is unknown (first row, stale book, disconnect, gap longer than
        SNAPSHOT_MAX_INTERVAL_SEC), clipped to the program window, and
        capped so the cumulative estimate for a program never exceeds
        pool × LIP_MAX_ACCOUNT_SHARE_OF_POOL. Summing the column over a
        window therefore estimates the payout in dollars.
        """
        key = int(now / 5)
        if self._last_persist_key.get(ticker, -1) == key:
            return False
        self._last_persist_key[ticker] = key
        st = self._accrual_for(ticker, params)
        payout, _note = self._accrue(st, params, now)
        st.last_ts = now
        st.last_share = scored.share
        r = scored.result
        try:
            conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
            try:
                conn.execute(
                    """INSERT INTO lip_snapshots
                       (market_ticker, captured_at, our_score, total_score,
                        yes_qualified, no_qualified, snapshot_valid,
                        estimated_payout_usd, was_resting, our_share)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ticker,
                     datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                     scored.raw_our_score,
                     r.yes_total_qualifying_score + r.no_total_qualifying_score,
                     1 if (scored.is_resting and r.yes_qualified) else 0,
                     1 if (scored.is_resting and r.no_qualified) else 0,
                     1 if (scored.is_resting and r.snapshot_valid) else 0,
                     payout,
                     1 if scored.is_resting else 0,
                     scored.share),
                )
                conn.commit()
            finally:
                conn.close()
            return True
        except Exception as e:
            self._snapshot_persist_failures += 1
            _log.warning(f"snapshot persist failed for {ticker}: {e} "
                         f"(total failures this session: {self._snapshot_persist_failures})")
            return False

    def _handle_skip(self, ticker: str, reason: str, *, force: bool = False) -> bool:
        """A quote target was NOT produced for `ticker` (2026-09-20 audit #7).

        Transient reasons keep resting orders (we still want the market,
        just not a reprice right now). Any other reason means the resting
        orders are exposure we no longer have a thesis for: cancel them
        (throttled so a flapping gate can't spam the API; `force=True`
        bypasses the throttle for stale/disconnect/retire, which are rare
        transitions where waiting is the failure). Only orders we can prove
        are ours are cancelled. Returns True when a cancel was issued."""
        self.skip_counts[reason] += 1
        base = reason.split(":", 1)[0]
        if base in TRANSIENT_SKIP_REASONS:
            return False
        if not [o for o in self._live_resting(ticker) if o.is_ours]:
            return False
        now = time.time()
        if not force and now - self._skip_cancel_ts.get(ticker, 0.0) < self.SKIP_CANCEL_THROTTLE_SEC:
            return False
        self._skip_cancel_ts[ticker] = now
        self.qm.cancel_all(market_ticker=ticker, only_ours=True)
        # From this instant our share is known to be zero (conservative:
        # whatever was accrued between the last row and now is forfeited).
        self._note_flat(ticker)
        _log.info(f"skip_cancel[{ticker}] reason={reason}: pulled resting orders")
        return True

    def pull_all_exposure(self, reason: str) -> int:
        """Cancel our resting orders on EVERY market (WS disconnect, shutdown
        of trust). Bypasses throttles; breaks every accrual chain."""
        n = 0
        for tkr in list(self.qm.resting.keys()):
            if self._handle_skip(tkr, reason, force=True):
                n += 1
            self._break_accrual(tkr, reason)
        return n

    def retire_market(self, ticker: str, reason: str) -> None:
        """Stop managing a market: pull exposure, forget its params."""
        self._handle_skip(ticker, f"retired:{reason}", force=True)
        self.params_by_ticker.pop(ticker, None)
        self.optimal_size_floors.pop(ticker, None)
        self._accrual.pop(ticker, None)
        self.markets = [m for m in self.markets if m.get("market_ticker") != ticker]
        _log.info(f"retired {ticker}: {reason}")

    def retire_inactive_markets(self, db_path: str | None = None) -> list[str]:
        """Discovery freshness (2026-09-20 review): after every discovery
        refresh, drop markets whose program is no longer active — ended,
        paid out, or de-enrolled — instead of quoting them until restart."""
        tickers = list(self.params_by_ticker.keys())
        if not tickers:
            return []
        active: set[str] = set()
        try:
            conn = sqlite3.connect(db_path or settings.DB_PATH, timeout=5.0)
            try:
                for i in range(0, len(tickers), 400):
                    chunk = tickers[i:i + 400]
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT market_ticker FROM lip_programs "
                        f"WHERE market_ticker IN ({marks}) AND {is_active_clause()}",
                        chunk,
                    ).fetchall()
                    active.update(r[0] for r in rows)
            finally:
                conn.close()
        except Exception as e:
            _log.warning(f"retire_inactive_markets: query failed, retiring nothing: {e}")
            return []
        retired = [t for t in tickers if t not in active]
        for t in retired:
            self.retire_market(t, "program_inactive")
        return retired

    @staticmethod
    def _program_window_reason(params: ProgramParams, now: float) -> str | None:
        """Blocking reason when `now` is outside the program window.

        Independent of discovery: the window came with the program, so
        expiry is known the moment it happens (2026-09-20 review)."""
        if params.end_ts is not None and now >= params.end_ts:
            return "program_expired"
        if params.start_ts is not None and now < params.start_ts:
            return "program_not_started"
        return None

    def _discovery_staleness_reason(self, now: float) -> str | None:
        """Blocking reason when no COMPLETE scan is recent enough.

        A failed or partial scan does not advance freshness, so repeated
        failures eventually pull all exposure rather than leaving us
        quoting programs we can no longer verify."""
        max_age = float(getattr(settings, "DISCOVERY_MAX_AGE_SEC", 0) or 0)
        if max_age <= 0:
            return None
        ts = self.last_complete_scan_ts
        if ts is None:
            return "discovery_never_completed"
        if now - ts > max_age:
            return "discovery_stale"
        return None

    def note_discovery(self, result) -> None:
        """Record a discovery scan's completeness for the freshness gate."""
        if getattr(result, "complete", False):
            self.last_complete_scan_ts = getattr(result, "finished_ts", time.time())
        else:
            _log.warning("discovery scan incomplete — freshness not advanced "
                         f"(errors={getattr(result, 'errors', [])[:3]})")

    def refresh_params(self, markets: list[dict]) -> dict:
        """Refresh ProgramParams for markets we ALREADY quote, not just new
        ones (2026-09-20 review).

        A ticker can be re-listed with a different pool, target size,
        discount factor or window. The old update path only handled
        `fresh - current`, so an existing ticker kept its stale parameters
        indefinitely. When the program WINDOW changes the accrual chain is
        reset (a new window means a new pool and a new cumulative cap)."""
        changed = reprogrammed = 0
        for m in markets:
            tkr = m.get("market_ticker")
            old = self.params_by_ticker.get(tkr) if tkr else None
            if old is None:
                continue
            new = _program_params_from_market(m)
            if new == old:
                continue
            new_window = (new.start_ts != old.start_ts or new.end_ts != old.end_ts)
            self.params_by_ticker[tkr] = new
            for i, existing in enumerate(self.markets):
                if existing.get("market_ticker") == tkr:
                    self.markets[i] = m
                    break
            self.optimal_size_floors[tkr] = int(m.get("optimal_size_per_side", 0) or 0)
            changed += 1
            if new_window:
                # Different program on the same ticker: the previous
                # window's accrual and cap must not carry over.
                self._accrual.pop(tkr, None)
                reprogrammed += 1
                _log.warning(f"refresh_params[{tkr}]: NEW PROGRAM WINDOW "
                             f"pool ${old.period_reward_usd:.2f}→${new.period_reward_usd:.2f} "
                             f"target {old.target_size:g}→{new.target_size:g} — accrual reset")
            else:
                _log.info(f"refresh_params[{tkr}]: pool ${old.period_reward_usd:.2f}→"
                          f"${new.period_reward_usd:.2f} target {old.target_size:g}→"
                          f"{new.target_size:g} df {old.discount_factor:g}→{new.discount_factor:g}")
        return {"changed": changed, "reprogrammed": reprogrammed}

    def on_fill(self, ev: FillEvent) -> str:
        """Apply an execution and immediately reconcile dependent state
        (2026-09-20 review).

        A fill changes the depth we have resting, so the accrual share
        recorded at the last snapshot is no longer what the venue is
        paying us. Rather than letting a fully-filled order keep earning
        its old rate until the next scoring event, the chain is BROKEN
        here: nothing is credited for the interval spanning an unobserved
        state change, and the next snapshot starts a fresh chain from a
        re-measured share. Inventory is invalidated so the next sizing
        decision re-reads it."""
        status_order = self.qm.apply_fill(
            ev.order_id, ev.market_ticker, ev.count,
            trade_id=ev.trade_id, side=ev.side,
            price_cents=(int(round(ev.price_cents_exact))
                         if ev.price_cents_exact is not None else None),
            is_taker=ev.is_taker, exchange_ts=ev.exchange_ts,
            subaccount=ev.subaccount,
        )
        status = self.qm.last_fill_status
        self.fill_counts[status] += 1
        if status not in ("applied", "persistence_failed", "untracked"):
            return status        # duplicates change nothing, including accrual
        self._break_accrual(ev.market_ticker, "fill")
        self.qm.inventory.pop(ev.market_ticker, None)
        if not self.qm.paper and status == "applied":
            self.qm.periodic_resync()
        if ev.trade_id and status == "applied":
            from tools.fill_consumers import drain_fill_consumers
            drain_fill_consumers(self.qm.db_path, trade_id=ev.trade_id)
        if status_order is None or status_order.size_contracts <= 0:
            # Nothing of ours left on this side: two-sided presence is gone,
            # so the share is a KNOWN zero from this instant.
            if not self._actually_resting(ev.market_ticker):
                self._note_flat(ev.market_ticker)
        return status

    # ── Accrual chain bookkeeping ─────────────────────────────────────
    def _accrual_for(self, ticker: str, params: ProgramParams) -> AccrualState:
        st = self._accrual.get(ticker)
        if st is None or st.program_key != params.start_ts:
            st = AccrualState(program_key=params.start_ts,
                              accrued_usd=self._seed_accrued(ticker, params))
            self._accrual[ticker] = st
        return st

    def _seed_accrued(self, ticker: str, params: ProgramParams) -> float:
        """Cumulative estimate already written for this program window, so
        the pool cap survives restarts."""
        try:
            conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
            try:
                if params.start_ts is not None:
                    since = datetime.fromtimestamp(params.start_ts, tz=timezone.utc).isoformat()
                    row = conn.execute(
                        "SELECT COALESCE(SUM(estimated_payout_usd), 0) FROM lip_snapshots "
                        "WHERE market_ticker = ? AND captured_at >= ?", (ticker, since)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(estimated_payout_usd), 0) FROM lip_snapshots "
                        "WHERE market_ticker = ?", (ticker,)).fetchone()
                return float(row[0] or 0.0)
            finally:
                conn.close()
        except Exception:
            return 0.0

    def _break_accrual(self, ticker: str, why: str) -> None:
        """State became UNKNOWN (stale book, disconnect): nothing may be
        credited until a fresh observation starts a new chain."""
        st = self._accrual.get(ticker)
        if st is not None and st.last_ts is not None:
            st.last_ts = None
            st.last_share = 0.0
            st.breaks += 1

    def _note_flat(self, ticker: str) -> None:
        """We pulled our orders: from now on the share is a KNOWN zero."""
        st = self._accrual.get(ticker)
        if st is not None:
            st.last_ts = time.time()
            st.last_share = 0.0

    def _accrue(self, st: AccrualState, params: ProgramParams, now: float) -> tuple[float, str]:
        """Dollars earned over [st.last_ts, now) at st.last_share — the
        state we last OBSERVED — clipped to the program window and to the
        cumulative cap. Returns (payout, note)."""
        if st.last_ts is None:
            return 0.0, "no_prior_state"
        gap = now - st.last_ts
        if gap <= 0.0 or gap > self.SNAPSHOT_MAX_INTERVAL_SEC:
            st.breaks += 1
            return 0.0, "gap"
        lo, hi = st.last_ts, now
        if params.start_ts is not None:
            lo = max(lo, params.start_ts)
        if params.end_ts is not None:
            hi = min(hi, params.end_ts)
        dur = hi - lo
        if dur <= 0.0:
            return 0.0, "outside_window"
        raw = interval_payout_usd(st.last_share, params, dur)
        share_cap = max(0.0, min(1.0, float(getattr(settings, "LIP_MAX_ACCOUNT_SHARE_OF_POOL", 1.0))))
        room = max(0.0, params.period_reward_usd * share_cap - st.accrued_usd)
        payout = min(raw, room)
        st.accrued_usd += payout
        return payout, ("capped" if payout < raw else "ok")

    def _skip(self, ticker: str, reason: str):
        """Record why _quote_target_for produced no target and return None."""
        self._skip_reason[ticker] = reason
        return None

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
        """Compute our desired quote using ADAPTIVE sizing to target 25% share.

        Returns None when we should not (re)quote; the reason is recorded in
        self._skip_reason[ticker] so on_book_update can decide whether
        resting orders must be pulled (audit #7)."""
        tkr = book.market_ticker
        self._skip_reason.pop(tkr, None)
        p = self.params_by_ticker.get(tkr)
        if p is None:
            return self._skip(tkr, "no_params")
        # 2026-09-20 audit #1: a book that lost a delta is not a book.
        if getattr(book, "stale", False):
            return self._skip(tkr, "stale_book")
        # 2026-09-20 review: sub-cent price grid — explicitly unsupported.
        if getattr(book, "unsupported_grid", False):
            return self._skip(tkr, "unsupported_grid")
        best_yes = book.best_yes_bid()
        best_no  = book.best_no_bid()
        if best_yes is None or best_no is None:
            return self._skip(tkr, "no_best")

        # A.1 (2026-05-14): cache microprice for downstream consumers
        # (A.2 reservation price, A.4 markout). Best-effort — None when
        # book is crossed/empty; downstream falls back to arithmetic mid.
        if settings.USE_MICROPRICE:
            mp = microprice_yes(book)
            if mp is not None:
                self._last_microprice[book.market_ticker] = (mp, time.time())

        # #104 (2026-04-28) Pre-settlement skip: don't re-quote in last
        # X min before close. Heartbeat already cancelled; this prevents
        # a book update from immediately triggering a fresh placement.
        mins_until = self._minutes_until_settle(book.market_ticker)
        if mins_until is not None and 0 < mins_until <= settings.PRE_SETTLEMENT_CANCEL_MIN:
            return self._skip(tkr, "pre_settlement")

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
            return self._skip(tkr, "volatility")

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
            return self._skip(tkr, "fair_value")

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

        # A.2 (2026-05-14): Avellaneda-Stoikov reservation price layer.
        # Computes r = mp - q×γ×σ²×(T-t) and proposes a per-side tick
        # offset. Only takes effect when AS_RESERVATION_ENABLED=True AND
        # market's DiscountFactor ≥ 0.70 (otherwise size-skew alone).
        # Always logged for diagnostic purposes so paper-mode A/B can show
        # whether the price skew would have changed fill toxicity.
        yes_bid_c = best_yes.price_cents
        no_bid_c  = best_no.price_cents
        as_reason = "off"
        if settings.AS_RESERVATION_ENABLED:
            mp_tuple = self._last_microprice.get(book.market_ticker)
            mp = mp_tuple[0] if mp_tuple is not None else None
            net_q = inv.net_yes_contracts if (inv and inv.net_yes_contracts) else 0
            hist = self._best_history.get(book.market_ticker)
            samples = [h[1] for h in hist] if hist else []
            sigma_c = realized_sigma_cents(samples)
            hours_settle = mins_until / 60.0 if (mins_until is not None) else 24.0
            if mp is not None and sigma_c > 0 and net_q != 0:
                r = reservation_price(
                    mp_cents=mp, net_inventory=net_q,
                    gamma=settings.AS_GAMMA, sigma_cents=sigma_c,
                    hours_to_settle=hours_settle,
                )
                skew = suggest_quote_skew(
                    mp_cents=mp, r_cents=r,
                    discount_factor=float(p.discount_factor),
                    max_tick_offset=1,
                )
                as_reason = skew.reason
                # Apply the offsets (negative = quote 1c worse than best)
                # Clamp to [1, 99] just in case
                if skew.yes_tick_offset:
                    yes_bid_c = max(1, min(99, yes_bid_c + skew.yes_tick_offset))
                if skew.no_tick_offset:
                    no_bid_c  = max(1, min(99, no_bid_c  + skew.no_tick_offset))
                if skew.yes_tick_offset or skew.no_tick_offset:
                    _log.info(f"AS_skew[{book.market_ticker}] mp={mp:.2f} r={r:.2f} "
                              f"q={net_q} σ={sigma_c:.2f}c T={hours_settle:.1f}h "
                              f"yes_off={skew.yes_tick_offset} no_off={skew.no_tick_offset} "
                              f"reason={skew.reason}")

        # 2026-09-20 review (qualification-aware placement): a quote that
        # cannot qualify earns nothing and is pure adverse-selection
        # exposure. Top a side up to the qualify cliff when the cap allows;
        # otherwise do not quote (and pull what is resting).
        q_reason, yes_size_override, no_size_override = self._qualification_adjust(
            book, p, yes_bid_c, no_bid_c, size, yes_size_override, no_size_override,
        )
        if q_reason:
            now_ts = time.time()
            last = self._fv_skip_log_ts.get(f"qual:{tkr}", 0)
            if now_ts - last > 300:
                _log.info(f"{q_reason}[{tkr}] target={p.target_size:g} size={size}")
                self._fv_skip_log_ts[f"qual:{tkr}"] = now_ts
            return self._skip(tkr, q_reason)

        return QuoteTarget(
            market_ticker=book.market_ticker,
            yes_bid_cents=yes_bid_c,
            no_bid_cents=no_bid_c,
            size_contracts=size,
            yes_size_override=yes_size_override,
            no_size_override=no_size_override,
        )

    def _qualification_adjust(self, book: BookState, p: ProgramParams,
                              yes_bid_c: int, no_bid_c: int, size: int,
                              yes_ov: int | None, no_ov: int | None
                              ) -> tuple[str | None, int | None, int | None]:
        """Check both sides of the intended quote against Kalshi's
        qualification rule and return (skip_reason | None, yes_override,
        no_override).

        Per side: the book INCLUDING our quote must reach target_size
        (otherwise the whole snapshot pays nobody) and our price must sit at
        or inside the resulting cutoff. In live mode our own resting depth
        is already inside the public book and is subtracted before the
        target is added, so it is not counted twice. When a side is short
        of the cliff, our size is raised to exactly close the gap, bounded
        by the per-market gross cap; a gap larger than that is
        `unqualifiable:<side>_depth`. A price that would fall beyond the
        cutoff (only possible after an AS/throttle tick-back) is
        `unqualifiable:<side>_beyond_cutoff`."""
        ticker = book.market_ticker
        ours_live = [] if self.qm.paper else self._live_resting(ticker)
        series = ticker.split("-", 1)[0] if ticker else ""
        cap_usd = float(settings.MAX_GROSS_PER_MARKET_BY_SERIES.get(
            series, settings.MAX_GROSS_PER_MARKET_USD))
        out: dict[str, int | None] = {"yes": yes_ov, "no": no_ov}
        for side, levels, price in (("yes", book.yes_bids, yes_bid_c),
                                    ("no", book.no_bids, no_bid_c)):
            our_size = float(out[side] if out[side] is not None else size)
            public: dict[int, float] = {}
            for l in levels:
                public[l.price_cents] = public.get(l.price_cents, 0.0) + float(l.size)
            for o in ours_live:
                if o.side == side and o.price_cents in public:
                    public[o.price_cents] = max(0.0, public[o.price_cents] - float(o.size_contracts))
            public_depth = sum(public.values())
            if public_depth + our_size < p.target_size:
                required = p.target_size - public_depth
                max_side = int((cap_usd / 2.0) * 100.0 / max(1, price))
                if required > max_side:
                    return f"unqualifiable:{side}_depth", None, None
                our_size = float(math.ceil(required))
                out[side] = int(our_size)
            merged = dict(public)
            merged[price] = merged.get(price, 0.0) + our_size
            aug = sorted([BookLevel(pc, sz) for pc, sz in merged.items() if sz > 1e-9],
                         key=lambda l: -l.price_cents)
            cutoff = _find_cutoff_price(aug, p.target_size)
            if cutoff is None or price < cutoff:
                return f"unqualifiable:{side}_beyond_cutoff", None, None
        return None, out["yes"], out["no"]

    def _exposure_gate(self, book: BookState) -> str | None:
        """Exposure gates that run on EVERY book event, ahead of any scoring
        or reprice throttle (2026-09-20 review, critical finding).

        A stale / off-grid / blacklisted / retired market must pull its
        resting orders the moment we learn about it. The 1-second scoring
        throttle used to sit in front of these checks, so a stale
        notification arriving right after a normal update was silently
        dropped and the orders stayed on the venue. Returns the blocking
        reason, or None when the event may proceed to scoring."""
        tkr = book.market_ticker
        if self.qm.uncertain_markets:
            self._handle_skip(tkr, "order_state_uncertain", force=True)
            self._break_accrual(tkr, "order_state_uncertain")
            return "order_state_uncertain"
        self._refresh_blacklist()
        if self._is_blacklisted(tkr):
            self._handle_blacklisted(tkr)
            return "blacklist"
        params = self.params_by_ticker.get(tkr)
        if params is None:
            # Market dropped from our set (program ended / de-allocated):
            # anything still resting there is unmonitored exposure.
            self._handle_skip(tkr, "no_params", force=True)
            return "no_params"
        # 2026-09-20 review: program boundaries and discovery freshness are
        # enforced HERE, on every event, not only when a discovery cycle
        # happens to run. A program that ended 15 minutes ago must not wait
        # up to 30 minutes for the next scan to have its exposure pulled.
        window = self._program_window_reason(params, time.time())
        if window is not None:
            self._handle_skip(tkr, window, force=True)
            self._break_accrual(tkr, window)
            return window
        stale_scan = self._discovery_staleness_reason(time.time())
        if stale_scan is not None:
            self._handle_skip(tkr, stale_scan, force=True)
            self._break_accrual(tkr, stale_scan)
            return stale_scan
        if getattr(book, "unsupported_grid", False):
            self._handle_skip(tkr, "unsupported_grid", force=True)
            self._break_accrual(tkr, "unsupported_grid")
            return "unsupported_grid"
        if getattr(book, "stale", False):
            self._handle_skip(tkr, "stale_book", force=True)
            self._break_accrual(tkr, "stale_book")
            return "stale_book"
        return None

    async def on_book_update(self, book: BookState):
        """Called by WS on every book change."""
        now = time.time()
        tkr = book.market_ticker
        if self._exposure_gate(book) is not None:
            return  # nothing about this book can be trusted; no snapshot

        # Throttle SCORING / REPRICING to ~1/sec per market. Nothing below
        # this line may be the only thing standing between us and a cancel.
        if now - self.last_score_ts[tkr] < 1.0:
            return
        self.last_score_ts[tkr] = now
        params = self.params_by_ticker[tkr]

        # Compute our target quote
        target = self._quote_target_for(book)
        if target is None:
            reason = self._skip_reason.get(tkr, "unknown")
            self._handle_skip(tkr, reason)
            # Score honestly whatever is (or isn't) still resting, but do
            # not reprice.
            scored = self._score_market(book, params, target=None)
            self._record_score(scored, now, feed_sizer=True)
            self._persist_snapshot(tkr, scored, params, now)
            return

        # 2026-09-20 audit #3: score what is ACTUALLY resting (or the target
        # hypothetically when nothing is), persist with real payout units,
        # feed the sizer only from actual presence. Same path as heartbeat.
        scored = self._score_market(book, params, target=target)
        self._record_score(scored, now, feed_sizer=True)
        self._persist_snapshot(book.market_ticker, scored, params, now)

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
            result = await loop.run_in_executor(None, self.qm.reconcile, target)
        self.reconciles[ticker] += 1
        # 2026-09-20 audit #7: a risk veto inside reconcile (Sentinel,
        # blacklist, full throttle) used to return {"action": "skip"} and
        # leave whatever was resting untouched. Route it through the same
        # exposure-pulling path as a skipped target. Capacity gates
        # (gross/net/bankroll caps, spread, min size) are NOT vetoes —
        # they refuse to add, not to keep.
        try:
            if isinstance(result, dict) and result.get("action") == "skip":
                why = str(result.get("reason", ""))
                if why.startswith(("SENTINEL", "BLACKLIST", "THROTTLE: size_scale=0")):
                    self._handle_skip(ticker, f"risk_veto:{why.split(':', 1)[0]}")
            elif isinstance(result, dict) and (result.get("placed") or result.get("cancelled")):
                # Resting state changed: the share credited FORWARD from
                # here must be the post-reconcile one, not the pre-reconcile
                # observation persisted a moment ago.
                st = self._accrual.get(ticker)
                if st is not None and st.last_ts is not None:
                    st.last_share = self._score_market(book, params).share
        except Exception as e:
            _log.debug(f"post-reconcile bookkeeping failed for {ticker}: {e}")

    async def heartbeat_snapshot_loop(self, ws, interval_sec: int = 30):
        """Periodically snapshot all quoted markets even if book hasn't updated.
        This ensures lip_snapshots is populated across ALL active markets, not
        just the noisy ones. Critical for honest share-estimation across the
        full paper portfolio.
        """
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
                # 2026-09-20 review: exposure gates run here INDEPENDENTLY
                # of scoring — a market that is skipped for scoring must
                # still have its orders pulled.
                # Orphan sweep: resting orders on tickers we no longer manage.
                for tkr in list(self.qm.resting.keys()):
                    if tkr not in self.params_by_ticker:
                        self._handle_skip(tkr, "no_params", force=True)
                ws_up = bool(getattr(ws, "connected", True))
                stale_scan = self._discovery_staleness_reason(now)
                for tkr, params in list(self.params_by_ticker.items()):
                    if self._is_blacklisted(tkr):
                        self._handle_blacklisted(tkr)
                        continue
                    if self.qm.uncertain_markets:
                        self._handle_skip(tkr, "order_state_uncertain", force=True)
                        self._break_accrual(tkr, "order_state_uncertain")
                        continue
                    if not ws_up:
                        self._handle_skip(tkr, "ws_disconnect", force=True)
                        self._break_accrual(tkr, "ws_disconnect")
                        continue
                    # Expiry is independent of both scoring and discovery:
                    # a program that ended is cancelled here even if its
                    # book has gone quiet and no scan has run since.
                    window = self._program_window_reason(params, now)
                    if window is not None:
                        self._handle_skip(tkr, window, force=True)
                        self._break_accrual(tkr, window)
                        continue
                    if stale_scan is not None:
                        self._handle_skip(tkr, stale_scan, force=True)
                        self._break_accrual(tkr, stale_scan)
                        continue
                    book = ws.books.get(tkr)
                    if book is None:
                        continue
                    if getattr(book, "unsupported_grid", False):
                        self._handle_skip(tkr, "unsupported_grid", force=True)
                        self._break_accrual(tkr, "unsupported_grid")
                        continue
                    if getattr(book, "stale", False):
                        self._handle_skip(tkr, "stale_book", force=True)
                        self._break_accrual(tkr, "stale_book")
                        continue
                    best_yes = book.best_yes_bid()
                    best_no = book.best_no_bid()
                    if best_yes is None or best_no is None:
                        continue
                    # A.4 (2026-05-14): persist microprice + book to history
                    # so markout_logger can compute t+1/10/60s markouts on
                    # fills retrospectively. Lightweight — one row per ticker
                    # per heartbeat (≈30s); skipped when microprice undefined.
                    if settings.USE_MICROPRICE:
                        mp = microprice_yes(book)
                        if mp is not None:
                            try:
                                from monitor.markout_logger import record_book_snapshot
                                # Derive yes_ask from explicit or no_bid mirror
                                yes_ask_lvl = book.best_yes_ask()
                                if yes_ask_lvl is not None:
                                    ask_c = yes_ask_lvl.price_cents
                                    ask_sz = yes_ask_lvl.size
                                else:
                                    ask_c = 100 - best_no.price_cents
                                    ask_sz = best_no.size
                                record_book_snapshot(
                                    tkr, mp,
                                    best_bid_c=best_yes.price_cents,
                                    best_ask_c=ask_c,
                                    bid_size=best_yes.size,
                                    ask_size=ask_sz,
                                )
                            except Exception as e:
                                _log.debug(f"markout_logger snapshot failed for {tkr}: {e}")
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
                    # 2026-09-20 audit #3: same scoring path as the book
                    # handler — ACTUAL resting orders, no hypothetical size.
                    scored = self._score_market(book, params, target=None)
                    self._record_score(scored, now, feed_sizer=False)
                    self._persist_snapshot(tkr, scored, params, now)

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
                # 2026-05-18: per-market net-capture calibration now used in place of
                # the flat 0.25 prior. Each market's pool is multiplied by its own
                # kalshi_calib_for() value (rebate-minus-adverse-cost EWMA from
                # market_calibration). Fallback to 0.25 only when n_samples < 5.
                try:
                    from cross_venue.yield_equation import kalshi_calib_for
                    qm_summary = self.qm.summary() if hasattr(self.qm, "summary") else {}
                    cap_deployed = (qm_summary.get("total_gross_usd")
                                    or qm_summary.get("gross_usd") or 0)
                    quoted_tickers = set(self.qm.resting.keys()) if hasattr(self.qm, "resting") else set()
                    proj_max = 0.0
                    proj_real = 0.0
                    pool_learned = 0.0
                    for m in self.markets:
                        if m["market_ticker"] not in quoted_tickers:
                            continue
                        pool = float(m["reward_per_day_usd"])
                        c = kalshi_calib_for(m["market_ticker"])
                        proj_max += pool
                        proj_real += pool * c
                        if abs(c - 0.25) > 1e-6:
                            pool_learned += pool
                    avg_calib = (proj_real / proj_max) if proj_max > 0 else 0.0
                    learned_pct = (pool_learned / proj_max * 100) if proj_max > 0 else 0.0
                    yield_pct = (proj_real / cap_deployed * 100) if cap_deployed > 0 else 0.0
                    _log.info(
                        f"💰 MONEY_PRINT: cap=${cap_deployed:.2f} "
                        f"proj_daily=${proj_real:.2f} "
                        f"(theo_max=${proj_max:.0f}, avg_calib={avg_calib:.3f}, "
                        f"learned_pool={learned_pct:.0f}% of {len(quoted_tickers)} mkts) "
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
        if self.skip_counts:
            top = sorted(self.skip_counts.items(), key=lambda kv: -kv[1])[:6]
            print("  Quote skips: " + ", ".join(f"{k}={v}" for k, v in top))
        if self.fill_counts:
            print("  Fills: " + ", ".join(f"{k}={v}" for k, v in sorted(self.fill_counts.items())))
        scan_age = ("never" if self.last_complete_scan_ts is None
                    else f"{time.time() - self.last_complete_scan_ts:.0f}s ago")
        print(f"  Last complete discovery scan: {scan_age}")
        st = self.account.state()
        print(f"  Account: cash ${st.cash_usd:.2f}  reserved ${st.reserved_usd:.2f}  "
              f"available ${st.available_usd:.2f}  inventory-at-cost "
              f"${st.inventory_cost_usd:.2f}  ({st.n_reservations} holds)")
        if self.qm.capital_refusals:
            print(f"  Orders refused for insufficient capital: {self.qm.capital_refusals}")
        # Reward figures here are ESTIMATES. Reconciled payments are the only
        # reward money (engine/reward_provenance.py); tools/reward_payments.py
        # reports what is actually on record.
        print("  NOTE: reward figures above are model estimates, not payments.")
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
    use_capital_alloc = os.getenv("LIP_USE_CAPITAL_ALLOC", "true").lower() == "true"
    def _select_markets() -> list[dict]:
        saturated = _compute_saturated_tickers()
        if use_capital_alloc:
            sel = select_optimal_portfolio(
                budget_usd=settings.MAX_TOTAL_GROSS_USD,
                exclude_tickers=saturated,
            )
            _log.info(f"capital-aware ranker: {len(sel)} markets, "
                      f"E[net]=${sum(m.get('expected_net_per_day',0) for m in sel):.2f}/d")
            # Sprint 4 #2: pre-deploy depth gate (reject <5% projected share)
            if os.getenv("DEPTH_GATE_ENABLED", "true").lower() == "true":
                try:
                    from execution.kalshi_auth import KalshiClient as _KC_dg
                    _dg_client = _KC_dg()
                    pre_n = len(sel)
                    sel = filter_by_depth(
                        sel, _dg_client,
                        min_share=float(os.getenv("DEPTH_GATE_MIN_SHARE", "0.05")),
                        exit_gate_enabled=os.getenv("EXIT_GATE_ENABLED", "true").lower() == "true",
                        min_exit_contracts=int(os.getenv("MIN_EXIT_CONTRACTS", "50")),
                        min_exit_price_cents=int(os.getenv("MIN_EXIT_PRICE_CENTS", "5")),
                    )
                    _log.info(f"depth_gate: {pre_n} -> {len(sel)} markets "
                              f"({100*(pre_n-len(sel))/max(1,pre_n):.0f}% rejected)")
                except Exception as e:
                    _log.warning(f"depth_gate skipped due to error: {e}")
            return sel
        return top_n_to_quote(top_n, exclude_tickers=saturated)
    markets = _select_markets()
    # 2026-05-13: clean-exit storm fix. Empty universe used to `return` →
    # exit 0 → systemd Restart loop with no OnFailure visibility (~36 cycles
    # in 48 min on 01:04-01:52 UTC). Idle and retry instead; escalate to
    # exit 2 after MAX_WAIT so the supervisor's flap detector + OnFailure
    # both fire loudly. See settings.EMPTY_UNIVERSE_*.
    consecutive_empty = 0
    while not markets:
        _log.warning(f"empty quotable universe (cycle {consecutive_empty}); "
                     f"idling {settings.EMPTY_UNIVERSE_SLEEP_SEC}s")
        await asyncio.sleep(settings.EMPTY_UNIVERSE_SLEEP_SEC)
        consecutive_empty += 1
        if consecutive_empty * settings.EMPTY_UNIVERSE_SLEEP_SEC >= settings.EMPTY_UNIVERSE_MAX_WAIT_SEC:
            _log.error(f"empty universe for >={settings.EMPTY_UNIVERSE_MAX_WAIT_SEC}s — "
                       "exiting non-zero so supervisor visibility kicks in")
            sys.exit(2)
        try:
            discover(save=True)
        except Exception as e:
            _log.warning(f"discover refresh during empty-universe idle failed: {e}")
        markets = _select_markets()
    _log.info(f"quoting top-{len(markets)} markets, total pool ${sum(m['reward_per_day_usd'] for m in markets):.2f}/day")

    runner = PaperRunner(markets)
    ws = KalshiWS()
    await ws.connect()
    ws.on_update(runner.on_book_update)

    # 2026-09-20 review: the socket dropping means every book is stale and
    # every resting order is unmanaged. Pull exposure IMMEDIATELY (before
    # the reconnect sleep), not on the next scoring tick.
    async def _on_ws_disconnect(tickers: list[str]) -> None:
        loop_ = asyncio.get_running_loop()
        n = await loop_.run_in_executor(None, runner.pull_all_exposure, "ws_disconnect")
        _log.warning(f"WS disconnect: pulled resting orders on {n} tickers "
                     f"({len(tickers)} subscribed)")
    ws.on_disconnect(_on_ws_disconnect)

    # 2026-04-22 (Architect audit): refresh resting state on reconnect so
    # reconcile() doesn't skip placement against phantom entries.
    # 2026-09-20 review: in live mode take the VENUE's truth (resync) rather
    # than blindly dropping memory — a blind drop followed by placement
    # would duplicate any order the disconnect-cancel failed to reach.
    async def _on_ws_reconnect(tickers: list[str]) -> None:
        if runner.qm.paper:
            for t in tickers:
                runner.qm.reset_for_market(t)
            _log.warning(f"WS reconnect: purged paper resting state for {len(tickers)} tickers")
        else:
            res = await asyncio.get_running_loop().run_in_executor(None, runner.qm.periodic_resync)
            _log.warning(f"WS reconnect: resynced resting state from venue: {res}")
    ws.on_reconnect(_on_ws_reconnect)

    # Private fill channel keeps resting sizes honest between resyncs and
    # invalidates accrual/inventory immediately (2026-09-20 review).
    async def _on_fill(ev: FillEvent) -> None:
        await asyncio.get_running_loop().run_in_executor(None, runner.on_fill, ev)
    ws.on_fill(_on_fill)

    await ws.subscribe_orderbook([m["market_ticker"] for m in markets])
    if not runner.qm.paper:
        await ws.subscribe_fills()

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
    def _discover_and_select() -> tuple[object, list[dict]]:
        """ALL blocking work for one discovery cycle: the /incentive_programs
        scan, the saturation probe, ranking and the depth gate. Runs in an
        executor thread — 2026-09-20 review: doing this inline stalled the WS
        feed (and therefore every cancellation) for the duration."""
        res = discover_result(save=True)
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
        return res, fresh

    async def _periodic_discover(interval_sec: int = 1800):
        """Refresh LIP programs + subscribe newly enrolled markets."""
        await asyncio.sleep(interval_sec)  # initial wait; startup already did it
        while not stop.is_set():
            try:
                loop_ = asyncio.get_running_loop()
                res, fresh = await loop_.run_in_executor(None, _discover_and_select)
                # Freshness: only a COMPLETE scan advances the clock. A
                # partial/failed scan leaves the gate to expire on its own.
                runner.note_discovery(res)
                if not getattr(res, "complete", False):
                    _log.warning("periodic_discover: incomplete scan — not retiring or "
                                 "re-ranking on its evidence")
                    await asyncio.sleep(interval_sec)
                    continue
                # 2026-09-20 review (discovery freshness): programs that
                # ended / paid out / got de-enrolled since the last refresh
                # are retired now, not at the next restart.
                retired = await loop_.run_in_executor(None, runner.retire_inactive_markets)
                if retired:
                    _log.warning(f"periodic_discover: retired {len(retired)} inactive "
                                 f"markets: {retired[:5]}")
                # Existing tickers get REFRESHED parameters, not just new ones.
                upd = runner.refresh_params(fresh)
                if upd["changed"]:
                    _log.info(f"periodic_discover: refreshed params for {upd['changed']} "
                              f"existing markets ({upd['reprogrammed']} new program windows)")
                current_tickers = set(runner.params_by_ticker.keys())
                fresh_tickers = {m["market_ticker"] for m in fresh}
                new_tickers = fresh_tickers - current_tickers
                if new_tickers:
                    _log.info(f"periodic_discover: {len(new_tickers)} new markets to subscribe")
                    for m in fresh:
                        tkr = m["market_ticker"]
                        if tkr in new_tickers:
                            runner.params_by_ticker[tkr] = _program_params_from_market(m)
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
