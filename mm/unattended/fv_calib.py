"""Out-of-sample scoring of model fair values (measurement only).

Is the model better than the book? For every market priced by the model
(mm/unattended/fv_weather.py) RunLoop records each new fair value with the
book mid at the same moment (``record``), and, when every bucket of the
market's city-day event has a value at that moment, the event's whole
bucket distribution (``record_event``). When markets settle (``on_settle``,
from RunLoop.settle) the recorded samples are scored against the result.

Per market, per lead-time bucket (fv_weather.lead_bucket: hours from the
value to the end of the settlement window) two samples are kept:

* the first sample (``samples``; book mid or not): model Brier
  (p - y)^2 and log loss -[y ln p + (1 - y) ln(1 - p)], y = 1 for YES;
* the first sample that HAD a two-sided book (``paired``): model and book
  mid (clipped to [0.01, 0.99]) Brier / log loss, the baseline. A first
  sample without a book no longer locks the bucket's pairing.

Per event (series + date segment of the ticker, e.g. KXHIGHNY-26OCT01) and
lead bucket, again the first sample and the first sample where EVERY bucket
had a book mid: the model distribution (the buckets' fair values normalised
to sum 1) and the book distribution (the mids normalised the same way) are
scored with the ranked probability score (RPS: mean squared difference of
the cumulative distributions over the ordered buckets) and the log score
(-ln p of the bucket that settled YES, p floored at 0.01). An event is
recorded only when its buckets tile the whole line (first open below, last
open above, no gap): otherwise there is no distribution to score. The five
or six buckets of one city-day are one test, not five: the verdict uses
events, and the per-market numbers are reported with the distinct paired
market count beside them.

Confidence: samples with conf >= LIP_FV_MIN_CONF (the quoting threshold)
make the headline aggregates; lower-confidence ones are kept separately
(``low_conf``) and never reach the verdict.

``report()``: per-market aggregates per station / lead bucket,
``skill_vs_book`` = 1 - Brier(model) / Brier(book) on paired samples, event
aggregates with ``rps_skill_vs_book`` = 1 - RPS(model) / RPS(book), and the
verdict: ``insufficient_data`` until LIP_FV_CALIB_MIN_MARKETS (200) distinct
markets with a paired headline sample AND LIP_FV_CALIB_MIN_EVENTS (40)
distinct events with a paired headline event sample were scored; then
``model_better_than_book`` only when the event RPS skill AND the paired
per-market Brier skill are both > 0, else ``book_better_or_equal``.

Settled per-market samples (with the market's range and the model's
member-max summary ``ens``) are queued in ``outbox``; the engine timer
appends them to LIP_FV_CALIB_SAMPLES_FILE for tools/fit_fv_weather.py.
Aggregates and pending samples persist in the engine state file
(``state``/``load_state``); a state written before this format loads its
aggregates as ``legacy`` (reported, never in the verdict). Nothing here
changes quoting except through the verdict (RunLoop credits model edge in
selection only when it passes).
"""
from __future__ import annotations

import math
import os
from collections import deque

from mm.unattended.fv_weather import lead_bucket

P_MIN, P_MAX = 0.01, 0.99
STATE_VERSION = 2
FIELDS = ("n", "brier", "logloss", "paired_n", "paired_brier", "paired_logloss",
          "book_brier", "book_logloss", "yes_n")
EV_FIELDS = ("n", "rps", "log", "paired_n", "paired_rps", "paired_log", "book_rps", "book_log")
OUTBOX_MAX = 20000


def _clip(p: float) -> float:
    return min(P_MAX, max(P_MIN, float(p)))


def brier(p: float, y: int) -> float:
    return (float(p) - float(y)) ** 2


def log_loss(p: float, y: int) -> float:
    p = _clip(p)
    return -math.log(p) if y else -math.log(1.0 - p)


