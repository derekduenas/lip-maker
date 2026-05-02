"""Polymarket auto-quoter — main loop (analogous to lip-maker run_paper.py).

ARCHITECTURE:
  Every CYCLE_SEC seconds:
    1. Refresh top-N markets from us_scanner (ranked by EV)
    2. Pull current BBO for each
    3. Build at-best two-sided quote per market
    4. Apply safety gates (per-market cap, total cap, qualification)
    5. Reconcile via PMQuoteManager (place/cancel as needed)
    6. Sleep, repeat

PAPER vs LIVE:
  PM_PAPER=true (default): all orders go to /v1/order/preview
                            (server validates, no money moves)
  PM_PAPER=false:          orders.create() — REAL money

USAGE:
  python run_pm.py                 # paper mode, top 5, $10/market cap
  python run_pm.py --top 10
  python run_pm.py --max-per-market 25 --total-cap 200
  PM_PAPER=false python run_pm.py  # LIVE — be careful

SYSTEMD:
  Deployed as polymarket-maker.service. Edit
  /etc/systemd/system/polymarket-maker.service to change params.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS
from execution.pm_quote_manager import PMQuoteManager, QuoteTarget
from execution.pm_ws import PMBookStream, book_age_sec
from engine.rewards_schedule import expected_daily_reward
from config import settings

_log = logging.getLogger(__name__)

# Defaults — tune via CLI
DEFAULT_CYCLE_SEC      = 30      # how often to repoll + reconcile
DEFAULT_TOP_N          = 5       # how many markets to quote
DEFAULT_MAX_PER_MARKET = 10.0    # USD cap per market
DEFAULT_TOTAL_CAP      = 50.0    # USD total open exposure
DEFAULT_MIN_QTY        = 5       # contracts


class Runner:
    def __init__(self, *, top_n: int, max_per_market: float, total_cap: float,
                 cycle_sec: int, min_qty: int):
        _key_id = os.getenv("PM_API_KEY_ID")
        _secret = (PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip()
        self.client = PolymarketUS(key_id=_key_id, secret_key=_secret)
        self.qm = PMQuoteManager(self.client, paper=settings.PAPER_MODE)
        self.top_n           = top_n
        self.max_per_market  = max_per_market
        self.total_cap       = total_cap
        self.cycle_sec       = cycle_sec
        self.min_qty         = min_qty
        self.cycles          = 0
        self.start_time      = time.time()
        self._stop           = False
        # WebSocket book stream for real-time updates
        self._ws_keys        = (_key_id, _secret)
        self.ws_stream       = PMBookStream(key_id=_key_id, secret_key=_secret)
        self._ws_started     = False
        self._ws_subscribed_slugs: set[str] = set()
        # Daily-loss circuit breaker (live mode). Tracks realized PnL since
        # 00:00 UTC; halts NEW orders if MAX_DAILY_LOSS_USD breached.
        self._day_start_balance: float | None = None
        self._day_start_iso     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._breaker_tripped   = False
        self._last_balance: float | None = None
        self._last_balance_at   = 0.0
        self._last_balance_err_at = 0.0
        self._cap_multiplier   = 1.0
        self._breaker_tier     = "normal"

        # 2026-05-01 PREDATOR: per-slug net YES position cache for inventory
        # skew. Refreshed once per cycle. Maps slug → int(net_yes_qty).
        # outcome="Yes" netPos = +n (long YES), outcome="No" netPos = +n (long NO = short YES).
        self._positions_cache: dict[str, int] = {}
        self._positions_cache_at = 0.0

    def request_stop(self) -> None:
        self._stop = True

    def _refresh_positions_cache(self) -> None:
        """Pull live positions, compute net_yes per slug. 5s TTL cache."""
        now = time.time()
        if (now - self._positions_cache_at) < 5.0:
            return
        try:
            r = self.client.portfolio.positions()
            positions = r.get("positions", {}) if isinstance(r, dict) else {}
            new_cache: dict[str, int] = {}
            for slug, p in positions.items():
                try:
                    net = int(float(p.get("netPosition", 0)))
                    if net == 0:
                        continue
                    outcome = (p.get("marketMetadata", {}) or {}).get("outcome", "Yes")
                    # outcome="Yes" + long = long YES; outcome="No" + long = long NO (= short YES)
                    if outcome == "Yes":
                        new_cache[slug] = net
                    else:
                        new_cache[slug] = -net
                except Exception:
                    continue
            self._positions_cache = new_cache
            self._positions_cache_at = now
        except Exception as e:
            _log.debug(f"positions refresh failed: {e}")

    def _persist_snapshots(self, rows: list[dict]) -> None:
        """Write per-market scoring snapshots to pm_snapshots (#2 observability)."""
        import sqlite3
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
            try:
                for r in rows:
                    q_min = min(r["our_yes_score"], r["our_no_score"])
                    conn.execute(
                        """INSERT INTO pm_snapshots
                           (market_slug, captured_at, midpoint,
                            yes_bid_size, no_bid_size,
                            our_yes_score, our_no_score, q_min,
                            snapshot_valid, estimated_payout_usd, was_resting)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (r["slug"], now_iso, r.get("midpoint"),
                         r["yes_bid_size"], r["no_bid_size"],
                         r["our_yes_score"], r["our_no_score"], q_min,
                         1 if r.get("qualifies") else 0,
                         (r.get("expected_usd") or 0) / 86400,
                         r.get("was_resting", 0)),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning(f"snapshot persist failed: {e}")

    def discover_targets(self) -> list[dict]:
        """Pull top-N rewards-eligible markets via us_scanner logic.

        v2 scoring (2026-04-29): markets sorted by SOONEST-SETTLING first
        (not most-recent created), so the rate-limited 60-market book scan
        prioritizes short-dated markets where adverse-cost is bounded.
        Long-dated political binaries (Trump 2026 etc.) get crushed by
        time-decay × adverse-cost in score_setup and are correctly ignored.
        """
        from tools.us_scanner import list_active_events, extract_markets, fetch_book_metrics, score_setup
        events = list_active_events(self.client, limit=200)
        markets = extract_markets(events)

        # Sort by soonest-settling first (defensive — strings sort as ISO dates)
        def _end(m):
            return m.get("endDate") or m.get("_event_endDate") or "9999"
        markets.sort(key=_end)

        scored = []
        for m in markets[:150]:  # 2026-04-30: bumped 60→150 — many tomorrow-settling markets have NO BOOK yet, need wider window
            book = fetch_book_metrics(self.client, m["slug"])
            if not book:
                continue
            s = score_setup(m, book, our_size=int(self.max_per_market / max(0.05, book.get("midpoint", 0.5))))
            if s and s.get("expected_usd", 0) > 0:
                scored.append((m, book, s))
        # v2: rank by adjusted EV (already incorporates time-decay + adverse cost)
        scored.sort(key=lambda x: -x[2]["expected_usd"])
        return scored[:self.top_n]

    def _ws_overlay(self, slug: str, rest_book: dict) -> tuple[dict, str]:
        """Prefer fresh WS book, fall back to REST. Returns (book, source)."""
        ws_book = self.ws_stream.books.get(slug)
        if ws_book and book_age_sec(ws_book) <= settings.STALE_DATA_PULL_SECONDS:
            ws_bid = ws_book.best_yes_bid()
            ws_ask = ws_book.best_yes_ask()
            if ws_bid is not None and ws_ask is not None:
                merged = dict(rest_book)
                merged["best_bid"] = ws_bid
                merged["best_ask"] = ws_ask
                merged["top_bid_size"] = ws_book.top_bid_size()
                merged["top_ask_size"] = ws_book.top_ask_size()
                return merged, f"ws({book_age_sec(ws_book):.1f}s)"
        return rest_book, "rest"

    def build_target(self, market: dict, book: dict) -> tuple[QuoteTarget | None, str]:
        """Build at-best two-sided quote target.

        Sizing math: for a binary market, our two-sided exposure is at
        most qty × max(yes_price, no_price) because at most one leg fills
        at the high price (the other leg's price = 1 - high). Conservative:
        size by qty × 1.0 = max_per_market gives upper bound on exposure.

        Returns (target_or_None, reason). reason is "ok" or rejection cause.
        """
        slug = market["slug"]
        # WS-first pricing with REST fallback (audit fix #1: stale-book guard)
        book, src = self._ws_overlay(slug, book)
        yes_bid = book.get("best_bid")
        yes_ask = book.get("best_ask")
        if yes_bid is None or yes_ask is None:
            return None, "no book"
        if yes_bid <= 0 or yes_ask >= 1 or yes_bid >= yes_ask:
            return None, f"degenerate book bid={yes_bid} ask={yes_ask}"
        # AUDIT FIX #2: spread sanity. Wide spreads = adverse-selection trap.
        spread = yes_ask - yes_bid
        if spread > settings.MAX_SPREAD_FRACTION:
            return None, f"spread {spread:.3f} > max {settings.MAX_SPREAD_FRACTION:.3f}"
        no_bid_implied = 1.0 - yes_ask
        worst_per_contract = max(yes_bid, no_bid_implied)
        max_qty = int(self.max_per_market / max(0.05, worst_per_contract))
        qty = max(self.min_qty, max_qty)
        if qty < self.min_qty:
            return None, f"qty<{self.min_qty}"

        # 2026-05-01 PREDATOR — Active inventory skew (mirror of Kalshi #97).
        # When holding net YES position, boost NO-side qty to absorb shorts
        # and bleed off the long. Skew bounded by abs(net) so we don't
        # over-skew on small imbalances.
        yes_qty_override = None
        no_qty_override  = None
        net_yes = self._positions_cache.get(slug, 0)
        if net_yes != 0:
            skew = min(int(qty * settings.INVENTORY_SKEW_FRACTION), abs(net_yes))
            if net_yes > 0:
                # Long YES — boost NO bid, shrink YES bid
                no_qty_override  = qty + skew
                yes_qty_override = max(self.min_qty, qty - skew)
            else:
                # Long NO — boost YES bid, shrink NO bid
                yes_qty_override = qty + skew
                no_qty_override  = max(self.min_qty, qty - skew)

        return QuoteTarget(
            slug=slug,
            yes_price=round(yes_bid, 3),
            no_price=round(no_bid_implied, 3),
            quantity=qty,
            yes_qty_override=yes_qty_override,
            no_qty_override=no_qty_override,
        ), f"ok({src})"

    def _check_circuit_breaker(self) -> bool:
        """AUDIT FIX #4: enforce MAX_DAILY_LOSS_USD via account balance delta.
        Returns True if breaker tripped (halt new orders). Live mode only.

        Rate-limit aware (smoke test 2026-04-29 hit Polymarket 429 on
        /balances after ~10 cycles): fetch at most every 300s during
        normal operation, AND back off ≥60s after any failure. Baseline
        is primed at startup via prime_baseline() so the cycle loop
        never blocks on first-call latency."""
        if settings.PAPER_MODE:
            return False
        # Reset baseline on UTC day rollover
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_start_iso:
            self._day_start_balance = None
            self._day_start_iso = today
            self._breaker_tripped = False
            self._last_balance = None
            self._last_balance_at = 0.0
            self._last_balance_err_at = 0.0

        POLL_SEC      = 300  # 5min between successful refreshes
        ERR_BACKOFF   = 60   # min wait after a failed call
        now = time.time()
        cur = self._last_balance
        last_ok  = self._last_balance_at
        last_err = getattr(self, "_last_balance_err_at", 0.0)
        # Decide whether to attempt fetch this cycle
        time_since_ok  = now - last_ok
        time_since_err = now - last_err
        should_fetch = (cur is None and time_since_err > ERR_BACKOFF) \
                       or (cur is not None and time_since_ok > POLL_SEC
                           and time_since_err > ERR_BACKOFF)
        if should_fetch:
            try:
                b = self.client.account.balances()
                cur = float(b.get("balances", [{}])[0].get("currentBalance", 0))
                self._last_balance = cur
                self._last_balance_at = now
            except Exception as e:
                self._last_balance_err_at = now
                _log.warning(f"breaker: balance fetch failed (backoff {ERR_BACKOFF}s): {str(e)[:100]}")

        # No baseline yet → fail closed
        if cur is None:
            return True

        if self._day_start_balance is None:
            self._day_start_balance = cur
            _log.info(f"breaker: day baseline set ${cur:.2f}")
            self._cap_multiplier = 1.0
            return False
        loss = self._day_start_balance - cur

        # TIERED BREAKER (autonomous bleed response):
        #   loss < WARN_USD              → normal operation
        #   WARN ≤ loss < THROTTLE_USD   → halve caps, log diagnosis
        #   THROTTLE ≤ loss < HALT_USD   → quarter caps, cancel widest-spread orders
        #   loss ≥ HALT_USD              → full halt + cancel_all
        WARN_USD     = settings.MAX_DAILY_LOSS_USD * 0.30   # ~$4 on $13.50 cap
        THROTTLE_USD = settings.MAX_DAILY_LOSS_USD * 0.60   # ~$8
        HALT_USD     = settings.MAX_DAILY_LOSS_USD          # $13.50
        prev_tier = getattr(self, "_breaker_tier", "normal")
        if loss >= HALT_USD:
            tier = "halt"
            self._cap_multiplier = 0.0
        elif loss >= THROTTLE_USD:
            tier = "throttle"
            self._cap_multiplier = 0.25
        elif loss >= WARN_USD:
            tier = "warn"
            self._cap_multiplier = 0.5
        else:
            tier = "normal"
            self._cap_multiplier = 1.0
        self._breaker_tier = tier

        if tier != prev_tier:
            _log.warning(f"⚠ breaker tier {prev_tier}→{tier}: loss=${loss:.2f}, "
                         f"cap_mul={self._cap_multiplier:.2f}")
            if tier in ("throttle", "halt"):
                self._diagnose_bleed(loss)

        if tier == "halt" and not self._breaker_tripped:
            self._breaker_tripped = True
            _log.error(f"🔴 CIRCUIT BREAKER TRIPPED: daily loss ${loss:.2f} > "
                       f"cap ${HALT_USD:.2f}. Halting NEW orders. Cancelling resting.")
            try:
                self.qm.cancel_all()
            except Exception as e:
                _log.warning(f"breaker cancel-all failed: {e}")
        return self._breaker_tripped

    def _diagnose_bleed(self, loss: float) -> None:
        """When breaker escalates, log WHY: which positions are open, what
        recent fills happened, top loss-attributable slugs. Goes to log so
        morning review knows the story."""
        try:
            pos_resp = self.client.portfolio.positions()
            positions = pos_resp.get("positions", {}) if isinstance(pos_resp, dict) else {}
            losers: list[tuple[str, float]] = []
            for slug, p in positions.items():
                # Each position dict has 'costBasis' and 'marketValue' or similar
                try:
                    cost = float(p.get("costBasis", 0))
                    mv   = float(p.get("marketValue", 0))
                    pnl  = mv - cost
                    if pnl < 0:
                        losers.append((slug, pnl))
                except Exception:
                    continue
            losers.sort(key=lambda x: x[1])
            top3 = losers[:3]
            _log.error(f"BLEED DIAGNOSIS: total_loss=${loss:.2f}, "
                       f"open_positions={len(positions)}, "
                       f"top losers: {[(s[:40], f'${p:.2f}') for s,p in top3]}")
            # Auto-cancel resting orders on top losing slugs (don't add fuel)
            for slug, pnl in top3:
                try:
                    n = self.qm.cancel_all(slug=slug)
                    if n > 0:
                        _log.warning(f"  bleed-cancel: {n} orders on {slug[:50]}")
                except Exception as e:
                    _log.warning(f"  bleed-cancel err on {slug[:30]}: {e}")
        except Exception as e:
            _log.warning(f"bleed diagnose failed: {e}")

    def prime_baseline(self) -> bool:
        """One-shot baseline fetch at startup with patient retry. Returns
        True if baseline acquired, False otherwise. Cheap to call —
        sleeps between attempts so we don't hammer rate limits."""
        if settings.PAPER_MODE:
            return True
        attempts = 5
        for i in range(attempts):
            try:
                b = self.client.account.balances()
                cur = float(b.get("balances", [{}])[0].get("currentBalance", 0))
                self._last_balance = cur
                self._last_balance_at = time.time()
                _log.info(f"breaker: baseline primed ${cur:.2f} (attempt {i+1})")
                return True
            except Exception as e:
                wait = 30 * (i + 1)  # 30, 60, 90, 120, 150
                _log.warning(f"prime baseline attempt {i+1}/{attempts} failed "
                             f"({str(e)[:80]}), wait {wait}s")
                if i < attempts - 1:
                    time.sleep(wait)
        _log.error("prime_baseline FAILED — live mode will block until next refresh")
        return False

    def passes_gates(self, target: QuoteTarget) -> tuple[bool, str]:
        """Safety gates. Use WORST-CASE leg cost (only one side actually fills).

        For binary markets: max(yes_price, no_price) × qty = max possible
        exposure if one leg gets fully filled.
        """
        # FAIR-VALUE GATE (#3 fix): for crypto markets, refuse to quote if
        # spot is materially mispriced relative to strike. Adverse-selection
        # defense — informed flow on a near-settled crypto bet wipes out
        # weeks of rebate in one fill.
        try:
            from engine.pm_fair_value import fair_value_skip
            fv_skip = fair_value_skip(target.slug,
                                       target.yes_price or 0,
                                       target.no_price or 0)
            if fv_skip:
                return False, fv_skip
        except Exception:
            pass  # don't block on infra issue

        existing = self.qm.resting.get(target.slug, [])
        # Worst-case if both NEW orders fully fill (rarely both, but bound it)
        new_max = max(target.yes_price or 0, target.no_price or 0) * target.quantity
        # Apply breaker tier cap multiplier (1.0 normal, 0.5 warn, 0.25 throttle, 0.0 halt)
        eff_per_market = self.max_per_market * self._cap_multiplier
        eff_total      = self.total_cap      * self._cap_multiplier
        if eff_per_market <= 0:
            return False, f"breaker:{self._breaker_tier} cap=0"
        if new_max > eff_per_market:
            return False, (f"per-market max-leg ${new_max:.2f} > cap "
                           f"${eff_per_market:.2f} (mul={self._cap_multiplier:.2f})")
        # Total cap
        total = sum(max(o.price, 0) * o.quantity for lst in self.qm.resting.values() for o in lst)
        existing_for_this = sum(o.price * o.quantity for o in existing)
        projected_total = total - existing_for_this + new_max
        if projected_total > eff_total:
            return False, (f"total ${projected_total:.2f} > cap ${eff_total:.2f} "
                           f"(mul={self._cap_multiplier:.2f})")
        return True, "ok"

    def cycle(self) -> None:
        self.cycles += 1
        cycle_start = time.time()
        _log.info(f"=== cycle {self.cycles} (mode={'paper' if settings.PAPER_MODE else 'live'}) ===")

        # AUDIT FIX (2026-04-29): reconcile self.resting against account
        # truth BEFORE planning new orders. Prevents cap drift from partial
        # fills + reprice cycles. In paper mode, clears state (fresh
        # preview every cycle); in live mode, purges phantoms vs Kalshi.
        try:
            r = self.qm.reconcile_against_account()
            if r.get("phantoms_purged", 0) > 0 or r.get("dropped", 0) > 0:
                _log.info(f"  account reconcile: {r}")
        except Exception as e:
            _log.warning(f"reconcile_against_account error: {e}")

        # AUDIT FIX #4: daily-loss circuit breaker
        if self._check_circuit_breaker():
            _log.warning("breaker tripped — skipping new orders this cycle")
            return

        # 2026-05-01 PREDATOR: refresh per-slug net YES position cache for
        # inventory skew. Cheap (1 API call), 5s TTL inside the function.
        self._refresh_positions_cache()

        # 2026-05-02 PREDATOR K1: drain requote queue from capital_reaper.
        # If reaper cancelled a PM order since the last cycle, log it so we
        # know to re-prioritize that slug. Discovery below will re-include
        # it if it still ranks well; this just makes the loop aware.
        try:
            import sys as _sys
            # APPEND (don't insert at 0) so PM's local tools/ takes precedence
            # over Kalshi's tools/. Bug discovered when path-insert hid PM's
            # tools/us_scanner from discover_targets().
            if "/root/lip-maker" not in _sys.path:
                _sys.path.append("/root/lip-maker")
            from cross_venue.requote_queue import drain as _drain_requote
            requotes = _drain_requote(venue_filter="pm")
            if requotes:
                slugs_drained = [e.get("key", "") for e in requotes]
                _log.info(f"K1 drained {len(requotes)} reaper-cancelled "
                          f"slugs: {slugs_drained[:5]}")
        except Exception as e:
            _log.debug(f"K1 requote drain skipped: {e}")

        try:
            targets = self.discover_targets()
        except Exception as e:
            _log.warning(f"discover failed: {e}")
            return
        _log.info(f"discovery: {len(targets)} candidates")

        # WS subscribe — restart shards if slug set changed since last cycle.
        # Original bug: _ws_started=True locked subscriptions to first cycle's
        # slugs forever; switching to PGA after Trump kept WS on dead Trump
        # subs while quoter polled REST for PGA (src={'ws':0,'rest':4}).
        slugs = [m["slug"] for m, _, _ in targets[:self.top_n]]
        new_set = set(slugs)
        if slugs and new_set != self._ws_subscribed_slugs:
            if self._ws_started:
                _log.info(f"WS slug set changed "
                          f"({len(self._ws_subscribed_slugs)}→{len(new_set)}); restarting")
                try:
                    self.ws_stream.stop()
                except Exception as e:
                    _log.debug(f"ws stop err: {e}")
            # Fresh stream instance — old thread will drain on its own
            self.ws_stream = PMBookStream(key_id=self._ws_keys[0],
                                          secret_key=self._ws_keys[1])
            self.ws_stream.subscribe(slugs)
            self.ws_stream.start()
            self._ws_started = True
            self._ws_subscribed_slugs = new_set
            _log.info(f"WS subscribed to {len(slugs)} slugs")

        active_slugs = set()
        snap_rows = []  # collect for batch insert
        src_counts = {"ws": 0, "rest": 0, "rejected": 0}
        for market, book, score in targets:
            target, build_reason = self.build_target(market, book)
            if not target:
                src_counts["rejected"] += 1
                # 2026-05-02: bumped from DEBUG to INFO so we can see WHY we
                # only quote 1-2 of 8 candidates per cycle. Without this it's
                # invisible — masquerading as just "rejected: 7" in the cycle
                # summary. Real reasons like "no book", "spread > max",
                # "degenerate book" need to surface.
                _log.info(f"  skip {market.get('slug','?')[:50]}: {build_reason}")
                continue
            if "ws(" in build_reason:
                src_counts["ws"] += 1
            else:
                src_counts["rest"] += 1
            ok, reason = self.passes_gates(target)
            if not ok:
                _log.info(f"skip {target.slug}: {reason}")
                continue
            active_slugs.add(target.slug)
            res = self.qm.reconcile(target)
            _log.info(f"  {target.slug[:50]:<50s} EV=${score['expected_usd']:>6.2f}/d  "
                      f"reconcile={res}")
            # OBSERVABILITY (#2 fix): persist snapshot per-market per-cycle
            snap_rows.append({
                "slug":           target.slug,
                "midpoint":       book.get("midpoint"),
                "yes_bid_size":   book.get("top_bid_size", 0),
                "no_bid_size":    book.get("top_ask_size", 0),
                "our_yes_score":  target.quantity if target.yes_price else 0,
                "our_no_score":   target.quantity if target.no_price else 0,
                "qualifies":      score.get("qualifies", False),
                "expected_usd":   score.get("expected_usd", 0),
                "was_resting":    1 if not settings.PAPER_MODE else 0,
            })

        # Batch persist snapshots
        if snap_rows:
            self._persist_snapshots(snap_rows)

        # Cancel orders on markets that fell off the top-N
        for slug in list(self.qm.resting.keys()):
            if slug not in active_slugs:
                n = self.qm.cancel_all(slug=slug)
                if n > 0:
                    _log.info(f"  pruned {slug}: cancelled {n} orders (off top-N)")

        elapsed = time.time() - cycle_start
        s = self.qm.summary()
        _log.info(f"cycle done in {elapsed:.1f}s — {s['n_markets']} markets, "
                  f"{s['n_orders']} orders, gross=${s['gross_usd']:.2f} "
                  f"src={src_counts}")

        # NEXUS-OMNI D-misc: Money-Print log line.
        # The scorer's expected_usd is THEORETICAL (assumes 100% capture
        # of our top-of-book share × full pool). Real capture is typically
        # 5-15% due to: (a) full target_size walk includes orders behind
        # us at lower DF^ticks score, (b) we get displaced during reprice
        # cycles, (c) min payout floor drops sub-$1 payouts.
        # CALIBRATION_FACTOR: empirical haircut to convert theoretical → expected.
        # Update after first PM payouts land (May 6-11). Default 0.10 conservative.
        CALIB = 0.10
        try:
            proj_daily_max = sum(score.get("expected_usd", 0) or 0
                                 for _, _, score in targets if score)
            proj_daily_real = proj_daily_max * CALIB
            capital_at_risk = s.get("gross_usd", 0)
            yield_pct = (proj_daily_real / capital_at_risk * 100) if capital_at_risk > 0 else 0
            _log.info(f"💰 MONEY_PRINT: cap=${capital_at_risk:.2f} "
                      f"proj_daily=${proj_daily_real:.2f} (theo_max=${proj_daily_max:.0f}, calib={CALIB:.2f}) "
                      f"proj_monthly=${proj_daily_real*30:.0f} "
                      f"yield={yield_pct:.2f}%/d")
        except Exception:
            pass

    def loop(self) -> None:
        while not self._stop:
            try:
                self.cycle()
            except Exception as e:
                _log.exception(f"cycle exception: {e}")
            for _ in range(self.cycle_sec):
                if self._stop:
                    break
                time.sleep(1)
        _log.info("graceful shutdown — cancelling all open orders")
        try:
            n = self.qm.cancel_all()
            _log.info(f"cancelled {n} orders on shutdown")
        except Exception as e:
            _log.warning(f"shutdown cancel error: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--max-per-market", type=float, default=DEFAULT_MAX_PER_MARKET)
    p.add_argument("--total-cap", type=float, default=DEFAULT_TOTAL_CAP)
    p.add_argument("--cycle", type=int, default=DEFAULT_CYCLE_SEC)
    p.add_argument("--min-qty", type=int, default=DEFAULT_MIN_QTY)
    a = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # 2026-04-30: tripwire — warn if rewards_schedule hasn't been verified
    try:
        from datetime import date
        from engine.rewards_schedule import LAST_VERIFIED, DRIFT_WARN_DAYS
        days_stale = (date.today() - LAST_VERIFIED).days
        if days_stale > DRIFT_WARN_DAYS:
            _log.warning(f"⚠️  PM rewards_schedule LAST_VERIFIED={LAST_VERIFIED} "
                         f"is {days_stale}d old (warn at >{DRIFT_WARN_DAYS}d). "
                         f"Re-check docs.polymarket.us for pool/TS/DF changes.")
        else:
            _log.info(f"PM rewards_schedule fresh (verified {days_stale}d ago)")
    except Exception as e:
        _log.warning(f"rewards_schedule freshness check failed: {e}")

    # 2026-04-30: bankroll divergence check — env var vs live USDC.
    # 2026-05-01 PREDATOR: now AUTO-UPDATES the runtime bankroll (and all
    # derived caps) when divergence detected. Old behavior just warned —
    # cost us 8 hours of zero quoting overnight when $750 deposit landed
    # but PM_BANKROLL env stayed at $270, so MAX_DAILY_LOSS=$13.50 and
    # WARN tier tripped at $4.67 MTM swing → 50% throttle stuck on.
    try:
        _key_id = os.getenv("PM_API_KEY_ID")
        _secret = (PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip()
        _c = PolymarketUS(key_id=_key_id, secret_key=_secret)
        _b = _c.account.balances().get("balances", [{}])[0]
        live_usdc = float(_b.get("currentBalance", 0))
        env_bankroll = settings.BANKROLL_USD
        if abs(live_usdc - env_bankroll) > max(50, env_bankroll * 0.20):
            # AUTO-PATCH: recompute settings constants in place
            settings.BANKROLL_USD       = live_usdc
            settings.MAX_DAILY_LOSS_USD = live_usdc * 0.05
            settings.MAX_SESSION_LOSS_USD = live_usdc * 0.10
            settings.MAX_SINGLE_LOSS_USD  = live_usdc * 0.02
            _log.warning(f"⚠️  PM_BANKROLL env=${env_bankroll:.0f} ≠ live=${live_usdc:.2f}. "
                         f"AUTO-PATCHED runtime: bankroll=${live_usdc:.0f}, "
                         f"daily_loss_cap=${live_usdc*0.05:.2f}. "
                         f"Persist via: systemctl set-environment PM_BANKROLL={int(live_usdc)}")
        else:
            _log.info(f"bankroll check OK: env=${env_bankroll:.0f} ≈ live=${live_usdc:.2f}")
    except Exception as e:
        _log.warning(f"bankroll divergence check failed: {e}")

    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  POLYMARKET AUTO-QUOTER")
    print(f"  mode:           {'PAPER' if settings.PAPER_MODE else '🔴 LIVE'}")
    print(f"  top-N markets:  {a.top}")
    print(f"  max/market:     ${a.max_per_market}")
    print(f"  total cap:      ${a.total_cap}")
    print(f"  cycle:          {a.cycle}s")
    print(f"══════════════════════════════════════════════════════════════\n")

    runner = Runner(
        top_n=a.top,
        max_per_market=a.max_per_market,
        total_cap=a.total_cap,
        cycle_sec=a.cycle,
        min_qty=a.min_qty,
    )

    def _sigterm(signum, frame):
        _log.info(f"received signal {signum} — requesting stop")
        runner.request_stop()
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    # In live mode, prime the breaker baseline BEFORE entering the loop
    # so we don't burn early cycles on rate-limited /balances calls.
    if not settings.PAPER_MODE:
        runner.prime_baseline()

    try:
        runner.loop()
    except KeyboardInterrupt:
        runner.request_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
