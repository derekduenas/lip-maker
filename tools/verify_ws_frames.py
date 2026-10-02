#!/usr/bin/env python3
"""Check recorded websocket frames against the fields the engine relies on.

Read-only. Reads the engine's frame recordings (mm/unattended/bookrec.py:
gzip JSON lines ``frames-<UTC>.jsonl.gz`` under LIP_RECORD_DIR, default
/var/lib/lip-maker/recordings) and reports, from REAL captures:

* frame type counts (header rows, ``"hdr": 1``, counted separately);
* the share of Kalshi orderbook snapshots / deltas carrying top-level
  ``sending_ts_ms`` and ``msg.ts_ms`` (what ``loop._exchange_ts`` reads);
* recv_ts - exchange_ts (p50/p95/p99/max) per source, how often |skew|
  exceeds the engine's LIP_CLOCK_SKEW_LIMIT_S, and how many times the
  guard (N consecutive / sustained) would have pulled quotes;
* market_lifecycle_v2 samples and the fields the settle path reads
  (``msg.event_type``, ``msg.market_ticker``, ``msg.result``), plus the
  derived ``settlement`` frames;
* ``subscribed`` / ``ok`` replies: sids named by more than one reply
  (merge evidence) and whether ``ok`` carries ``sid`` / ``seq``; from book
  frames, tickers first seen on a sid well after that sid's first frame
  (later subscribe batch merged into an earlier sid) and tickers seen on
  more than one sid;
* seq gaps per sid (snapshot gaps are the merge resyncs SidSequencer
  allows; delta gaps are real gaps). ``ok`` / ``unsubscribed`` replies
  carrying a seq advance the sid's last seq exactly as the engine's
  SidSequencer does (update_subscription acks consume a seq), so they do not
  read as gaps; ``subscribed`` replies do not (the engine does not check
  them either);
* engine-detected gaps: the dispatcher records a ws_raw ``seq_gap`` row
  (sid, seq, last_seq, market_ticker) before it raises SequenceGap and
  reconnects; counted under ``seq.engine_gaps``. Recordings made before that
  row existed cannot show them.

What the recorder shows. The engine records the frames passed to
RunLoop.on_frame, i.e. AFTER loop._dispatch_ws_message. That function sets
``seq`` to None on book frames and keeps the original under ``ws_seq``;
it turns market_lifecycle_v2 into a derived ``{"kind": "settlement"}`` frame
(determined/settled yes/no only) and also forwards every raw
market_lifecycle_v2 message and every ``subscribed`` / ``unsubscribed`` /
``ok`` reply as ``{"type": "ws_raw", "channel": <type>, "msg": <message>}``
(RunLoop ignores these rows). This tool unwraps ws_raw rows and analyses
them as the original message types (counted under ``ws_raw_rows``).
Recordings made before that change carry none of this: there the seq-gap,
raw-lifecycle and reply checks report ``not recorded`` instead of guessing;
the book-frame merge evidence and the skew checks still work.

Exit status: 0 every engine-required field was seen; 1 a field the engine
depends on is absent from ALL frames of a type that was recorded; 2 no
recordings / no frames.

    sudo -u lip /opt/lip-maker/.venv/bin/python /opt/lip-maker/tools/verify_ws_frames.py --newest 3
    ... --json                  machine-readable report
    ... /path/a.jsonl.gz 'dir/frames-2026100*.jsonl.gz'
"""
from __future__ import annotations

import argparse
import calendar
import glob
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mm.unattended.bookrec import iter_frames, list_files  # noqa: E402

DEFAULT_DIR = "/var/lib/lip-maker/recordings"
LOOP_PY = ROOT / "mm" / "unattended" / "loop.py"
UNIT_CONFIGS = (
    "/etc/systemd/system/lip-unattended.service.d/override.conf",
    "/etc/systemd/system/lip-unattended.service.d/policy.conf",
    "/etc/lip-maker/lip-maker.env",   # EnvironmentFile: overrides Environment= lines
)
SKEW_KEYS = ("LIP_CLOCK_SKEW_LIMIT_S", "LIP_CLOCK_SKEW_N", "LIP_CLOCK_SKEW_SUSTAIN_S")
BOOK_TYPES = ("orderbook_snapshot", "orderbook_delta")
WS_RAW_TYPE = "ws_raw"   # mm.unattended.loop.WS_RAW_TYPE
PMUS_PREFIX = "PMUS:"

