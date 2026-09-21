"""Estimating how often OUR order executes — not how much the market trades.

The correction this implements
------------------------------
An earlier version divided total observed market volume by (queue + size)
and fed that in as `expected_fills`. That was described as a measured fill
rate. It is not one, on three counts:

  * it counted volume on BOTH sides of the book, including trades that hit
    the side we are not quoting;
  * it counted volume at EVERY price, including levels our order can never
    be reached at;
  * it is market flow, which bounds our executions from above but says
    nothing about the probability that our specific order is filled.

Public trade volume cannot establish our fill probability. What it can do
is bound it, and the bound gets much tighter once you use the information
the trade actually carries: its price, and which side the aggressor took.

What this model uses
--------------------
For a resting buy of `side` at `price_cents`:

  PRICE      only trades executed at our price level can fill us. On Kalshi
             a YES bid at p is hit at yes_price == p; a NO bid at p is hit
             at no_price == p.
  AGGRESSOR  our YES bid is hit by a taker buying NO (selling YES), and our
             NO bid by a taker buying YES. Flow on our own side lifts
             offers and never touches our bid.
  DEPTH      volume must clear the depth resting ahead of us at our level
             before any of it reaches us.
  LATENCY    time in flight is not time in the queue; the horizon is
             reduced by it.
  SIZE       executions are counted in whole fills of our order, so a
             larger quote is filled fewer times by the same flow.

    eligible_rate = eligible_volume / observed_window
    reachable     = max(0, eligible_rate * (horizon - latency) - queue_ahead)
    executions    = reachable / size

Sensitivity, because queue position is unobservable
---------------------------------------------------
We cannot see cancellations ahead of us, so `queue_ahead` is an assumption,
not a measurement. Three cases are reported rather than one number:

  optimistic    queue_ahead = 0                 (we are at the front)
  base          queue_ahead = displayed depth   (we joined the back)
  conservative  queue_ahead = displayed depth x CONSERVATIVE_QUEUE_MULT
                                                (others join ahead of us too)

`base` is the default for decisions. A decision that flips between
`optimistic` and `conservative` is a decision the data does not support,
and callers can see that because all three are returned.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

MIN_OBSERVATION_SEC = 60.0
CONSERVATIVE_QUEUE_MULT = 2.0

# Observing NOTHING is a measurement, not ignorance. With zero qualifying
# events in a window W, the one-sided 95% upper bound on the event rate is
# 3/W (the "rule of three"). We use that bound rather than either
#   - assuming zero, which would make an illiquid market look fee-free, or
#   - falling back to "one full fill", which charges a complete round trip
#     to a market where nothing traded for ten minutes.
RULE_OF_THREE = 3.0
DEFAULT_TRADE_SIZE_CONTRACTS = 1.0


@dataclass
class _Level:
    """Volume that could have hit a resting order on one side at one price."""
    contracts: float = 0.0
    trades: int = 0


@dataclass
class _Market:
    # None until the first observation. Using 0.0 as the sentinel made a
    # ts of 0 indistinguishable from "unset" and reset the window start.
    first_ts: Optional[float] = None
    last_ts: float = 0.0
    trades: int = 0
    contracts: float = 0.0
    # (side_we_would_rest_on, price_cents) -> volume that could reach it
    eligible: dict = field(default_factory=lambda: defaultdict(_Level))


@dataclass(frozen=True)
class ExecutionEstimate:
    """Expected whole-order executions over a horizon, with its assumptions
    attached. `measured` is False when the market has not been watched long
    enough; then every figure is None and the caller must treat it as
    unknown."""
    ticker: str
    side: str
    price_cents: int
    size: float
    horizon_sec: float
    measured: bool
    observed_window_sec: float
    eligible_contracts: float
    eligible_rate_per_sec: Optional[float]
    optimistic: Optional[float]
    base: Optional[float]
    conservative: Optional[float]
    queue_ahead_displayed: float
    latency_sec: float
    replenish_cap: Optional[float] = None
    inventory_cap: Optional[float] = None
    binding_cap: str = "flow"
    note: str = ""

    def explain(self) -> dict:
        return {
            "ticker": self.ticker, "side": self.side,
            "price_cents": self.price_cents, "size": self.size,
            "horizon_sec": self.horizon_sec, "measured": self.measured,
            "observed_window_sec": round(self.observed_window_sec, 1),
            "eligible_contracts": round(self.eligible_contracts, 2),
            "eligible_rate_per_sec": self.eligible_rate_per_sec,
            "executions_optimistic": self.optimistic,
            "executions_base": self.base,
            "executions_conservative": self.conservative,
            "queue_ahead_displayed": self.queue_ahead_displayed,
            "latency_sec": self.latency_sec,
            "replenish_cap": self.replenish_cap,
            "inventory_cap": self.inventory_cap,
            "binding_cap": self.binding_cap,
            "basis": ("public trade flow filtered to our price level and to "
                      "takers hitting our side; an UPPER BOUND on our "
                      "executions, not a measured fill rate"),
            "note": self.note,
        }


class ExecutionModel:
    """Accumulates public trades and estimates our executions from them."""

    def __init__(self, *, min_observation_sec: float = MIN_OBSERVATION_SEC,
                 latency_sec: float = 0.25):
        self._m: dict[str, _Market] = {}
        self.min_observation_sec = min_observation_sec
        self.latency_sec = latency_sec

    # ── observation ───────────────────────────────────────────────────
    def observe_trade(self, *, ticker: str, contracts: float,
                      yes_price_cents: Optional[int],
                      no_price_cents: Optional[int],
                      taker_side: str, ts: Optional[float] = None) -> None:
        ts = time.time() if ts is None else ts
        m = self._m.get(ticker)
        if m is None:
            m = _Market(first_ts=ts)
            self._m[ticker] = m
        if m.first_ts is None or ts < m.first_ts:
            m.first_ts = ts
        m.last_ts = max(m.last_ts, ts)
        m.trades += 1
        m.contracts += float(contracts)
        t = (taker_side or "").lower()
        # A taker buying NO sells YES: it consumes YES bids at yes_price.
        if t == "no" and yes_price_cents is not None:
            lvl = m.eligible[("yes", int(yes_price_cents))]
            lvl.contracts += float(contracts)
            lvl.trades += 1
        # A taker buying YES consumes NO bids at no_price.
        elif t == "yes" and no_price_cents is not None:
            lvl = m.eligible[("no", int(no_price_cents))]
            lvl.contracts += float(contracts)
            lvl.trades += 1

    def observe_trades(self, trades: list) -> int:
        n = 0
        for tr in trades or []:
            try:
                qty = float(tr.get("count_fp") or tr.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or not tr.get("ticker"):
                continue

            def cents(v):
                try:
                    return int(round(float(v) * 100))
                except (TypeError, ValueError):
                    return None
            self.observe_trade(
                ticker=tr["ticker"], contracts=qty,
                yes_price_cents=cents(tr.get("yes_price_dollars")),
                no_price_cents=cents(tr.get("no_price_dollars")),
                taker_side=tr.get("taker_side") or "")
            n += 1
        return n

    # ── estimation ────────────────────────────────────────────────────
    def observe_market(self, ticker: str, ts: Optional[float] = None) -> None:
        """Record that we were WATCHING this market at `ts`, whether or not
        anything traded. Without this a silent market has no observation
        window at all, and silence cannot be distinguished from not looking.
        """
        ts = time.time() if ts is None else ts
        m = self._m.get(ticker)
        if m is None:
            m = _Market(first_ts=ts)
            self._m[ticker] = m
        m.last_ts = max(m.last_ts, ts)
        if m.first_ts is None or ts < m.first_ts:
            m.first_ts = ts

    def window_sec(self, ticker: str) -> float:
        m = self._m.get(ticker)
        if m is None or m.first_ts is None:
            return 0.0
        return max(0.0, m.last_ts - m.first_ts)

    def measured(self, ticker: str) -> bool:
        """True once we have WATCHED long enough — trades or no trades."""
        return self.window_sec(ticker) >= self.min_observation_sec

    def _avg_trade_size(self, ticker: str) -> float:
        m = self._m.get(ticker)
        if not m or m.trades <= 0:
            return DEFAULT_TRADE_SIZE_CONTRACTS
        return max(DEFAULT_TRADE_SIZE_CONTRACTS, m.contracts / m.trades)

    def estimate(self, *, ticker: str, side: str, price_cents: int,
                 size: float, horizon_sec: float,
                 queue_ahead_displayed: float = 0.0,
                 replenish_sec: float = 0.0,
                 max_cycles_from_inventory: Optional[float] = None
                 ) -> ExecutionEstimate:
        """Expected whole-order executions, capped by what a real policy can
        actually do.

        Flow alone gave absurd counts — 12,297 refills of an 18-lot in a few
        hours — because `volume / (queue + size)` silently assumes we
        replenish instantly, forever, with no latency, no capital and no
        inventory limit. Being filled is not free:

          REPLENISH  after a fill we must re-quote. At best that is one
                     cycle per `replenish_sec`, so executions cannot exceed
                     horizon / replenish_sec however much volume trades.
          INVENTORY  each fill adds exposure. Once the net limit is reached
                     we must stop and unwind, which caps cycles again.

        Both caps are reported so a reader can see which one bound.
        """
        w = self.window_sec(ticker)
        m = self._m.get(ticker)
        lvl = m.eligible.get((side, int(price_cents))) if m else None
        eligible = lvl.contracts if lvl else 0.0
        base_kw = dict(ticker=ticker, side=side, price_cents=int(price_cents),
                       size=float(size), horizon_sec=float(horizon_sec),
                       observed_window_sec=w, eligible_contracts=eligible,
                       queue_ahead_displayed=float(queue_ahead_displayed),
                       latency_sec=self.latency_sec)
        if not self.measured(ticker) or size <= 0:
            return ExecutionEstimate(
                measured=False, eligible_rate_per_sec=None, optimistic=None,
                base=None, conservative=None, binding_cap="unmeasured",
                note=("not observed long enough to estimate; caller must "
                      "treat executions as UNKNOWN"), **base_kw)
        zero_observed = eligible <= 0.0
        if zero_observed:
            # Rule of three: no qualifying trade in w seconds bounds the
            # rate at 3/w events/sec. Converted to contracts using this
            # market's own average trade size.
            rate = (RULE_OF_THREE * self._avg_trade_size(ticker) / w) if w > 0 else 0.0
        else:
            rate = eligible / w if w > 0 else 0.0
        effective = max(0.0, float(horizon_sec) - self.latency_sec)
        volume = rate * effective

        caps = []
        if replenish_sec and replenish_sec > 0:
            caps.append(float(horizon_sec) / float(replenish_sec))
        if max_cycles_from_inventory is not None:
            caps.append(float(max_cycles_from_inventory))
        cap = min(caps) if caps else None

        def execs(queue: float) -> float:
            raw = max(0.0, volume - queue) / float(size)
            return min(raw, cap) if cap is not None else raw

        raw_base = (max(0.0, volume - float(queue_ahead_displayed))
                    / float(size))
        if cap is not None and cap < raw_base:
            binding = ("replenish" if (replenish_sec and replenish_sec > 0
                                       and cap == float(horizon_sec) / float(replenish_sec))
                       else "inventory")
        else:
            binding = "flow"
        return ExecutionEstimate(
            measured=True, eligible_rate_per_sec=rate,
            optimistic=execs(0.0),
            base=execs(float(queue_ahead_displayed)),
            conservative=execs(float(queue_ahead_displayed)
                               * CONSERVATIVE_QUEUE_MULT),
            replenish_cap=(float(horizon_sec) / float(replenish_sec))
                          if (replenish_sec and replenish_sec > 0) else None,
            inventory_cap=max_cycles_from_inventory,
            binding_cap=binding,
            note=(("no qualifying trade observed in "
                   f"{w:.0f}s; rate bounded by the rule of three "
                   f"({RULE_OF_THREE}/w), not assumed zero and not assumed "
                   "a full fill. ") if zero_observed else "")
                 + ("queue position is unobservable (cancellations ahead of "
                    "us are invisible); three cases reported"), **base_kw)

    def summary(self) -> dict:
        return {t: {"trades": m.trades, "contracts": round(m.contracts, 2),
                    "window_sec": round(self.window_sec(t), 1),
                    "measured": self.measured(t),
                    "eligible_levels": len(m.eligible)}
                for t, m in self._m.items()}