def rps(probs: list, k: int) -> float:
    """Ranked probability score of ordered bucket probabilities ``probs`` when
    bucket ``k`` happened: mean over the K-1 inner edges of the squared
    difference between the forecast and observed cumulative distributions."""
    if len(probs) < 2:
        raise ValueError("rps needs at least two buckets")
    cum, tot = 0.0, 0.0
    for i in range(len(probs) - 1):
        cum += float(probs[i])
        tot += (cum - (1.0 if i >= k else 0.0)) ** 2
    return tot / (len(probs) - 1)


def log_score(probs: list, k: int) -> float:
    return -math.log(max(P_MIN, float(probs[k])))


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def min_markets() -> int:
    """LIP_FV_CALIB_MIN_MARKETS (200): distinct markets with a paired headline sample."""
    return _env_int("LIP_FV_CALIB_MIN_MARKETS", 200)


def min_events() -> int:
    """LIP_FV_CALIB_MIN_EVENTS (40): distinct city-day events with a paired headline event sample."""
    return _env_int("LIP_FV_CALIB_MIN_EVENTS", 40)


def min_conf() -> float:
    from mm.unattended.fairvalue import fv_min_conf
    return fv_min_conf()


def event_of(market: str) -> str:
    """KXHIGHNY-26OCT01-B72.5 -> KXHIGHNY-26OCT01."""
    return str(market).rsplit("-", 1)[0]


def tiles(ranges: list) -> bool:
    """Integer ranges [lo, hi] (None = open) that partition the whole line, in order."""
    if len(ranges) < 2 or ranges[0][0] is not None or ranges[-1][1] is not None:
        return False
    for (lo1, hi1), (lo2, hi2) in zip(ranges, ranges[1:]):
        if hi1 is None or lo2 is None or lo2 != hi1 + 1:
            return False
    return all(lo is None or hi is None or lo <= hi for lo, hi in ranges)


def _sample_row(s: dict) -> dict:
    """Validated pending per-market sample (raises on a malformed one)."""
    rng = s.get("range")
    if rng is not None:
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            raise ValueError("bad sample range")
        rng = [None if x is None else int(x) for x in rng]
    ens = s.get("ens")
    if ens is not None and not isinstance(ens, dict):
        raise ValueError("bad sample ens")
    return {"ts": float(s["ts"]), "fv": float(s["fv"]), "conf": float(s["conf"]),
            "lead_h": float(s["lead_h"]), "mid": None if s.get("mid") is None else float(s["mid"]),
            "range": rng, "ens": None if ens is None else dict(ens)}


def _event_row(s: dict) -> dict:
    markets = [str(m) for m in s["markets"]]
    model = [float(x) for x in s["model"]]
    book = None if s.get("book") is None else [float(x) for x in s["book"]]
    if len(model) != len(markets) or (book is not None and len(book) != len(markets)):
        raise ValueError("bad event sample")
    return {"ts": float(s["ts"]), "lead_h": float(s["lead_h"]), "conf": float(s["conf"]),
            "markets": markets, "model": model, "book": book}