# Fields the engine reads, per frame type. Each entry is a group of
# alternatives: satisfied by a frame when any path is present and not null.
# A group satisfied by NO frame of a recorded type is a failure.
DEPENDENCIES = {
    "orderbook_snapshot": [
        ("market ticker", ("msg.market_ticker",)),
        ("sid", ("sid",)),
        ("receive time", ("ts",)),
        ("book levels", ("msg.yes_dollars_fp", "msg.no_dollars_fp", "msg.yes", "msg.no")),
        ("exchange time (clock-skew guard)", ("sending_ts_ms", "msg.ts_ms")),
    ],
    "orderbook_delta": [
        ("market ticker", ("msg.market_ticker",)),
        ("sid", ("sid",)),
        ("receive time", ("ts",)),
        ("side", ("msg.side",)),
        ("price", ("msg.price_dollars", "msg.price_dollars_fp", "msg.price")),
        ("delta", ("msg.delta_fp", "msg.delta")),
        ("exchange time (clock-skew guard)", ("sending_ts_ms", "msg.ts_ms")),
    ],
    "market_lifecycle_v2": [
        ("event type", ("msg.event_type",)),
        ("market ticker", ("msg.market_ticker",)),
    ],
    "settlement": [
        ("market", ("market",)),
        ("result", ("result",)),
    ],
    "subscribed": [
        ("sid", ("msg.sid",)),
        ("channel", ("msg.channel",)),
    ],
    "ok": [
        ("sid", ("sid", "msg.sid")),
    ],
    "trade": [
        ("market ticker", ("trade.market_ticker", "trade.ticker")),
    ],
}
# Checked only over the frames where they apply.
LIFECYCLE_RESULT_EVENTS = ("determined", "settled")


# --------------------------------------------------------------------- helpers
def _get(frame: dict, path: str):
    cur = frame
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _has(frame: dict, path: str) -> bool:
    return _get(frame, path) is not None


def _ftype(frame: dict) -> str:
    return str(frame.get("type") or frame.get("kind") or "?")


def _is_pmus(frame: dict) -> bool:
    mt = _get(frame, "msg.market_ticker") or frame.get("market") or ""
    return str(mt).startswith(PMUS_PREFIX)


def _pct(n: int, d: int):
    return None if d == 0 else round(100.0 * n / d, 2)


