"""Patch 19: offline replay bench for recorded frame streams (paper only).

    python -m mm.replay bench [--dir /var/lib/lip-maker/recordings] [--files F ...]
        [--since-hours H] [--policy /etc/systemd/system/lip-unattended.service.d/policy.conf]
        [--config NAME:KEY=VAL,KEY=VAL ...] [--json OUT]

Each config replays the same recorded frames through a fresh ``RunLoop``
(the production selection, sizing, quoting, guards, skew and reward accrual
code) in paper mode with carry-forward accrual. Fills come from
``PaperFillSimulator``: we join LAST in queue at our price (queue ahead =
displayed size at our price when placed), orders activate after a latency,
and only recorded public trades at/through our price consume queue then us.

Reported per config: reward score (raw $ accrued before the $1/period floor
and the payable estimate), fills, premium, maker fees, markout (MTM vs mid at
the end of the data, plus 60/300/1800 s), net $ = raw rewards + markout - fees,
and $/day extrapolations. No network, no orders, fair-value thread off.
Short recordings give noisy numbers: fills are rare events.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

DEFAULT_DIR = "/var/lib/lip-maker/recordings"
DEFAULT_POLICY = "/etc/systemd/system/lip-unattended.service.d/policy.conf"


def parse_policy(path: str | None) -> dict:
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("Environment="):
            continue
        body = line[len("Environment="):].strip().strip('"')
        if "=" in body:
            k, v = body.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def queue_band_configs() -> list:
    """Configs of the queue-model sensitivity band, most pessimistic first
    (execution/paper_fills.py ``queue_model``)."""
    out = [("q_risk_averse", {"LIP_SIM_QUEUE_MODEL": "risk_averse"}),
           ("q_depletion", {"LIP_SIM_QUEUE_MODEL": "depletion"})]
    for n in (3, 2, 1):      # n sharpens the depletion split; the effect is not monotone, the band is the min/max
        out.append((f"q_prob_n{n}", {"LIP_SIM_QUEUE_MODEL": "prob_power", "LIP_SIM_QUEUE_POWER": str(n)}))
    return out


def band(values: dict) -> dict:
    """{min, max} of a name -> number mapping (the sensitivity band)."""
    nums = [v for v in values.values() if v is not None]
    return {"min": min(nums), "max": max(nums)} if nums else {"min": None, "max": None}


def parse_config(spec: str) -> tuple:
    name, _, body = spec.partition(":")
    env = {}
    for item in filter(None, (x.strip() for x in body.split(","))):
        k, _, v = item.partition("=")
        env[k.strip()] = v.strip()
    return name or "cfg", env


@contextlib.contextmanager
def env_overlay(env: dict):
    old = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def select_files(directory: str | None, files: list | None, since_hours: float | None) -> list:
    from mm.unattended.bookrec import list_files
    if files:
        return [Path(f) for f in files]
    paths = list_files(directory or DEFAULT_DIR)
    if since_hours:
        cut = time.time() - since_hours * 3600.0
        paths = [p for p in paths if p.stat().st_mtime >= cut]
    return paths


def run_one(paths: list, env: dict, *, bankroll: float | None = None,
            select_every: float | None = None, warmup_s: float = 60.0,
            fee_type: str = "quadratic_with_maker_fees", until_ts: float | None = None) -> dict:
    from mm.accounting import kalshi_fee_usd
    from mm.unattended.bookrec import iter_frames
    from mm.unattended import loop as L
    base = {"LIP_SELECTION_DUMP": "off", "LIP_FV_ENABLE": "0", "LIP_RECORD_ENABLE": "0",
            "LIP_PMUS_PAPER_ENABLE": "0"}
    base.update(env)
    t0 = time.time()
    with env_overlay(base):
        bk = float(bankroll if bankroll is not None else os.environ.get("LIP_BANKROLL", 5000))
        loop = L.RunLoop(mode="paper", bankroll=bk,
                         select_every=float(select_every or L.SELECT_EVERY_S),
                         first_select_warmup_s=warmup_s, carry_forward=True)
        n = 0
        first_ts = last_ts = None
        seen_prog: set = set()
        kinds: dict = {}
        for frame in iter_frames(paths):
            kind = str(frame.get("kind") or frame.get("type") or "")
            if kind == "ws_raw":
                continue  # raw websocket evidence rows (verify_ws_frames); not loop input
            if kind == "program":
                # A program row repeats in each file header; re-adding resets accrual.
                if frame.get("market") in seen_prog:
                    continue
                seen_prog.add(frame.get("market"))
            frame.pop("hdr", None)
            if frame.get("ts") is not None and kind not in ("program", "screen", "shard"):
                ts = float(frame["ts"])
                if until_ts is not None and ts > until_ts:
                    break  # same data window for every config (files may still be growing)
                if last_ts is not None and ts < last_ts:
                    frame["ts"] = last_ts  # recorder is single-writer; guard anyway
                    ts = last_ts
                first_ts = ts if first_ts is None else first_ts
                last_ts = ts
            kinds[kind] = kinds.get(kind, 0) + 1
            loop.on_frame(frame)
            n += 1
        if last_ts is not None:
            loop.on_frame({"type": "clock", "ts": last_ts + 1})
        acc = loop.live_accrual()
        raw = float(sum((v["raw_usd"] for v in acc.values()), Decimal(0)))
        payable = float(sum(loop.live_estimates().values(), Decimal(0)))
        fees = 0.0
        premium = 0.0
        for f in loop.fills:
            premium += float(f["count"]) * float(f["price_cents"]) / 100.0
            try:
                fees += float(kalshi_fee_usd(int(f["price_cents"]), Decimal(str(f["count"])), fee_type=fee_type, is_taker=False))
            except Exception:
                pass
        buckets = loop.bucket_report(acc)
        markout_end = sum(b["markout_usd"] for b in buckets.values())
        dur = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else 0.0
        net = raw + markout_end - fees
        day = 86400.0 / dur if dur > 0 else 0.0
        quoted_s = sum(int(v["known"]) for v in acc.values())
        return {
            "frames": n, "kinds": kinds, "duration_s": round(dur, 1),
            "first_ts": first_ts, "last_ts": last_ts,
            "programs": len(loop.programs), "selections": loop.selection_count,
            "quotes": len(loop.quotes), "repegs": loop.repegs_n, "pulls": dict(loop.pulls),
            "resting_end": len(loop.resting),
            "capital_end_usd": round(float(sum(loop.committed.values(), Decimal(0))), 2),
            "scored_market_seconds": quoted_s,
            "reward_raw_usd": round(raw, 4), "reward_payable_usd": round(payable, 4),
            "fee_type": fee_type, "fills": len(loop.fills), "premium_usd": round(premium, 4), "fees_usd": round(fees, 4),
            "markout_end_usd": round(markout_end, 4), "markouts": loop.markout_summary(),
            "net_usd": round(net, 4),
            "reward_raw_per_day": round(raw * day, 2), "net_per_day": round(net * day, 2),
            "skew": loop._skew_status(), "kill": loop.kill,
            "wall_s": round(time.time() - t0, 2),
        }


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mm.replay bench")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--since-hours", type=float)
    ap.add_argument("--policy", default=DEFAULT_POLICY,
                    help="systemd drop-in whose Environment= lines form the base config ('' = none)")
    ap.add_argument("--config", action="append", default=[],
                    help="NAME:KEY=VAL,KEY=VAL (repeatable). Default: one 'base' run")
    ap.add_argument("--queue-band", action="store_true",
                    help="also run the queue-model sensitivity band (risk_averse, depletion, prob_power n=3,2,1) "
                         "and print the fills/net band: expected fills are a RANGE, not one number")
    ap.add_argument("--bankroll", type=float)
    ap.add_argument("--select-every", type=float)
    ap.add_argument("--warmup-s", type=float, default=60.0)
    ap.add_argument("--fee-type", default="quadratic_with_maker_fees",
                    help="Kalshi maker fee schedule for fills (conservative default charges maker fees)")
    ap.add_argument("--until-ts", type=float,
                    help="ignore frames after this epoch (default: bench start, so configs see the same window)")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    paths = select_files(args.dir, args.files, args.since_hours)
    if not paths:
        print(json.dumps({"error": "no recording files", "dir": args.dir}))
        return 2
    base = parse_policy(args.policy)
    configs = [parse_config(c) for c in args.config] or [("base", {})]
    if args.queue_band:
        configs = configs + queue_band_configs()
    until = args.until_ts if args.until_ts is not None else time.time()
    out_until = until
    out = {"files": [str(p) for p in paths], "policy": args.policy, "results": {}}
    out["until_ts"] = out_until
    for name, env in configs:
        merged = dict(base, **env)
        out["results"][name] = dict(run_one(paths, merged, bankroll=args.bankroll,
                                            select_every=args.select_every,
                                            warmup_s=args.warmup_s, fee_type=args.fee_type,
                                            until_ts=until),
                                    overrides=env)
    if args.queue_band:
        qs = {n: out["results"][n] for n, _e in queue_band_configs()}
        out["queue_band"] = {"fills": band({n: r["fills"] for n, r in qs.items()}),
                             "net_usd": band({n: r["net_usd"] for n, r in qs.items()}),
                             "note": "same recordings, same selection; only the queue model differs. "
                                     "Not a calibration: see docs/SIMULATOR_VALIDATION.md"}
    text = json.dumps(out, indent=2, default=str)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    cols = ("duration_s", "selections", "quotes", "fills", "reward_raw_usd", "markout_end_usd",
            "fees_usd", "net_usd", "reward_raw_per_day", "net_per_day")
    print("config".ljust(14) + "".join(c[:16].rjust(17) for c in cols))
    for name, r in out["results"].items():
        print(name[:14].ljust(14) + "".join(str(r.get(c))[:16].rjust(17) for c in cols))
    if args.queue_band:
        qb = out["queue_band"]
        print(f"\nqueue-model band: fills {qb['fills']['min']}..{qb['fills']['max']}, "
              f"net ${qb['net_usd']['min']}..${qb['net_usd']['max']}")
    if not args.json:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
