"""Replay recordings through RunLoop (paper) and journal fills + quote samples.

    python -m mm.ml.replayfills [--dir REC_DIR] [--last N | --files F ...]
        [--policy policy.conf] [--out-dir /var/lib/lip-maker/ml]

Same engine code as the replay bench (mm.replay_bench.run_one): a fresh
RunLoop in paper mode fed the recorded frames in receive order, PaperFill-
Simulator fills, fair-value thread / recorder / PM US poller off, the
production policy env overlaid. Offline only; nothing is sent anywhere.

Writes ``<out-dir>/replay_journal.jsonl`` (replaced each run; the dataset
builder turns rows into point-in-time samples):

* ``source=replay``   one row per replay fill. ``cutoff`` = event time of the
  frame that produced the fill (the print's exchange time, or the crossing book
  frame) so features exclude it. Carries the resting quote's placement ts and
  best0 (fast-move anchor) and our inventory on that side before the fill.
* ``source=quote_bg`` every BG_EVERY_S of loop time, each resting quote side
  (market, side, price, size, placement ts, inventory) with ``filled_60s`` =
  1 if that side got a replay fill within the next 60 s. Background rows for a
  fill-probability model.

Replay fills differ from the live run (RunLoop starts cold at the first file:
no positions, warm-up, selection timing). Labelled ``replay`` and never
counted by the readiness gate.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from mm.ml.dataset import OUT_DIR, REC_DIR, event_ts, frame_kind, list_recordings

BG_EVERY_S = 300.0


def run(paths, env=None, *, warmup_s=60.0, log=print) -> tuple[list, dict]:
    from mm.replay_bench import env_overlay
    from mm.unattended import loop as L
    from mm.unattended.bookrec import iter_frames
    base = {"LIP_SELECTION_DUMP": "off", "LIP_FV_ENABLE": "0", "LIP_RECORD_ENABLE": "0",
            "LIP_PMUS_PAPER_ENABLE": "0"}
    base.update(env or {})
    rows: list = []
    fills: list = []
    cur = {"frame": None}
    t0 = time.time()
    with env_overlay(base):
        loop = L.RunLoop(mode="paper", bankroll=float(os.environ.get("LIP_BANKROLL", 5000)),
                         select_every=float(L.SELECT_EVERY_S), first_select_warmup_s=warmup_s,
                         carry_forward=True)
        orig = loop._record_fill

        def hook(fill, ts):
            fr = cur["frame"] or {}
            market, side = str(fill.get("market_ticker")), str(fill.get("side"))
            q = dict(loop.resting.get(market) or {})
            pos = loop.position.get(market) or {}
            opp = "no" if side == "yes" else "yes"
            ets = event_ts(fr) if fr else ts
            rows.append({"source": "replay", "market": market, "side": side,
                         "price": float(fill.get("price_cents") or 0), "count": float(fill.get("count") or 0),
                         "cutoff": float(ets if ets is not None else ts), "fill_ts": float(ts),
                         "synthetic": bool(fill.get("synthetic")),
                         "prev_poll": bool(fill.get("synthetic")) and frame_kind(fr) == "trade",
                         "quote_ts": q.get("ts"), "quote_best0": list(q.get("best0") or (None, None)),
                         "inventory": float(pos.get(side, 0.0)) - float(pos.get(opp, 0.0)),
                         "fill_kind": fill.get("source") or "print",
                         "jid": f"r:{market}:{side}:{ts:.3f}:{len(rows)}"})
            fills.append((market, side, float(ts)))
            return orig(fill, ts)

        loop._record_fill = hook
        seen_prog: set = set()
        last_ts = None
        next_bg = None
        n = 0
        for frame in iter_frames(paths):
            kind = frame_kind(frame)
            if kind == "ws_raw":
                continue
            if kind == "program":
                if frame.get("market") in seen_prog:
                    continue
                seen_prog.add(frame.get("market"))
            frame.pop("hdr", None)
            if frame.get("ts") is not None and kind not in ("program", "screen", "shard"):
                ts = float(frame["ts"])
                if last_ts is not None and ts < last_ts:
                    frame["ts"] = ts = last_ts
                last_ts = ts
                if next_bg is None:
                    next_bg = ts + warmup_s + BG_EVERY_S
                elif ts >= next_bg:
                    next_bg = ts + BG_EVERY_S
                    ets = event_ts(frame)
                    for market, q in list(loop.resting.items()):
                        pos = loop.position.get(market) or {}
                        for side in ("yes", "no"):
                            if float(q.get(side) or 0) <= 0:
                                continue
                            opp = "no" if side == "yes" else "yes"
                            rows.append({"source": "quote_bg", "market": market, "side": side,
                                         "price": float(q.get(f"{side}_cents") or 0), "count": float(q.get(side) or 0),
                                         "cutoff": float(min(ets, ts) if ets is not None else ts), "sample_ts": ts,
                                         "quote_ts": q.get("ts"), "quote_best0": list(q.get("best0") or (None, None)),
                                         "inventory": float(pos.get(side, 0.0)) - float(pos.get(opp, 0.0)),
                                         "synthetic": market.startswith("PMUS:"),
                                         "jid": f"b:{market}:{side}:{ts:.3f}"})
            cur["frame"] = frame
            loop.on_frame(frame)
            n += 1
        if last_ts is not None:
            loop.on_frame({"type": "clock", "ts": last_ts + 1})
    by = {}
    for m, s, t in fills:
        by.setdefault((m, s), []).append(t)
    end = last_ts or 0.0
    out = []
    for r in rows:
        if r["source"] == "quote_bg":
            t = r["sample_ts"]
            if t + 60.0 > end:
                continue                  # label window not covered
            r["filled_60s"] = int(any(t < x <= t + 60.0 for x in by.get((r["market"], r["side"]), ())))
        out.append(r)
    stats = {"frames": n, "fills": len(fills), "quote_bg": sum(1 for r in out if r["source"] == "quote_bg"),
             "quotes": len(getattr(loop, "quotes", []) or []), "pulls": dict(getattr(loop, "pulls", {}) or {}),
             "wall_s": round(time.time() - t0, 1), "files": [str(p) for p in paths]}
    return out, stats


def main(argv=None) -> int:
    from mm.replay_bench import DEFAULT_POLICY, parse_policy
    ap = argparse.ArgumentParser(prog="python -m mm.ml.replayfills")
    ap.add_argument("--dir", default=REC_DIR)
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--last", type=int, help="replay only the newest N closed files")
    ap.add_argument("--policy", default=DEFAULT_POLICY)
    ap.add_argument("--out-dir", default=OUT_DIR)
    a = ap.parse_args(argv)
    paths = [Path(f) for f in a.files] if a.files else list_recordings(a.dir)[:-1]
    if a.last:
        paths = paths[-a.last:]
    rows, stats = run(paths, parse_policy(a.policy))
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "replay_journal.tmp"
    with tmp.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, out / "replay_journal.jsonl")
    (out / "replay_stats.json").write_text(json.dumps(stats, indent=1))
    print(json.dumps({k: v for k, v in stats.items() if k != "files"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
