"""Multi-horizon paper fill markouts. Measurement only (paper); nothing here
changes what the engine quotes.

For every paper fill RunLoop calls ``add``; the loop's own clock (once per
loop second) calls ``on_clock``, which measures every check that has come
due. Each fill is checked at 5 s, 30 s, 2 min, 10 min and 1 h after the fill
against two references for the side bought:

* ``mid``: the loop's side mark at check time (book mid, the remaining side
  of a one-sided book, the last known mark, or the settlement value;
  RunLoop._side_mid_cents);
* ``fv``: the external fair value (mm/unattended/fairvalue.py) when the
  cache has one for the market at check time. When it has none the check is
  counted under ``missing`` and nothing is substituted.

markout (cents per contract) = reference - fill price. A fill bought at 40c
whose side marks 43c five seconds later has a +3c 5 s markout.

A check measured more than max(2 s, 25 % of its horizon) after it fell due
(a feed gap, a stalled loop) is counted under ``late`` and kept out of the
aggregates: it would measure a different horizon.

At settlement (``on_settle``) the held legs of the market (per venue and
bucket, from RunLoop.bucket_pos) are marked at 100/0 under the reference
``settlement``, horizon ``settle``.

Aggregates are kept per (reference, horizon) for all fills and sliced by
venue and by bucket (durable/short): fills ``n``, ``contracts``, ``usd``
total, and ``mean_cents`` = usd x 100 / contracts (contract-weighted).

Also kept for the P&L attribution (RunLoop.pnl_attribution):
``spread_usd`` = sum of count x (side mark at fill - price) / 100 over fills
with a mark at fill time, and ``adverse_usd`` = sum of count x (side mark at
the 10 min check - side mark at fill) / 100 over fills whose 10 min check was
measured on time.

Memory is bounded: at most ``max_pending`` fills (LIP_MARKOUT_MAX_PENDING,
5000) wait for checks; the oldest is dropped (``dropped_fills``) beyond
that. Pending checks are not persisted; ``state()`` / ``load_state()`` carry
the aggregates only.
"""
from __future__ import annotations

import heapq
import os
from typing import Callable, Optional

HORIZONS = (("5s", 5.0), ("30s", 30.0), ("2m", 120.0), ("10m", 600.0), ("1h", 3600.0))
ADVERSE_HORIZON = "10m"
REFS = ("mid", "fv")
LABEL = ("estimate (paper): simulated paper fills; markout = reference - fill price "
         "for the side bought")

MarkFn = Callable[[str, str], Optional[float]]


def max_pending_default() -> int:
    try:
        return max(1, int(float(os.environ.get("LIP_MARKOUT_MAX_PENDING", 5000))))
    except (TypeError, ValueError):
        return 5000


def _late_s(horizon_s: float) -> float:
    return max(2.0, 0.25 * float(horizon_s))