def _quantile(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _dist(vals: list) -> dict:
    s = sorted(vals)
    r = lambda v: None if v is None else round(float(v), 4)  # noqa: E731
    return {"n": len(s), "min": r(s[0] if s else None), "p50": r(_quantile(s, 0.5)),
            "p95": r(_quantile(s, 0.95)), "p99": r(_quantile(s, 0.99)),
            "max": r(s[-1] if s else None)}


def _num(raw):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ------------------------------------------------------------- engine config
def loop_skew_defaults(path: Path = LOOP_PY) -> dict:
    """Defaults of the skew guard as written in loop.py (source, not import)."""
    out = {"LIP_CLOCK_SKEW_LIMIT_S": 5.0, "LIP_CLOCK_SKEW_N": 3.0,
           "LIP_CLOCK_SKEW_SUSTAIN_S": 3.0, "source": "built-in fallback"}
    try:
        src = path.read_text()
    except OSError:
        return out
    m = re.search(r"^CLOCK_SKEW_LIMIT_S\s*=\s*([0-9.]+)", src, re.M)
    if m:
        out["LIP_CLOCK_SKEW_LIMIT_S"] = float(m.group(1))
        out["source"] = str(path)
    for key in ("LIP_CLOCK_SKEW_N", "LIP_CLOCK_SKEW_SUSTAIN_S", "LIP_CLOCK_SKEW_LIMIT_S"):
        m = re.search(r'_env_num\(\s*"%s"\s*,\s*([0-9.]+)\s*\)' % key, src)
        if m:
            out[key] = float(m.group(1))
            out["source"] = str(path)
    return out


def _scan_config(path: str) -> dict:
    """LIP_CLOCK_SKEW_* from a unit drop-in (Environment=K=V) or env file (K=V)."""
    found = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return found
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Environment="):
            line = line[len("Environment="):]
        line = line.strip().strip('"')
        for key in SKEW_KEYS:
            if line.startswith(key + "="):
                v = _num(line.split("=", 1)[1].strip().strip('"').strip("'"))
                if v is not None:
                    found[key] = v
    return found


def engine_skew_config(*, cli_limit=None, unit_configs=UNIT_CONFIGS, env=None) -> dict:
    """Effective skew settings: --skew-limit > process env > unit configs > loop.py."""
    env = os.environ if env is None else env
    cfg = loop_skew_defaults()
    src = {k: cfg["source"] for k in SKEW_KEYS}
    for path in unit_configs or ():
        for k, v in _scan_config(path).items():
            cfg[k] = v
            src[k] = path
    for k in SKEW_KEYS:
        v = _num(env.get(k))
        if v is not None:
            cfg[k] = v
            src[k] = "environment"
    if cli_limit is not None:
        cfg["LIP_CLOCK_SKEW_LIMIT_S"] = float(cli_limit)
        src["LIP_CLOCK_SKEW_LIMIT_S"] = "--skew-limit"
    return {"limit_s": cfg["LIP_CLOCK_SKEW_LIMIT_S"], "n": max(1, int(cfg["LIP_CLOCK_SKEW_N"])),
            "sustain_s": cfg["LIP_CLOCK_SKEW_SUSTAIN_S"], "sources": src}


def exchange_time(frame: dict):
    """(epoch s, source) the way loop._exchange_ts derives it."""
    raw = frame.get("sending_ts_ms")
    if raw is not None:
        v = _num(raw)
        return (None if v is None else v / 1000.0), "sending_ts_ms"
    raw = _get(frame, "msg.ts_ms")
    if raw is not None:
        v = _num(raw)
        return (None if v is None else v / 1000.0), "msg.ts_ms"
    return None, None


# ------------------------------------------------------------------ analysis
class _SkewGuard:
    """Replays RunLoop._note_clock_skew to count would-be pulls."""

    def __init__(self, limit_s, n, sustain_s):
        self.limit, self.n, self.sustain = float(limit_s), int(n), float(sustain_s)
        self.streak, self.since, self.active, self.trips = 0, 0.0, False, 0

    def feed(self, ts, ets):
        if abs(ts - ets) <= self.limit:
            self.streak, self.active = 0, False
            return
        if self.streak == 0:
            self.since = ts
        self.streak += 1
        if self.active:
            return
        if self.streak >= self.n or (self.streak >= 2 and ts - self.since >= self.sustain):
            self.active = True
            self.trips += 1


def analyze(frames, *, skew: dict, merge_gap_s: float = 2.0, samples: int = 3) -> dict:
    types = Counter()
    header = Counter()
    present = defaultdict(Counter)          # type -> path -> n frames with it
    keys = defaultdict(Counter)             # type -> top-level/msg key -> n
    n_by_type = Counter()                   # dependency-checked frames per type
    skew_vals = defaultdict(list)           # source -> [recv - exch]
    over = Counter()
    unit_suspect = Counter()
    guard = _SkewGuard(skew["limit_s"], skew["n"], skew["sustain_s"])
    pmus_books = 0
    lifecycle = {"n": 0, "event_types": Counter(), "samples": [], "result_events": 0,
                 "result_events_with_result": 0}
    settlements = {"n": 0, "sources": Counter(), "samples": []}
    replies = {"subscribed": 0, "ok": 0, "unsubscribed": 0, "ok_with_sid": 0, "ok_with_seq": 0,
               "subscribed_with_seq": 0, "sid_replies": defaultdict(list), "samples": []}
    epoch = 0
    sid_first = {}                          # (epoch, sid) -> first ts
    sid_tickers = defaultdict(dict)         # (epoch, sid) -> ticker -> first snapshot ts
    ticker_sids = defaultdict(set)          # (epoch, ticker) -> sids
    seq_last = {}
    seq_src = Counter()
    seq_gaps = defaultdict(lambda: {"snapshot_gaps": 0, "delta_gaps": 0, "dups": 0, "n": 0})
    engine_gaps = {"n": 0, "samples": []}
    ws_raw = Counter()
    ts_min = ts_max = None
    total = 0

    for fr in frames:
        if not isinstance(fr, dict):
            continue
        total += 1
        t = _ftype(fr)
        if fr.get("hdr"):
            header[t] += 1
            continue
        if t == WS_RAW_TYPE:
            # raw websocket message forwarded by loop._dispatch_ws_message
            inner = fr.get("msg")
            if not isinstance(inner, dict):
                continue
            ws_raw[str(fr.get("channel") or _ftype(inner))] += 1
            inner = dict(inner)
            if inner.get("ts") is None:
                inner["ts"] = fr.get("ts")
            fr, t = inner, _ftype(inner)
        types[t] += 1
        ts = _num(fr.get("ts"))
        if ts is not None:
            ts_min = ts if ts_min is None else min(ts_min, ts)
            ts_max = ts if ts_max is None else max(ts_max, ts)
        if t == "disconnect":
            epoch += 1
            continue
        if t in BOOK_TYPES and _is_pmus(fr):
            pmus_books += 1        # PM US poller frames: no sid/sending_ts by design
            continue
        if t in DEPENDENCIES:
            n_by_type[t] += 1
            for _label, paths in DEPENDENCIES[t]:
                for p in paths:
                    if _has(fr, p):
                        present[t][p] += 1
            for k in fr:
                keys[t][k] += 1
            if isinstance(fr.get("msg"), dict):
                for k in fr["msg"]:
                    keys[t]["msg." + k] += 1

        if t in BOOK_TYPES:
            if ts is not None:
                # Each field on its own, then what the engine uses
                # (sending_ts_ms, else msg.ts_ms) and the guard replay on it.
                for src, raw in (("sending_ts_ms", fr.get("sending_ts_ms")),
                                 ("msg.ts_ms", _get(fr, "msg.ts_ms"))):
                    v = _num(raw)
                    if v is None:
                        continue
                    d = ts - v / 1000.0
                    skew_vals[src].append(d)
                    over[src] += int(abs(d) > skew["limit_s"])
                    if abs(d) > 1e6:   # off by 1000x: ms vs s confusion
                        unit_suspect[src] += 1
                ets, _src = exchange_time(fr)
                if ets is not None:
                    d = ts - ets
                    skew_vals["engine"].append(d)
                    over["engine"] += int(abs(d) > skew["limit_s"])
                    guard.feed(ts, ets)
            # sid / ticker merge evidence
            sid = fr.get("sid")
            mt = _get(fr, "msg.market_ticker")
            if sid is not None and mt:
                key = (epoch, sid)
                if key not in sid_first and ts is not None:
                    sid_first[key] = ts
                ticker_sids[(epoch, mt)].add(sid)
                if t == "orderbook_snapshot" and mt not in sid_tickers[key] and ts is not None:
                    sid_tickers[key][mt] = ts
            # seq
            raw_seq = fr.get("ws_seq") if fr.get("ws_seq") is not None else fr.get("seq")
            if raw_seq is not None and sid is not None:
                seq_src["ws_seq" if fr.get("ws_seq") is not None else "seq"] += 1
                try:
                    seq = int(raw_seq)
                except (TypeError, ValueError):
                    seq = None
                if seq is not None:
                    key = (epoch, sid)
                    row = seq_gaps[key]
                    row["n"] += 1
                    last = seq_last.get(key)
                    if last is not None and seq <= last:
                        row["dups"] += 1
                    else:
                        if last is not None and seq > last + 1:
                            row["snapshot_gaps" if t == "orderbook_snapshot" else "delta_gaps"] += 1
                        seq_last[key] = seq
        elif t == "market_lifecycle_v2":
            body = fr.get("msg") if isinstance(fr.get("msg"), dict) else {}
            lifecycle["n"] += 1
            ev = str(body.get("event_type") or "?")
            lifecycle["event_types"][ev] += 1
            if ev in LIFECYCLE_RESULT_EVENTS:
                lifecycle["result_events"] += 1
                if body.get("result") not in (None, ""):
                    lifecycle["result_events_with_result"] += 1
            if len(lifecycle["samples"]) < samples or (
                    ev in LIFECYCLE_RESULT_EVENTS and not any(
                        (s.get("msg") or {}).get("event_type") in LIFECYCLE_RESULT_EVENTS
                        for s in lifecycle["samples"])):
                lifecycle["samples"].append(fr)
        elif t == "settlement":
            settlements["n"] += 1
            settlements["sources"][str(fr.get("source") or "ws_lifecycle (default)")] += 1
            if len(settlements["samples"]) < samples:
                settlements["samples"].append(fr)
        elif t == "seq_gap":
            engine_gaps["n"] += 1
            if len(engine_gaps["samples"]) < samples:
                engine_gaps["samples"].append(fr)
        elif t in ("subscribed", "ok", "unsubscribed"):
            replies[t] += 1
            body = fr.get("msg") if isinstance(fr.get("msg"), dict) else {}
            sid = fr.get("sid", body.get("sid"))
            if t != "subscribed" and sid is not None and fr.get("seq") is not None:
                # as loop._dispatch_ws_message: the ack's seq is checked by
                # SidSequencer, so the next book frame is not a gap
                try:
                    rseq = int(fr["seq"])
                except (TypeError, ValueError):
                    rseq = None
                key = (epoch, sid)
                if rseq is not None and (seq_last.get(key) is None or rseq > seq_last[key]):
                    seq_last[key] = rseq
            if t == "ok":
                replies["ok_with_sid"] += int(sid is not None)
                replies["ok_with_seq"] += int(fr.get("seq") is not None)
            if t == "subscribed":
                replies["subscribed_with_seq"] += int(fr.get("seq") is not None)
            if sid is not None and t in ("subscribed", "ok"):
                replies["sid_replies"][(epoch, sid)].append(t)
            if len(replies["samples"]) < samples:
                replies["samples"].append(fr)

    # ---- dependency verdicts
    deps, failures = [], []
    for t, groups in DEPENDENCIES.items():
        n = n_by_type.get(t, 0)
        if n == 0:
            continue
        for label, paths in groups:
            hit = max((present[t][p] for p in paths), default=0)
            any_n = sum(present[t][p] for p in paths)
            status = "ok" if hit == n else ("missing" if any_n == 0 else "partial")
            row = {"type": t, "field": label, "paths": list(paths), "frames": n,
                   "with_field": {p: present[t][p] for p in paths}, "status": status}
            deps.append(row)
            if status == "missing":
                failures.append(f"{t}: {label} ({' | '.join(paths)}) absent from all {n} frames")
    if lifecycle["result_events"] and lifecycle["result_events_with_result"] == 0:
        failures.append("market_lifecycle_v2: msg.result absent from all "
                        f"{lifecycle['result_events']} determined/settled events")

    # ---- merge evidence from book frames
    late = []
    for key, tick in sid_tickers.items():
        first = sid_first.get(key)
        for mt, at in tick.items():
            if first is not None and at - first > merge_gap_s:
                late.append({"epoch": key[0], "sid": key[1], "ticker": mt,
                             "first_snapshot_after_sid_start_s": round(at - first, 3)})
    multi_sid = [{"epoch": k[0], "ticker": k[1], "sids": sorted(v, key=str)}
                 for k, v in ticker_sids.items() if len(v) > 1]
    multi_reply = [{"epoch": k[0], "sid": k[1], "replies": v}
                   for k, v in replies["sid_replies"].items() if len(v) > 1]

    book_n = {t: n_by_type.get(t, 0) for t in BOOK_TYPES}
    book = {}
    for t in BOOK_TYPES:
        n = book_n[t]
        book[t] = {"n": n,
                   "pct_sending_ts_ms": _pct(present[t]["sending_ts_ms"], n),
                   "pct_msg_ts_ms": _pct(present[t]["msg.ts_ms"], n),
                   "keys": dict(keys[t].most_common())}

    seq_present = sum(seq_src.values())
    notes = []
    any_book = sum(book_n.values())
    if any_book and seq_present == 0:
        notes.append("seq not recorded: book frames carry neither seq nor ws_seq (a recording "
                     "made before loop._dispatch_ws_message kept the original seq as ws_seq); "
                     "seq-gap counts are not available")
    if lifecycle["n"] == 0:
        notes.append("raw market_lifecycle_v2 messages not recorded: no ws_raw lifecycle rows in "
                     "this window (a recording made before the dispatcher forwarded them, or no "
                     "lifecycle traffic); only derived settlement frames are available")
    if replies["subscribed"] + replies["ok"] == 0:
        notes.append("subscribed/ok replies not recorded: no ws_raw reply rows in this window (a "
                     "recording made before the dispatcher forwarded them, or a file that starts "
                     "after the subscribes); merge evidence below comes from book frames only")
    if any(unit_suspect.values()):
        notes.append(f"exchange-time unit suspect (|skew| > 1e6 s): {dict(unit_suspect)}")

    return {
        "frames_total": total,
        "window": {"first_ts": ts_min, "last_ts": ts_max,
                   "hours": None if ts_min is None else round((ts_max - ts_min) / 3600.0, 3)},
        "type_counts": dict(types.most_common()),
        "header_counts": dict(header.most_common()),
        "pmus_book_frames_skipped": pmus_books,
        "ws_raw_rows": dict(ws_raw.most_common()),
        "connections": epoch + 1,
        "book": book,
        "skew": {
            "limit_s": skew["limit_s"], "n": skew["n"], "sustain_s": skew["sustain_s"],
            "config_sources": skew.get("sources", {}),
            "recv_minus_exchange_s": {k: _dist(v) for k, v in skew_vals.items()},
            "over_limit": {k: {"n": over[k], "pct": _pct(over[k], len(skew_vals[k]))}
                           for k in skew_vals},
            "guard_would_pull": guard.trips,
        },
        "lifecycle": {"raw": {"n": lifecycle["n"],
                              "event_types": dict(lifecycle["event_types"]),
                              "determined_or_settled": lifecycle["result_events"],
                              "with_result": lifecycle["result_events_with_result"],
                              "samples": lifecycle["samples"]},
                      "settlement_frames": {"n": settlements["n"],
                                            "sources": dict(settlements["sources"]),
                                            "samples": settlements["samples"]}},
        "subscriptions": {
            "replies": {k: replies[k] for k in ("subscribed", "ok", "unsubscribed", "ok_with_sid",
                                                  "ok_with_seq", "subscribed_with_seq")},
            "sids_in_multiple_replies": multi_reply,
            "reply_samples": replies["samples"],
            "late_tickers_on_sid": late,
            "merge_gap_s": merge_gap_s,
            "tickers_on_multiple_sids": multi_sid,
            "sids_seen": len(sid_first),
        },
        "seq": {"source": dict(seq_src), "recorded": seq_present > 0,
                "per_sid": [{"epoch": k[0], "sid": k[1], **v} for k, v in sorted(
                    seq_gaps.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))],
                "snapshot_gaps": sum(v["snapshot_gaps"] for v in seq_gaps.values()),
                "delta_gaps": sum(v["delta_gaps"] for v in seq_gaps.values()),
                "engine_gaps": engine_gaps},
        "dependencies": deps,
        "failures": failures,
        "notes": notes,
    }


