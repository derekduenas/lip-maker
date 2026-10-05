#!/usr/bin/env python3
"""Performance log + trend report for the paper engine (read-only).

    python tools/perf_log.py log            # append one snapshot of /status (run every 5 min from cron)
    python tools/perf_log.py report         # rates over the last 1 h / 6 h / 24 h, and a plain verdict

One /status reading says little: the estimated-reward total is cumulative since the state file began,
so only its GROWTH shows whether the engine is earning. This logs the few numbers that matter and
turns them into rates: estimated rewards per day, fills per day, how much of the time anything was
resting, and how that compares with the engine's own plan ($/day it expected to earn).
Estimates only: paper fills, rewards are never paid money.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

DEFAULT_FILE = "/var/lib/lip-maker/perf.jsonl"
WINDOWS = (("1h", 3600.0), ("6h", 6 * 3600.0), ("24h", 24 * 3600.0))
KEEP_S = 8 * 86400.0


def snapshot(s: dict, now: float | None = None) -> dict:
    k = (s.get("venues") or {}).get("kalshi") or {}
    cap = (s.get("capital") or {}).get("kalshi") or {}
    acc = s.get("accrual_seconds") or {}
    ck = s.get("checkpoint") or {}
    m5 = ck.get("markout_5m") or {}
    pulls = s.get("pulls") or {}
    return {
        "ts": float(now if now is not None else time.time()),
        "rewards_est": (s.get("pnl_attribution") or {}).get("est_rewards_kalshi_usd"),
        "pnl": s.get("pnl_usd"),
        "fills": s.get("fills_n"), "fills_sample": (ck.get("kalshi_fills") or {}).get("from_sampling_group"),
        "quotes": s.get("quotes_n"), "resting": s.get("resting_n"), "selected": s.get("selected_n"),
        "plan_day": k.get("plan_net_usd_per_day"),
        "budget": cap.get("budget_usd"), "locked": cap.get("locked_usd"), "starved": (s.get("capital") or {}).get("starved"),
        "known_s": acc.get("known"), "idle_s": acc.get("idle"),
        "pulls": sum(v for v in pulls.values() if isinstance(v, (int, float))),
        "mk5_n": m5.get("fills"), "mk5_c": m5.get("cents_per_contract"),
        "frozen": ((s.get("series_gate") or {}).get("frozen_days")),
    }


def append(path: str, row: dict) -> None:
    rows = load(path)
    rows.append(row)
    cut = row["ts"] - KEEP_S
    rows = [r for r in rows if r.get("ts", 0) >= cut]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load(path: str) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _delta(a: dict, b: dict, key: str):
    x, y = a.get(key), b.get(key)
    return None if x is None or y is None else float(y) - float(x)


def report(rows: list, now: float | None = None) -> dict:
    rows = sorted((r for r in rows if "ts" in r), key=lambda r: r["ts"])
    if len(rows) < 2:
        return {"windows": {}, "samples": len(rows), "flags": ["not enough samples yet (log every 5 min)"]}
    now = rows[-1]["ts"] if now is None else float(now)
    out = {"windows": {}, "samples": len(rows), "flags": []}
    for name, span in WINDOWS:
        win = [r for r in rows if r["ts"] >= now - span]
        if len(win) < 2:
            continue
        a, b = win[0], win[-1]
        hours = (b["ts"] - a["ts"]) / 3600.0
        if hours < 0.1:
            continue
        d_rew, d_fills, d_quotes = _delta(a, b, "rewards_est"), _delta(a, b, "fills"), _delta(a, b, "quotes")
        d_known, d_idle = _delta(a, b, "known_s"), _delta(a, b, "idle_s")
        resting = [r["resting"] for r in win if r.get("resting") is not None]
        plans = [float(r["plan_day"]) for r in win if r.get("plan_day") is not None]
        rew_day = None if d_rew is None else d_rew / hours * 24.0
        plan = sum(plans) / len(plans) if plans else None
        out["windows"][name] = {
            "hours": round(hours, 2), "rewards_per_day": None if rew_day is None else round(rew_day, 2),
            "fills_per_day": None if d_fills is None else round(d_fills / hours * 24.0, 1),
            "quotes_per_hour": None if d_quotes is None else round(d_quotes / hours, 1),
            "resting_avg": round(sum(resting) / len(resting), 1) if resting else None,
            "quoting_share": round(sum(1 for x in resting if x > 0) / len(resting), 2) if resting else None,
            "idle_share": (None if d_known is None or d_idle is None or (d_known + d_idle) <= 0
                           else round(d_idle / (d_known + d_idle), 2)),
            "plan_per_day": None if plan is None else round(plan, 2),
            "realized_vs_plan": (None if plan is None or plan <= 0 or rew_day is None else round(rew_day / plan, 2)),
            "starved_share": round(sum(1 for r in win if r.get("starved")) / len(win), 2),
            "markout5_c": b.get("mk5_c"), "markout5_n": b.get("mk5_n"),
        }
    w = out["windows"].get("6h") or out["windows"].get("1h") or out["windows"].get("24h")
    if w:
        if (w.get("starved_share") or 0) > 0.3:
            out["flags"].append("capital-starved for much of the window: nothing to quote with")
        elif w.get("quoting_share") is not None and w["quoting_share"] < 0.5:
            out["flags"].append("often nothing resting although capital is free: selection or guards are blocking quotes")
        if w.get("idle_share") is not None and w["idle_share"] > 0.8:
            out["flags"].append(f"{int(w['idle_share'] * 100)}% of market-seconds idle in this window")
        if w.get("realized_vs_plan") is not None and w["realized_vs_plan"] < 0.4:
            out["flags"].append(f"estimated rewards are {w['realized_vs_plan']:.0%} of the engine's own plan "
                                f"(${w['rewards_per_day']}/day vs ${w['plan_per_day']}/day): the plan overstates, "
                                "or quotes are not resting where the scorer pays")
        if w.get("rewards_per_day") is not None and w["rewards_per_day"] < 1.0 and (w.get("quoting_share") or 0) > 0.5:
            out["flags"].append("quoting but earning under $1/day: pools are small or share is tiny")
    if not out["flags"]:
        out["flags"].append("no structural problem seen in the logged window")
    return out


def render(rep: dict) -> str:
    lines = [f"samples: {rep.get('samples')}"]
    for name, w in rep.get("windows", {}).items():
        lines.append(f"[{name}] {w['hours']}h  est rewards ${w['rewards_per_day']}/day (plan ${w['plan_per_day']}/day, "
                     f"{w['realized_vs_plan']}x)  fills {w['fills_per_day']}/day  resting avg {w['resting_avg']} "
                     f"(quoting {w['quoting_share']} of samples)  idle {w['idle_share']}  starved {w['starved_share']}  "
                     f"5m markout {w['markout5_c']}c/{w['markout5_n']}")
    lines += ["VERDICT   " + f for f in rep.get("flags", [])]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("log", "report"))
    ap.add_argument("--url", default="http://127.0.0.1:8765/status")
    ap.add_argument("--file", default=DEFAULT_FILE)
    args = ap.parse_args(argv)
    if args.cmd == "log":
        with urllib.request.urlopen(args.url, timeout=10) as resp:
            append(args.file, snapshot(json.loads(resp.read().decode("utf-8"))))
        return 0
    print(render(report(load(args.file))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
