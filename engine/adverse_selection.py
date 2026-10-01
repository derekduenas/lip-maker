"""Online adverse-selection guard for the LIP maker (2026-09-30).

Why this exists
---------------
Everything the repo had against toxic flow was either *batch* or *reactive*:

  * tools/toxicity_filter.py, tools/vpin_gate.py, tools/order_flow_tracker.py
    run on systemd timers and write market_throttle rows minutes later;
  * monitor/markout_logger.py backfills markouts offline;
  * SERIES_BLOCKLIST / series_auto_prune ban a series only after it has
    already lost money (KXTRUEV: 14d net -$229 across 155 fills before the
    ban; KXMETGALA -$65 in 6h; KXEOWEEK -$48 in 6h — config/settings.py).

Inside the quoting loop the only defence was `_is_volatile`, and its skip
reason ("volatility") was TRANSIENT: it stopped *repricing* but left the
stale quotes resting — exactly the orders informed flow picks off.

This module is the in-loop layer. It is pure (no I/O unless a db_path is
given for markout persistence), deterministic given timestamps, and cheap.

What it decides, per market and side
------------------------------------
1. Post-fill fade. After we are filled on side S, S is suppressed for
   ``fill_cooldown_sec``. A fill on a resting bid is evidence the price is
   moving through us; topping the same bid back up at the same price is the
   classic way a maker accumulates one-sided inventory.
2. Fill burst. If same-side filled contracts within ``burst_window_sec``
   reach ``burst_contracts``, BOTH sides are pulled for
   ``burst_cooldown_sec``.
3. Markout toxicity. Every fill is marked to the YES mid at fixed horizons.
   A quantity-weighted EWMA of the ``score_horizon_sec`` markout (in cents,
   negative = we lost) forms the per-market toxicity score. Score below
   ``-pull_markout_cents`` (with at least ``min_obs`` matured fills) pulls
   the market for ``toxic_cooldown_sec``; below ``-widen_markout_cents`` it
   backs both sides off one tick.
4. Volatility pull. ``note_volatility`` puts the market in cooldown so the
   runner cancels instead of leaving quotes behind.

Markouts are measured against the FILL price (realised view), so the half
spread a maker earns counts in its favour; a negative score therefore means
the flow is more informed than the spread pays for.

The defaults are deliberately conservative starting points, not fitted
values. Every matured markout is (optionally) persisted to ``as_markouts``
so the thresholds can be refit from paper data — see
docs/ADVERSE_SELECTION.md.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger(__name__)

SIDES = ("yes", "no")


@dataclass(frozen=True)
class ASConfig:
    fill_cooldown_sec: float = 15.0
    burst_window_sec: float = 60.0
    burst_contracts: float = 100.0
    burst_cooldown_sec: float = 120.0
    horizons_sec: tuple = (5.0, 30.0, 120.0)
    score_horizon_sec: float = 30.0
    ewma_alpha: float = 0.2
    min_obs: int = 3
    widen_markout_cents: float = 1.0
    pull_markout_cents: float = 3.0
    toxic_cooldown_sec: float = 600.0
    volatility_cooldown_sec: float = 30.0
    # A mid older than this is not used to mark a fill. Mids are recorded on
    # book events, and an unchanged book means an unchanged mid, so this only
    # guards against data gaps (disconnects already mark books stale).
    max_mid_gap_sec: float = 600.0


@dataclass
class _Fill:
    ts: float
    side: str
    price_cents: float
    qty: float
    pending: set = field(default_factory=set)   # horizons not yet matured
    markouts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ASDecision:
    """What the guard wants for one market right now."""
    pull_market: bool = False
    suppress: frozenset = frozenset()       # sides that must not be quoted
    tick_back: int = 0                      # cents to back BOTH sides off
    reason: str = ""

    @property
    def is_clear(self) -> bool:
        return not self.pull_market and not self.suppress and not self.tick_back


def markout_cents(side: str, fill_price_cents: float, mid_yes_cents: float) -> float:
    """Signed markout of one contract, in cents (positive = in our favour).

    A YES bid fill bought YES at p: worth mid_yes later.
    A NO bid fill bought NO at q: worth (100 - mid_yes) later."""
    if side == "yes":
        return float(mid_yes_cents) - float(fill_price_cents)
    if side == "no":
        return (100.0 - float(mid_yes_cents)) - float(fill_price_cents)
    raise ValueError(f"side must be yes/no, got {side!r}")


def yes_mid_cents(best_yes_bid_cents: Optional[float],
                  best_no_bid_cents: Optional[float]) -> Optional[float]:
    """YES mid from the two bid ladders (YES ask = 100 - best NO bid)."""
    if best_yes_bid_cents is None or best_no_bid_cents is None:
        return None
    ask = 100.0 - float(best_no_bid_cents)
    bid = float(best_yes_bid_cents)
    if ask < bid:          # crossed/locked garbage: refuse to mark against it
        return None
    return (bid + ask) / 2.0


class AdverseSelectionGuard:
    def __init__(self, config: ASConfig | None = None, *, db_path: Optional[str] = None):
        self.cfg = config or ASConfig()
        self.db_path = db_path
        self._fills: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._mids: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))
        self._last_fill_ts: dict[tuple, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._cooldown_reason: dict[str, str] = {}
        self._score: dict[str, float] = {}
        self._obs: dict[str, int] = defaultdict(int)
        self.matured: list[dict] = []          # bounded audit trail (last 1000)
        self.counters: dict[str, int] = defaultdict(int)
        if db_path:
            self._ensure_schema()

    # ── inputs ──────────────────────────────────────────────────────────
    def record_fill(self, ticker: str, side: str, price_cents: Optional[float],
                    qty: float, ts: float) -> None:
        if side not in SIDES or qty is None or qty <= 0:
            return
        self._last_fill_ts[(ticker, side)] = ts
        self.counters["fills"] += 1
        if price_cents is not None:
            self._fills[ticker].append(_Fill(ts=ts, side=side,
                                             price_cents=float(price_cents),
                                             qty=float(qty),
                                             pending=set(self.cfg.horizons_sec)))
        # Burst check is on fills in the window, priced or not.
        window = [f for f in self._fills[ticker]
                  if f.side == side and ts - f.ts <= self.cfg.burst_window_sec]
        burst_qty = sum(f.qty for f in window)
        if price_cents is None:
            burst_qty += qty
        if burst_qty >= self.cfg.burst_contracts:
            self._cool(ticker, ts + self.cfg.burst_cooldown_sec,
                       f"fill_burst:{side}:{burst_qty:g}in{self.cfg.burst_window_sec:g}s")
            self.counters["burst_pulls"] += 1

    def record_mid(self, ticker: str, mid_yes_cents: Optional[float], ts: float) -> None:
        if mid_yes_cents is None:
            return
        self._mids[ticker].append((ts, float(mid_yes_cents)))
        self._mature(ticker, ts)

    def note_volatility(self, ticker: str, ts: float) -> None:
        self._cool(ticker, ts + self.cfg.volatility_cooldown_sec, "volatility")
        self.counters["volatility_pulls"] += 1

    # ── decision ────────────────────────────────────────────────────────
    def decide(self, ticker: str, now: float) -> ASDecision:
        until = self._cooldown_until.get(ticker, 0.0)
        if now < until:
            return ASDecision(pull_market=True,
                              reason=f"as_cooldown:{self._cooldown_reason.get(ticker, '')}")
        suppress = frozenset(
            s for s in SIDES
            if now - self._last_fill_ts.get((ticker, s), -1e18) < self.cfg.fill_cooldown_sec)
        tick_back = 0
        reason = ""
        score = self._score.get(ticker)
        if score is not None and self._obs[ticker] >= self.cfg.min_obs:
            if score <= -self.cfg.pull_markout_cents:
                self._cool(ticker, now + self.cfg.toxic_cooldown_sec,
                           f"toxic_markout:{score:.2f}c")
                # One pull per breach: reset so the market re-enters after
                # the cooldown and must re-earn the toxic label.
                self._score.pop(ticker, None)
                self._obs[ticker] = 0
                self.counters["toxic_pulls"] += 1
                return ASDecision(pull_market=True, reason=f"toxic_markout:{score:.2f}c")
            if score <= -self.cfg.widen_markout_cents:
                tick_back = 1
                reason = f"widen_markout:{score:.2f}c"
        if suppress and not reason:
            reason = "post_fill_fade:" + ",".join(sorted(suppress))
        return ASDecision(suppress=suppress, tick_back=tick_back, reason=reason)

    def toxicity(self, ticker: str) -> tuple[Optional[float], int]:
        return self._score.get(ticker), self._obs[ticker]

    def summary(self) -> dict:
        return {"counters": dict(self.counters),
                "scores": {k: round(v, 3) for k, v in self._score.items()},
                "observations": dict(self._obs)}

    # ── internals ───────────────────────────────────────────────────────
    def _cool(self, ticker: str, until: float, reason: str) -> None:
        if until > self._cooldown_until.get(ticker, 0.0):
            self._cooldown_until[ticker] = until
            self._cooldown_reason[ticker] = reason
            _log.info(f"AS pull[{ticker}] {reason} until={until:.0f}")

    def _mid_at(self, ticker: str, t: float) -> Optional[float]:
        """Latest mid observed at or before t, if recent enough."""
        best = None
        for ts, mid in self._mids[ticker]:
            if ts <= t:
                best = (ts, mid)
            else:
                break
        if best is None or t - best[0] > self.cfg.max_mid_gap_sec:
            return None
        return best[1]

    def _mature(self, ticker: str, now: float) -> None:
        for f in self._fills[ticker]:
            for h in sorted(f.pending):
                if now < f.ts + h:
                    continue
                f.pending.discard(h)
                mid = self._mid_at(ticker, f.ts + h)
                if mid is None:
                    self.counters["markout_unmarkable"] += 1
                    continue
                mo = markout_cents(f.side, f.price_cents, mid)
                f.markouts[h] = mo
                rec = {"ticker": ticker, "side": f.side, "fill_ts": f.ts,
                       "price_cents": f.price_cents, "qty": f.qty,
                       "horizon_sec": h, "mid_cents": mid, "markout_cents": mo}
                self.matured.append(rec)
                if len(self.matured) > 1000:
                    del self.matured[:-1000]
                self._persist(rec)
                if h == self.cfg.score_horizon_sec:
                    self._update_score(ticker, mo, f.qty)

    def _update_score(self, ticker: str, mo: float, qty: float) -> None:
        # Quantity-aware EWMA: a 100-lot fill moves the score more than a
        # 1-lot fill, capped so one fill can never exceed full weight.
        a = min(1.0, self.cfg.ewma_alpha * max(1.0, qty / 25.0))
        prev = self._score.get(ticker)
        self._score[ticker] = mo if prev is None else (1 - a) * prev + a * mo
        self._obs[ticker] += 1

    def _ensure_schema(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                conn.execute("""CREATE TABLE IF NOT EXISTS as_markouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL, side TEXT NOT NULL,
                    fill_ts REAL NOT NULL, price_cents REAL, qty REAL,
                    horizon_sec REAL NOT NULL, mid_cents REAL,
                    markout_cents REAL NOT NULL,
                    UNIQUE (ticker, side, fill_ts, horizon_sec))""")
                conn.commit()
            finally:
                conn.close()
        except Exception as e:     # persistence is best-effort, never a gate
            _log.warning(f"as_markouts schema failed: {e}")
            self.db_path = None

    def _persist(self, rec: dict) -> None:
        if not self.db_path:
            return
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO as_markouts (ticker, side, fill_ts, price_cents, "
                    "qty, horizon_sec, mid_cents, markout_cents) VALUES (?,?,?,?,?,?,?,?)",
                    (rec["ticker"], rec["side"], rec["fill_ts"], rec["price_cents"],
                     rec["qty"], rec["horizon_sec"], rec["mid_cents"], rec["markout_cents"]))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.debug(f"as_markouts insert failed: {e}")


def inventory_side_controls(net_yes: float, *, max_net_contracts: float,
                            soft_fraction: float = 0.5) -> tuple[Optional[str], int, str]:
    """Side-aware inventory control.

    Returns (suppress_side, heavy_tick_back, reason):
      * |net| >= max_net_contracts  → stop quoting the HEAVY side entirely
        (the side that would ADD to exposure); keep quoting the reducing side.
      * |net| >= soft_fraction × max → back the heavy side off one tick.
    Long YES (net > 0) ⇒ heavy side is "yes": buying more YES adds exposure,
    buying NO completes pairs and reduces it."""
    if not net_yes or max_net_contracts <= 0:
        return None, 0, ""
    heavy = "yes" if net_yes > 0 else "no"
    mag = abs(float(net_yes))
    if mag >= max_net_contracts:
        return heavy, 0, f"inv_cap:{heavy}:{mag:g}>={max_net_contracts:g}"
    if mag >= soft_fraction * max_net_contracts:
        return None, 1, f"inv_soft:{heavy}:{mag:g}"
    return None, 0, ""