# ------------------------------------------------------------------- output
def _fmt_dist(d: dict) -> str:
    if not d or not d.get("n"):
        return "n=0"
    return (f"n={d['n']} p50={d['p50']:+.3f}s p95={d['p95']:+.3f}s "
            f"p99={d['p99']:+.3f}s max={d['max']:+.3f}s min={d['min']:+.3f}s")


def render(rep: dict) -> str:
    out = []
    w = rep["window"]
    out.append(f"files: {len(rep.get('files', []))}  frames: {rep['frames_total']}  "
               f"window: {w['hours']} h  connections: {rep['connections']}")
    out.append("frame types: " + ", ".join(f"{k}={v}" for k, v in rep["type_counts"].items()))
    if rep["header_counts"]:
        out.append("header rows: " + ", ".join(f"{k}={v}" for k, v in rep["header_counts"].items()))
    if rep.get("ws_raw_rows"):
        out.append("raw ws rows (unwrapped below): "
                   + ", ".join(f"{k}={v}" for k, v in rep["ws_raw_rows"].items()))
    if rep["pmus_book_frames_skipped"]:
        out.append(f"PM US poller book frames (not Kalshi ws, skipped): {rep['pmus_book_frames_skipped']}")
    out.append("")
    out.append("Kalshi book frames (exchange time for the clock-skew guard):")
    for t, b in rep["book"].items():
        out.append(f"  {t}: n={b['n']} sending_ts_ms={b['pct_sending_ts_ms']}% msg.ts_ms={b['pct_msg_ts_ms']}%")
    s = rep["skew"]
    out.append(f"recv_ts - exchange_ts (limit {s['limit_s']}s, N={s['n']}, sustain {s['sustain_s']}s):")
    for k, d in s["recv_minus_exchange_s"].items():
        o = s["over_limit"].get(k, {})
        out.append(f"  {k:14s} {_fmt_dist(d)}  over limit: {o.get('n', 0)} ({o.get('pct')}%)")
    out.append(f"  guard would have pulled quotes {s['guard_would_pull']} time(s)")
    out.append("")
    lc = rep["lifecycle"]
    out.append(f"market_lifecycle_v2 raw: n={lc['raw']['n']} events={lc['raw']['event_types']} "
               f"determined/settled={lc['raw']['determined_or_settled']} with result={lc['raw']['with_result']}")
    for smp in lc["raw"]["samples"]:
        out.append("  sample: " + json.dumps(smp, sort_keys=True)[:400])
    out.append(f"settlement frames: n={lc['settlement_frames']['n']} sources={lc['settlement_frames']['sources']}")
    for smp in lc["settlement_frames"]["samples"]:
        out.append("  sample: " + json.dumps(smp, sort_keys=True)[:300])
    out.append("")
    sub = rep["subscriptions"]
    out.append(f"subscription replies: {sub['replies']}")
    out.append(f"sids named by more than one subscribed/ok reply: {len(sub['sids_in_multiple_replies'])}")
    for r in sub["sids_in_multiple_replies"][:10]:
        out.append(f"  epoch {r['epoch']} sid {r['sid']}: {r['replies']}")
    out.append(f"tickers first seen on a sid > {sub['merge_gap_s']}s after the sid's first frame "
               f"(later batch merged): {len(sub['late_tickers_on_sid'])} (sids seen: {sub['sids_seen']})")
    for r in sub["late_tickers_on_sid"][:10]:
        out.append(f"  epoch {r['epoch']} sid {r['sid']} {r['ticker']} +{r['first_snapshot_after_sid_start_s']}s")
    out.append(f"tickers on more than one sid: {len(sub['tickers_on_multiple_sids'])}")
    out.append("")
    sq = rep["seq"]
    if sq["recorded"]:
        out.append(f"seq ({sq['source']}): snapshot gaps (merge resyncs)={sq['snapshot_gaps']} "
                   f"delta gaps (real)={sq['delta_gaps']}")
        for r in sq["per_sid"]:
            if r["snapshot_gaps"] or r["delta_gaps"] or r["dups"]:
                out.append(f"  epoch {r['epoch']} sid {r['sid']}: n={r['n']} snap_gaps={r['snapshot_gaps']} "
                           f"delta_gaps={r['delta_gaps']} dups={r['dups']}")
    else:
        out.append("seq: not recorded")
    out.append(f"engine-detected seq gaps: {sq['engine_gaps']['n']} (ws_raw seq_gap rows; none in "
               "recordings made before the dispatcher wrote them)")
    for smp in sq["engine_gaps"]["samples"]:
        out.append("  sample: " + json.dumps(smp, sort_keys=True)[:300])
    out.append("")
    out.append("engine-required fields:")
    for d in rep["dependencies"]:
        mark = {"ok": "ok     ", "partial": "PARTIAL", "missing": "MISSING"}[d["status"]]
        out.append(f"  [{mark}] {d['type']}: {d['field']} {d['with_field']} of {d['frames']}")
    for n in rep["notes"]:
        out.append(f"note: {n}")
    out.append("")
    if rep["failures"]:
        out.append("FAIL:")
        out.extend(f"  {f}" for f in rep["failures"])
    else:
        out.append("OK: every engine-required field was seen in the recorded frame types")
    return "\n".join(out)