class FVCalibration:
    def __init__(self, *, max_pending: int = 5000, recent: int = 200) -> None:
        self.max_pending = int(max_pending)
        self.pending: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.agg: dict[str, dict] = {}          # headline (conf >= min_conf), "station|bucket"
        self.agg_low: dict[str, dict] = {}
        self.event_agg: dict[str, dict] = {}
        self.event_agg_low: dict[str, dict] = {}
        self.legacy_agg: dict[str, dict] = {}   # pre-fix state: reported only
        self.legacy_scored_markets = 0
        self.recent: deque = deque(maxlen=int(recent))
        self.last_fv_ts: dict[str, float] = {}
        self.scored_markets = 0
        self.paired_markets = 0
        self.paired_markets_low = 0
        self.scored_events = 0
        self.paired_events = 0
        self.paired_events_low = 0
        self.dropped = 0
        self.outbox: list = []
        self.outbox_dropped = 0
        self._verdict = None

    # -- recording (engine thread)
    def record(self, market: str, station: str, ts: float, fv_cents: float, conf: float,
               lead_h: float, mid_cents, fv_ts: float, *, rng=None, ens=None) -> bool:
        """Record one fair value (dedup on the value's own timestamp): the
        first sample of its lead bucket, and the first sample with a book
        mid. True when something new was stored."""
        if self.last_fv_ts.get(market) == fv_ts:
            return False
        self.last_fv_ts[market] = fv_ts
        bucket = lead_bucket(float(lead_h))
        row = _sample_row({"ts": ts, "fv": round(float(fv_cents), 3), "conf": round(float(conf), 4),
                           "lead_h": round(float(lead_h), 2),
                           "mid": None if mid_cents is None else round(float(mid_cents), 3),
                           "range": None if rng is None else list(rng), "ens": ens})
        self.recent.append({k: row[k] for k in ("ts", "fv", "conf", "lead_h", "mid")}
                           | {"market": market, "bucket": bucket})
        pend = self.pending.get(market)
        if pend is None:
            if len(self.pending) >= self.max_pending:
                oldest = min(self.pending, key=lambda m: self.pending[m]["last_ts"])
                del self.pending[oldest]
                self.last_fv_ts.pop(oldest, None)
                self.dropped += 1
            pend = self.pending[market] = {"station": str(station), "samples": {}, "paired": {},
                                           "last_ts": float(ts)}
        pend["last_ts"] = float(ts)
        new = False
        if bucket not in pend["samples"]:
            pend["samples"][bucket] = row
            new = True
        if row["mid"] is not None and bucket not in pend["paired"]:
            # scoring needs fv/mid/conf only (range/ens go out with ``samples``)
            pend["paired"][bucket] = dict(row, range=None, ens=None)
            new = True
        return new

    def record_event(self, event: str, station: str, ts: float, lead_h: float, entries: list) -> bool:
        """Record the bucket distribution of one city-day event at one moment.
        ``entries``: [{"market", "range": [lo, hi], "fv", "conf", "mid"}]
        for every bucket of the event. Recorded only when the ranges tile the
        line; paired only when every bucket has a mid. True when stored."""
        try:
            rows = sorted(entries, key=lambda e: (-math.inf if e["range"][0] is None else e["range"][0]))
            ranges = [[None if x is None else int(x) for x in e["range"]] for e in rows]
            fvs = [float(e["fv"]) for e in rows]
            mids = [None if e.get("mid") is None else float(e["mid"]) for e in rows]
            conf = min(float(e["conf"]) for e in rows)
        except (KeyError, TypeError, ValueError, IndexError):
            return False
        if not tiles(ranges) or len({e["market"] for e in rows}) != len(rows):
            return False
        tot = sum(fvs)
        if not tot > 0:
            return False
        bucket = lead_bucket(float(lead_h))
        sample = {"ts": float(ts), "lead_h": round(float(lead_h), 2), "conf": round(conf, 4),
                  "markets": [str(e["market"]) for e in rows], "model": [round(f / tot, 6) for f in fvs],
                  "book": None}
        if all(q is not None for q in mids) and sum(mids) > 0:
            sample["book"] = [round(q / sum(mids), 6) for q in mids]
        ev = self.events.get(event)
        if ev is None:
            if len(self.events) >= self.max_pending:
                oldest = min(self.events, key=lambda k: self.events[k]["last_ts"])
                del self.events[oldest]
                self.dropped += 1
            ev = self.events[event] = {"station": str(station), "samples": {}, "paired": {},
                                       "results": {}, "last_ts": float(ts)}
        ev["last_ts"] = float(ts)
        new = False
        if bucket not in ev["samples"]:
            ev["samples"][bucket] = sample
            new = True
        if sample["book"] is not None and bucket not in ev["paired"]:
            ev["paired"][bucket] = sample
            new = True
        return new

    # -- scoring (engine thread, from RunLoop.settle)
    def _add(self, aggs: tuple, key: str, conf: float, fields: tuple, vals: dict) -> bool:
        hi = conf >= min_conf()
        a = (aggs[0] if hi else aggs[1]).setdefault(key, {k: 0.0 for k in fields})
        for k, v in vals.items():
            a[k] += v
        return hi

    def on_settle(self, market: str, result: str) -> int:
        """Score ``market``'s samples (and its event once the YES bucket is
        known). Returns the number of per-market samples scored."""
        self.last_fv_ts.pop(market, None)
        if result not in ("yes", "no"):
            return 0
        y = 1 if result == "yes" else 0
        self._score_event(market, y)
        pend = self.pending.pop(market, None)
        if pend is None:
            return 0
        n = 0
        st = pend["station"]
        aggs = (self.agg, self.agg_low)
        for bucket, s in pend["samples"].items():
            p = _clip(float(s["fv"]) / 100.0)
            self._add(aggs, f"{st}|{bucket}", s["conf"], FIELDS,
                      {"n": 1, "yes_n": y, "brier": brier(p, y), "logloss": log_loss(p, y)})
            n += 1
            if len(self.outbox) >= OUTBOX_MAX:
                self.outbox.pop(0)
                self.outbox_dropped += 1
            self.outbox.append({"market": market, "event": event_of(market), "station": st,
                                "lead_bucket": bucket, "y": y, **s})
        paired_hi = paired_lo = False
        for bucket, s in pend["paired"].items():
            p, q = _clip(float(s["fv"]) / 100.0), _clip(float(s["mid"]) / 100.0)
            hi = self._add(aggs, f"{st}|{bucket}", s["conf"], FIELDS,
                           {"paired_n": 1, "paired_brier": brier(p, y), "paired_logloss": log_loss(p, y),
                            "book_brier": brier(q, y), "book_logloss": log_loss(q, y)})
            paired_hi |= hi
            paired_lo |= not hi
        self.paired_markets += int(paired_hi)
        self.paired_markets_low += int(paired_lo and not paired_hi)
        if pend["samples"]:
            self.scored_markets += 1
        self._verdict = None
        return n

    def _score_event(self, market: str, y: int) -> None:
        """Book the result of one bucket; score the event when its YES
        bucket settles."""
        ev_key = event_of(market)
        ev = self.events.get(ev_key)
        if ev is None:
            return 0
        ev["results"][market] = y
        if not y:
            known = {m for s in list(ev["samples"].values()) + list(ev["paired"].values()) for m in s["markets"]}
            if known and all(ev["results"].get(m) == 0 for m in known):
                del self.events[ev_key]          # no YES bucket among them: nothing to score
                self.dropped += 1
            return
        del self.events[ev_key]
        st = ev["station"]
        aggs = (self.event_agg, self.event_agg_low)
        scored = paired_hi = paired_lo = False
        for bucket, s in ev["samples"].items():
            if market not in s["markets"]:
                continue
            k = s["markets"].index(market)
            self._add(aggs, f"{st}|{bucket}", s["conf"], EV_FIELDS,
                      {"n": 1, "rps": rps(s["model"], k), "log": log_score(s["model"], k)})
            scored = True
        for bucket, s in ev["paired"].items():
            if market not in s["markets"]:
                continue
            k = s["markets"].index(market)
            hi = self._add(aggs, f"{st}|{bucket}", s["conf"], EV_FIELDS,
                           {"paired_n": 1, "paired_rps": rps(s["model"], k),
                            "paired_log": log_score(s["model"], k),
                            "book_rps": rps(s["book"], k), "book_log": log_score(s["book"], k)})
            paired_hi |= hi
            paired_lo |= not hi
        self.scored_events += int(scored)
        self.paired_events += int(paired_hi)
        self.paired_events_low += int(paired_lo and not paired_hi)
        self._verdict = None

    def take_outbox(self) -> list:
        rows, self.outbox = self.outbox, []
        return rows

    def prune(self, now: float, keep_s: float = 10 * 86400.0) -> int:
        """Forget pending samples / events not updated for ``keep_s`` (a
        settlement that never reached the loop)."""
        old = [m for m, p in self.pending.items() if float(now) - float(p["last_ts"]) > keep_s]
        for m in old:
            del self.pending[m]
            self.last_fv_ts.pop(m, None)
        old_ev = [e for e, p in self.events.items() if float(now) - float(p["last_ts"]) > keep_s]
        for e in old_ev:
            del self.events[e]
        self.dropped += len(old) + len(old_ev)
        return len(old) + len(old_ev)

    # -- reporting
    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        tot = {k: sum(r.get(k, 0.0) for r in rows) for k in FIELDS}
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

    @staticmethod
    def _ev_summary(rows: list[dict]) -> dict:
        tot = {k: sum(r.get(k, 0.0) for r in rows) for k in EV_FIELDS}
        n, pn = tot["n"], tot["paired_n"]
        out = {"n": int(n), "paired_n": int(pn),
               "rps": None if not n else round(tot["rps"] / n, 5),
               "log": None if not n else round(tot["log"] / n, 5),
               "paired_rps_model": None if not pn else round(tot["paired_rps"] / pn, 5),
               "paired_rps_book": None if not pn else round(tot["book_rps"] / pn, 5),
               "paired_log_model": None if not pn else round(tot["paired_log"] / pn, 5),
               "paired_log_book": None if not pn else round(tot["book_log"] / pn, 5)}
        out["rps_skill_vs_book"] = (None if not pn or tot["book_rps"] <= 0
                                    else round(1.0 - tot["paired_rps"] / tot["book_rps"], 4))
        return out

    @staticmethod
    def _split(agg: dict, summary) -> dict:
        keys = sorted(agg)
        by_station: dict[str, list] = {}
        by_lead: dict[str, list] = {}
        for k in keys:
            st, lb = k.split("|", 1)
            by_station.setdefault(st, []).append(agg[k])
            by_lead.setdefault(lb, []).append(agg[k])
        return {"overall": summary([agg[k] for k in keys]),
                "by_station": {s: summary(r) for s, r in sorted(by_station.items())},
                "by_lead": {b: summary(r) for b, r in sorted(by_lead.items())},
                "by_station_lead": {k: summary([agg[k]]) for k in keys}}

    def verdict(self) -> str:
        if self._verdict is None:
            mk = self._summary(list(self.agg.values()))
            ev = self._ev_summary(list(self.event_agg.values()))
            if self.paired_markets < min_markets() or self.paired_events < min_events():
                self._verdict = "insufficient_data"
            elif ((ev["rps_skill_vs_book"] or 0.0) > 0 and (mk["skill_vs_book"] or 0.0) > 0):
                self._verdict = "model_better_than_book"
            else:
                self._verdict = "book_better_or_equal"
        return self._verdict

    def passed(self) -> bool:
        return self.verdict() == "model_better_than_book"

    def report(self) -> dict:
        self._verdict = None   # thresholds are env knobs: re-read them
        mk = self._split(self.agg, self._summary)
        ev = self._split(self.event_agg, self._ev_summary)
        out = {"label": "out-of-sample (paper): model fair value vs book mid at the same time, scored at "
                        "settlement. Per market: first sample and first paired sample per lead bucket. "
                        "Per event (city-day): bucket distribution, RPS and log score. Headline = conf >= "
                        "LIP_FV_MIN_CONF; verdict on events",
               "verdict": self.verdict(),
               "thresholds": {"min_paired_markets": min_markets(), "min_paired_events": min_events(),
                              "min_conf": min_conf()},
               **mk,
               "events": dict(ev, scored_events=self.scored_events, paired_events=self.paired_events,
                              pending_events=len(self.events)),
               "low_conf": {"overall": self._summary(list(self.agg_low.values())),
                            "paired_markets": self.paired_markets_low,
                            "events": {"overall": self._ev_summary(list(self.event_agg_low.values())),
                                       "paired_events": self.paired_events_low}},
               "scored_markets": self.scored_markets, "paired_markets": self.paired_markets,
               "pending_markets": len(self.pending), "dropped": self.dropped,
               "samples_queued": len(self.outbox), "samples_dropped": self.outbox_dropped,
               "recent": list(self.recent)[-10:]}
        if self.legacy_agg:
            out["legacy"] = {"label": "aggregates from before the paired/event fix (mixed confidence, first "
                                      "sample locked pairing): not used for the verdict",
                             "overall": self._summary(list(self.legacy_agg.values())),
                             "scored_markets": self.legacy_scored_markets}
        return out

    # -- persistence
    def state(self) -> dict:
        return {"version": STATE_VERSION, "agg": self.agg, "agg_low": self.agg_low,
                "event_agg": self.event_agg, "event_agg_low": self.event_agg_low,
                "legacy_agg": self.legacy_agg, "legacy_scored_markets": self.legacy_scored_markets,
                "pending": self.pending, "events": self.events,
                "scored_markets": self.scored_markets, "paired_markets": self.paired_markets,
                "paired_markets_low": self.paired_markets_low, "scored_events": self.scored_events,
                "paired_events": self.paired_events, "paired_events_low": self.paired_events_low,
                "dropped": self.dropped}

    @staticmethod
    def _load_agg(raw, fields) -> dict:
        out = {}
        for k, row in dict(raw or {}).items():
            if "|" not in str(k) or not isinstance(row, dict):
                raise ValueError(f"bad fv_calibration key {k!r}")
            out[str(k)] = {f: float(row.get(f, 0.0)) for f in fields}
        return out

    def load_state(self, data: dict) -> None:
        """Validate then apply; raises ValueError on a malformed section."""
        if not isinstance(data, dict):
            raise ValueError("fv_calibration must be an object")
        v2 = data.get("version") == STATE_VERSION
        agg = self._load_agg(data.get("agg"), FIELDS)
        pending = {}
        for m, p in dict(data.get("pending") or {}).items():
            samples = {str(b): _sample_row(s) for b, s in dict(p["samples"]).items()}
            if v2:
                paired = {str(b): _sample_row(s) for b, s in dict(p.get("paired") or {}).items()}
            else:   # pre-fix: the only sample per bucket, paired when it had a mid
                paired = {b: s for b, s in samples.items() if s["mid"] is not None}
            pending[str(m)] = {"station": str(p["station"]), "samples": samples, "paired": paired,
                               "last_ts": float(p["last_ts"])}
        events = {}
        for e, p in dict(data.get("events") or {}).items():
            events[str(e)] = {"station": str(p["station"]),
                              "samples": {str(b): _event_row(s) for b, s in dict(p["samples"]).items()},
                              "paired": {str(b): _event_row(s) for b, s in dict(p.get("paired") or {}).items()},
                              "results": {str(k): int(v) for k, v in dict(p.get("results") or {}).items()},
                              "last_ts": float(p["last_ts"])}
        if v2:
            self.agg = agg
            self.agg_low = self._load_agg(data.get("agg_low"), FIELDS)
            self.event_agg = self._load_agg(data.get("event_agg"), EV_FIELDS)
            self.event_agg_low = self._load_agg(data.get("event_agg_low"), EV_FIELDS)
            self.legacy_agg = self._load_agg(data.get("legacy_agg"), FIELDS)
            self.legacy_scored_markets = int(data.get("legacy_scored_markets") or 0)
            self.scored_markets = int(data.get("scored_markets") or 0)
            for k in ("paired_markets", "paired_markets_low", "scored_events", "paired_events",
                      "paired_events_low"):
                setattr(self, k, int(data.get(k) or 0))
        else:
            self.agg, self.agg_low, self.event_agg, self.event_agg_low = {}, {}, {}, {}
            self.legacy_agg = agg
            self.legacy_scored_markets = int(data.get("scored_markets") or 0)
            self.scored_markets = 0
        self.pending = pending
        self.events = events
        self.dropped = int(data.get("dropped") or 0)
        self._verdict = None