class MarkoutBook:
    def __init__(self, max_pending: int | None = None) -> None:
        self.max_pending = max_pending_default() if max_pending is None else max(1, int(max_pending))
        self._heap: list = []                 # (due_ts, seq, key, horizon index)
        self._fills: dict[int, dict] = {}     # key -> fill record (insertion ordered)
        self._seq = 0
        # "ref|horizon|slice kind|slice name" -> [fills, contracts, usd]
        self.agg: dict[str, list] = {}
        self.missing: dict[str, int] = {}     # "ref|horizon" -> checks with no reference
        self.late: dict[str, int] = {}        # horizon -> checks measured too late
        self.dropped = 0
        self.synthetic = 0
        self.spread_usd = 0.0
        self.spread_unmeasured = 0
        self.adverse_usd = 0.0
        self.adverse_n = 0

    # ------------------------------------------------------------ input
    def add(self, *, market: str, side: str, price_cents: float, count: float, ts: float,
            venue: str, bucket: str, mid0: float | None, synthetic: bool = False) -> None:
        if side not in ("yes", "no") or count <= 0:
            return
        self._seq += 1
        key = self._seq
        self._fills[key] = {"market": market, "side": side, "price": float(price_cents),
                            "count": float(count), "ts": float(ts), "venue": str(venue),
                            "bucket": str(bucket or "short"),
                            "mid0": None if mid0 is None else float(mid0), "left": len(HORIZONS)}
        if synthetic:
            self.synthetic += 1
        if mid0 is None:
            self.spread_unmeasured += 1
        else:
            self.spread_usd += float(count) * (float(mid0) - float(price_cents)) / 100.0
        for i, (_name, secs) in enumerate(HORIZONS):
            self._seq += 1
            heapq.heappush(self._heap, (float(ts) + secs, self._seq, key, i))
        while len(self._fills) > self.max_pending:
            self._fills.pop(next(iter(self._fills)))
            self.dropped += 1
        if len(self._heap) > 6 * self.max_pending + len(HORIZONS):
            self._heap = [e for e in self._heap if e[2] in self._fills]
            heapq.heapify(self._heap)

    def on_clock(self, now: float, mark_fn: MarkFn, fv_fn: MarkFn) -> int:
        """Measure every check due at or before ``now``. Returns how many."""
        done = 0
        while self._heap and self._heap[0][0] <= now:
            due, _seq, key, idx = heapq.heappop(self._heap)
            rec = self._fills.get(key)
            if rec is None:
                continue
            name, secs = HORIZONS[idx]
            rec["left"] -= 1
            if rec["left"] <= 0:
                self._fills.pop(key, None)
            if now - due > _late_s(secs):
                self.late[name] = self.late.get(name, 0) + 1
                continue
            done += 1
            refs = {"mid": mark_fn(rec["market"], rec["side"]),
                    "fv": fv_fn(rec["market"], rec["side"])}
            for ref in REFS:
                val = refs[ref]
                if val is None:
                    k = f"{ref}|{name}"
                    self.missing[k] = self.missing.get(k, 0) + 1
                    continue
                usd = rec["count"] * (float(val) - rec["price"]) / 100.0
                self._bump(ref, name, rec["venue"], rec["bucket"], 1, rec["count"], usd)
            if name == ADVERSE_HORIZON and rec["mid0"] is not None and refs["mid"] is not None:
                self.adverse_usd += rec["count"] * (float(refs["mid"]) - rec["mid0"]) / 100.0
                self.adverse_n += 1
        return done

    def on_settle(self, market: str, result: str, legs) -> None:
        """``legs``: (venue, bucket, yes, no, yes_cost, no_cost, fills_n) held
        in the market at settlement; YES pays 100c on "yes", NO on "no"."""
        if result not in ("yes", "no"):
            return
        yes_value = 100.0 if result == "yes" else 0.0
        for venue, bucket, yes, no, yes_cost, no_cost, fills_n in legs:
            contracts = float(yes) + float(no)
            if contracts <= 0:
                continue
            usd = (float(yes) * yes_value + float(no) * (100.0 - yes_value)) / 100.0 \
                - float(yes_cost) - float(no_cost)
            self._bump("settlement", "settle", venue, bucket, int(fills_n or 0), contracts, usd)

    def _bump(self, ref, horizon, venue, bucket, n, contracts, usd) -> None:
        for kind, name in (("all", "all"), ("venue", venue), ("bucket", bucket)):
            row = self.agg.setdefault(f"{ref}|{horizon}|{kind}|{name}", [0, 0.0, 0.0])
            row[0] += int(n)
            row[1] += float(contracts)
            row[2] += float(usd)

    # ------------------------------------------------------------ output
    @staticmethod
    def _cell(row) -> dict:
        n, contracts, usd = row if row is not None else (0, 0.0, 0.0)
        return {"n": int(n), "contracts": round(float(contracts), 6),
                "mean_cents": None if not contracts else round(float(usd) * 100.0 / float(contracts), 4),
                "usd": round(float(usd), 6)}

    def report(self) -> dict:
        by_ref: dict = {}
        plan = [(ref, h) for ref in REFS for h, _s in HORIZONS] + [("settlement", "settle")]
        for ref, h in plan:
            node = {"all": self._cell(self.agg.get(f"{ref}|{h}|all|all")), "venue": {}, "bucket": {}}
            for key, row in self.agg.items():
                r, hh, kind, name = key.split("|", 3)
                if r == ref and hh == h and kind in ("venue", "bucket"):
                    node[kind][name] = self._cell(row)
            by_ref.setdefault(ref, {})[h] = node
        return {
            "label": LABEL,
            "horizons": [h for h, _s in HORIZONS] + ["settle"],
            "by_ref": by_ref,
            "missing": {ref: {h: self.missing.get(f"{ref}|{h}", 0) for h, _s in HORIZONS} for ref in REFS},
            "late": {h: self.late.get(h, 0) for h, _s in HORIZONS},
            "pending_fills": len(self._fills),
            "dropped_fills": self.dropped,
            "synthetic_fills": self.synthetic,
        }

    # ------------------------------------------------------------ persistence
    def state(self) -> dict:
        return {"agg": {k: list(v) for k, v in self.agg.items()}, "missing": dict(self.missing),
                "late": dict(self.late), "dropped": self.dropped, "synthetic": self.synthetic,
                "spread_usd": self.spread_usd, "spread_unmeasured": self.spread_unmeasured,
                "adverse_usd": self.adverse_usd, "adverse_n": self.adverse_n}

    def load_state(self, data: dict) -> None:
        """Replace the aggregates with a saved ``state()``. Raises ValueError
        on a malformed payload (nothing is applied then)."""
        if not isinstance(data, dict):
            raise ValueError("markouts state must be an object")
        agg = {}
        for k, v in dict(data.get("agg") or {}).items():
            if len(str(k).split("|")) != 4 or len(v) != 3:
                raise ValueError(f"bad markouts aggregate {k!r}")
            agg[str(k)] = [int(v[0]), float(v[1]), float(v[2])]
        missing = {str(k): int(v) for k, v in dict(data.get("missing") or {}).items()}
        late = {str(k): int(v) for k, v in dict(data.get("late") or {}).items()}
        nums = {k: float(data.get(k) or 0.0) for k in ("spread_usd", "adverse_usd")}
        ints = {k: int(data.get(k) or 0) for k in ("dropped", "synthetic", "spread_unmeasured", "adverse_n")}
        self.agg, self.missing, self.late = agg, missing, late
        self.spread_usd, self.adverse_usd = nums["spread_usd"], nums["adverse_usd"]
        self.dropped, self.synthetic = ints["dropped"], ints["synthetic"]
        self.spread_unmeasured, self.adverse_n = ints["spread_unmeasured"], ints["adverse_n"]
