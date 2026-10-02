"""Out-of-sample scoring of model fair values (measurement only).

Is the model better than the book? For every market priced by the model
(mm/unattended/fv_weather.py) RunLoop records each new fair value with the
book mid at the same moment (``record``). When the market settles
(``on_settle``, from RunLoop.settle) the first recorded value in each
lead-time bucket (fv_weather.lead_bucket: hours from the value to the end of
the settlement window) is scored against the result:

* Brier score (p - y)^2 and log loss -[y ln p + (1 - y) ln(1 - p)], with
  y = 1 for a YES result and p the model probability (already clipped to
  [0.01, 0.99]);
* the same two scores for the book mid at that moment (clipped the same
  way), on the samples that had a two-sided book (``paired``): the baseline.

One sample per (market, lead bucket) keeps the count honest: values
refreshed every few minutes for the same market are not independent tests.
Aggregates are kept per station (series) x lead bucket and persisted in the
engine state file (``state``/``load_state``); pending samples are persisted
too so a restart does not lose them. Recent records (all of them, not only
the first per bucket) are kept in memory for /status.

``report()`` gives mean scores, ``skill_vs_book`` = 1 - Brier(model) /
Brier(book) on paired samples (> 0: the model beat the book), and a verdict
that stays ``insufficient_data`` below LIP_FV_CALIB_MIN_N (300) paired
samples. Nothing here changes quoting.
"""
from __future__ import annotations

import math
import os
from collections import deque

from mm.unattended.fv_weather import lead_bucket

P_MIN, P_MAX = 0.01, 0.99
FIELDS = ("n", "brier", "logloss", "paired_n", "paired_brier", "paired_logloss",
          "book_brier", "book_logloss", "yes_n")


def _clip(p: float) -> float:
    return min(P_MAX, max(P_MIN, float(p)))


def brier(p: float, y: int) -> float:
    return (float(p) - float(y)) ** 2


def log_loss(p: float, y: int) -> float:
    p = _clip(p)
    return -math.log(p) if y else -math.log(1.0 - p)


def min_n() -> int:
    try:
        return max(1, int(float(os.environ.get("LIP_FV_CALIB_MIN_N", 300))))
    except (TypeError, ValueError):
        return 300


