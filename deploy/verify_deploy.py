#!/usr/bin/env python3
"""Post-deploy verification (read-only): one GET of /status, loud failure.

    python deploy/verify_deploy.py --expect-commit "$(git rev-parse HEAD)" [--url http://127.0.0.1:8765/status]

Fails (exit 1, every problem listed) unless the engine reports: paper mode, live_armed false,
no kill latch, the engine state loaded without error, the feed connected, a recent frame, no
capital-budget warning, and (with --expect-commit) the commit that was just deployed. It waits
up to --wait-s for the engine to come up after a restart. It changes nothing and never reads or
sets an arming flag. (Knight Capital lesson: a deploy is not done until the box says what it runs.)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def check(status: dict, *, expect_commit: str | None, now: float | None = None,
          max_frame_age_s: float = 300.0) -> list:
    now = time.time() if now is None else float(now)
    if not isinstance(status, dict) or not status:
        return ["status unreadable or empty"]
    fails = []
    if status.get("paper") is not True or status.get("mode") != "paper":
        fails.append(f"not paper (paper={status.get('paper')!r}, mode={status.get('mode')!r})")
    if status.get("live_armed") is not False:
        fails.append(f"live_armed is {status.get('live_armed')!r}, expected False")
    if status.get("kill"):
        fails.append(f"kill latched: {status.get('kill')}")
    state = status.get("state") or {}
    if state.get("error"):
        fails.append(f"state file error: {state.get('error')}")
    if not (status.get("feed") or {}).get("connected"):
        fails.append("feed not connected")
    last = status.get("last_frame_ts")
    if not last or now - float(last) > max_frame_age_s:
        fails.append(f"no recent frame (last_frame_ts={last})")
    if status.get("budget_warning"):
        fails.append(f"budget warning: {status.get('budget_warning')}")
    if expect_commit:
        got = (status.get("build") or {}).get("commit") or ""
        if not got or not (got.startswith(expect_commit) or expect_commit.startswith(got)):
            fails.append(f"commit mismatch: running {got or 'unknown'}, expected {expect_commit}")
    return fails


def fetch(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8765/status")
    ap.add_argument("--expect-commit")
    ap.add_argument("--wait-s", type=float, default=120.0)
    ap.add_argument("--max-frame-age-s", type=float, default=300.0)
    args = ap.parse_args(argv)
    deadline = time.time() + args.wait_s
    fails = ["engine not reachable"]
    while True:
        try:
            fails = check(fetch(args.url), expect_commit=args.expect_commit, max_frame_age_s=args.max_frame_age_s)
        except Exception as exc:
            fails = [f"engine not reachable: {type(exc).__name__}"]
        if not fails or time.time() >= deadline:
            break
        time.sleep(5.0)
    if fails:
        print("DEPLOY VERIFICATION FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("deploy verified: paper, not armed, no kill, state loaded, feed live"
          + (f", commit {args.expect_commit[:12]}" if args.expect_commit else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