# --------------------------------------------------------------------- main
_START = re.compile(r"(\d{8}T\d{6}Z)")


def file_start_ts(path) -> float:
    """When a recording was opened: the UTC stamp in its name
    (frames-YYYYmmddTHHMMSSZ[-n].jsonl.gz), else its mtime."""
    m = _START.search(Path(path).name)
    if m:
        try:
            return calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%SZ"))
        except ValueError:
            pass
    return Path(path).stat().st_mtime


def resolve_paths(paths, directory, newest, min_age_s: float = 0.0, now: float | None = None) -> list:
    """Explicit files/globs, else the recordings in ``directory``; from the
    directory, files opened less than ``min_age_s`` ago are skipped (the file
    a just-restarted engine is writing), then the newest ``newest`` kept."""
    files = []
    for p in paths or ():
        hits = sorted(glob.glob(p))
        files.extend(hits if hits else [p])
    if not files:
        found = list_files(directory)
        if min_age_s > 0:
            now = time.time() if now is None else float(now)
            found = [p for p in found if now - file_start_ts(p) >= min_age_s]
        files = [str(p) for p in (found[-newest:] if newest else found)]
    elif newest:
        files = files[-newest:]
    return [f for f in files if Path(f).is_file()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="recording files or globs (default: --dir)")
    ap.add_argument("--dir", default=os.environ.get("LIP_RECORD_DIR", DEFAULT_DIR))
    ap.add_argument("--newest", type=int, default=0, help="only the newest N files")
    ap.add_argument("--min-age-s", type=float, default=0.0,
                    help="with --dir: skip recordings opened less than this many seconds ago "
                         "(deploy.sh: 600, so the file the restarted engine just opened is not the one checked)")
    ap.add_argument("--skew-limit", type=float, default=None,
                    help="override LIP_CLOCK_SKEW_LIMIT_S (default: env, unit config, loop.py)")
    ap.add_argument("--unit-config", action="append", default=None,
                    help="unit drop-in / env file to read LIP_CLOCK_SKEW_* from (repeatable); "
                         "default: the installed lip-unattended drop-ins and /etc/lip-maker/lip-maker.env")
    ap.add_argument("--merge-gap-s", type=float, default=2.0)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    files = resolve_paths(a.paths, a.dir, a.newest, a.min_age_s)
    if not files:
        print(f"no recordings found ({a.paths or a.dir})", file=sys.stderr)
        return 2
    skew = engine_skew_config(cli_limit=a.skew_limit,
                              unit_configs=UNIT_CONFIGS if a.unit_config is None else a.unit_config)
    rep = analyze(iter_frames(files), skew=skew, merge_gap_s=a.merge_gap_s, samples=a.samples)
    rep["files"] = files
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    else:
        print(render(rep))
    if rep["frames_total"] == 0:
        return 2
    return 1 if rep["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