class FVCalibration:
    def __init__(self, *, max_pending: int = 5000, recent: int = 200) -> None:
        self.max_pending = int(max_pending)
        self.pending: dict[str, dict] = {}
        self.agg: dict[str, dict] = {}
        self.recent: deque = deque(maxlen=int(recent))
        self.last_fv_ts: dict[str, float] = {}
        self.scored_markets = 0
        self.dropped = 0

    # -- recording (engine thread)
    def record(self, market: str, station: str, ts: float, fv_cents: float, conf: float,
               lead_h: float, mid_cents, fv_ts: float) -> bool:
        """Record one fair value (dedup on the value's own timestamp)."""
        if self.last_fv_ts.get(market) == fv_ts:
            return False
        self.last_fv_ts[market] = fv_ts
        bucket = lead_bucket(float(lead_h))
        row = {"ts": float(ts), "fv": round(float(fv_cents), 3), "conf": round(float(conf), 4),
               "lead_h": round(float(lead_h), 2),
               "mid": None if mid_cents is None else round(float(mid_cents), 3)}
        self.recent.append(dict(row, market=market, bucket=bucket))
        pend = self.pending.get(market)
        if pend is None:
            if len(self.pending) >= self.max_pending:
                oldest = min(self.pending, key=lambda m: self.pending[m]["last_ts"])
                del self.pending[oldest]
                self.last_fv_ts.pop(oldest, None)
                self.dropped += 1
            pend = self.pending[market] = {"station": str(station), "samples": {}, "last_ts": float(ts)}
        pend["last_ts"] = float(ts)
        if bucket in pend["samples"]:
            return False
        pend["samples"][bucket] = row
        return True

    # -- scoring (engine thread, from RunLoop.settle)
    def on_settle(self, market: str, result: str) -> int:
        pend = self.pending.pop(market, None)
        self.last_fv_ts.pop(market, None)
        if pend is None or result not in ("yes", "no"):
            return 0
        y = 1 if result == "yes" else 0
        n = 0
        for bucket, s in pend["samples"].items():
            p = _clip(float(s["fv"]) / 100.0)
            a = self.agg.setdefault(f"{pend['station']}|{bucket}", {k: 0.0 for k in FIELDS})
            a["n"] += 1
            a["yes_n"] += y
            a["brier"] += brier(p, y)
            a["logloss"] += log_loss(p, y)
            if s.get("mid") is not None:
                q = _clip(float(s["mid"]) / 100.0)
                a["paired_n"] += 1
                a["paired_brier"] += brier(p, y)
                a["paired_logloss"] += log_loss(p, y)
                a["book_brier"] += brier(q, y)
                a["book_logloss"] += log_loss(q, y)
            n += 1
        if n:
            self.scored_markets += 1
        return n

    def prune(self, now: float, keep_s: float = 10 * 86400.0) -> int:
        """Forget pending samples not updated for ``keep_s`` (a settlement
        that never reached the loop)."""
        old = [m for m, p in self.pending.items() if float(now) - float(p["last_ts"]) > keep_s]
        for m in old:
            del self.pending[m]
            self.last_fv_ts.pop(m, None)
        self.dropped += len(old)
        return len(old)

    # -- reporting
    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        tot = {k: sum(r[k] for r in rows) for k in FIELDS}
        n, pn = tot["n"], tot["paired_n"]
        out = {"n": int(n), "paired_n": int(pn), "yes_rate": None if not n else round(tot["yes_n"] / n, 4),
               "brier": None if not n else round(tot["brier"] / n, 5),
               "logloss": None if not n else round(tot["logloss"] / n, 5),
               "paired_brier_model": None if not pn else round(tot["paired_brier"] / pn, 5),
               "paired_brier_book": None if not pn else round(tot["book_brier"] / pn, 5),
               "paired_logloss_model": None if not pn else round(tot["paired_logloss"] / pn, 5),
               "paired_logloss_book": None if not pn else round(tot["book_logloss"] / pn, 5)}
        out["skill_vs_book"] = (None if not pn or tot["book_brier"] <= 0
                                else round(1.0 - tot["paired_brier"] / tot["book_brier"], 4))
        return out

    def report(self) -> dict:
        keys = sorted(self.agg)
        by_station: dict[str, list] = {}
        by_lead: dict[str, list] = {}
        for k in keys:
            st, lb = k.split("|", 1)
            by_station.setdefault(st, []).append(self.agg[k])
            by_lead.setdefault(lb, []).append(self.agg[k])
        overall = self._summary([self.agg[k] for k in keys])
        need = min_n()
        if overall["paired_n"] < need:
            verdict = "insufficient_data"
        elif overall["skill_vs_book"] is not None and overall["skill_vs_book"] > 0:
            verdict = "model_better_than_book"
        else:
            verdict = "book_better_or_equal"
        return {"label": "out-of-sample (paper): model fair value vs book mid at the same time, scored at "
                         "settlement; one sample per market and lead bucket",
                "verdict": verdict, "min_paired_n": need, "overall": overall,
                "by_station": {s: self._summary(r) for s, r in sorted(by_station.items())},
                "by_lead": {b: self._summary(r) for b, r in sorted(by_lead.items())},
                "by_station_lead": {k: self._summary([self.agg[k]]) for k in keys},
                "scored_markets": self.scored_markets, "pending_markets": len(self.pending),
                "dropped": self.dropped, "recent": list(self.recent)[-10:]}

    # -- persistence
    def state(self) -> dict:
        return {"agg": self.agg, "pending": self.pending, "scored_markets": self.scored_markets,
                "dropped": self.dropped}

    def load_state(self, data: dict) -> None:
        """Validate then apply; raises ValueError on a malformed section."""
        if not isinstance(data, dict):
            raise ValueError("fv_calibration must be an object")
        agg = {}
        for k, row in dict(data.get("agg") or {}).items():
            if "|" not in str(k) or not isinstance(row, dict):
                raise ValueError(f"bad fv_calibration key {k!r}")
            agg[str(k)] = {f: float(row.get(f, 0.0)) for f in FIELDS}
        pending = {}
        for m, p in dict(data.get("pending") or {}).items():
            samples = {}
            for b, s in dict(p["samples"]).items():
                samples[str(b)] = {"ts": float(s["ts"]), "fv": float(s["fv"]), "conf": float(s["conf"]),
                                   "lead_h": float(s["lead_h"]),
                                   "mid": None if s.get("mid") is None else float(s["mid"])}
            pending[str(m)] = {"station": str(p["station"]), "samples": samples, "last_ts": float(p["last_ts"])}
        self.agg = agg
        self.pending = pending
        self.scored_markets = int(data.get("scored_markets") or 0)
        self.dropped = int(data.get("dropped") or 0)
