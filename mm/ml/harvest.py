"""Copy live paper fills into a durable journal (offline, read-only on the engine).

    python -m mm.ml.harvest [--out-dir /var/lib/lip-maker/ml] [--status-url URL] [--log-dir DIR]

The engine keeps per-fill detail only in memory (RunLoop.fill_marks, last 20
on the status page as ``fills_detail``) and in the rotating lip.log ("paper
fill ..." lines; 11 x 20 MB, a few hours). This harvester runs every 10 min
(systemd timer), reads both, and appends new fills to
``<out-dir>/fills_live.jsonl``. Status rows are preferred (exact ts, engine
``synthetic`` flag, venue, bucket, mid0); log-only rows are kept with
``ts_source=log`` (millisecond log time, count/price rounded by the log
format, ``synthetic`` inferred: PM US fills are always synthetic/inferred,
Kalshi fills from public prints are not; a Kalshi cross-fill cannot be told
apart in the log and is recorded as non-synthetic). Dedup: same market, side,
price, count within 2 s. Never writes to the engine; GET only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = "/var/lib/lip-maker/ml"
STATUS_URL = "http://127.0.0.1:8765/status"
LOG_DIR = "/var/lib/lip-maker"
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) \S+ lip\.risk paper fill (\S+) (yes|no) "
                  r"([0-9.]+)@([0-9.]+)c mid (\S+) unpaired_yes (\S+)")


def parse_log_line(line: str):
    m = LINE.match(line.strip())
    if not m:
        return None
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc).timestamp()
    market = m.group(2)
    mid = None if m.group(6) == "None" else float(m.group(6))
    return {"ts": ts, "market": market, "side": m.group(3), "count": float(m.group(4)),
            "price_cents": float(m.group(5)), "mid0": mid,
            "venue": "pmus" if market.startswith("PMUS:") else "kalshi",
            "synthetic": market.startswith("PMUS:"), "ts_source": "log"}


def from_status(st: dict) -> list:
    out = []
    for f in st.get("fills_detail") or []:
        try:
            out.append({"ts": float(f["ts"]), "market": str(f["market"]), "side": str(f["side"]),
                        "count": float(f["count"]), "price_cents": float(f["price_cents"]),
                        "mid0": f.get("mid0"), "venue": f.get("venue"), "bucket": f.get("bucket"),
                        "synthetic": bool(f.get("synthetic")), "ts_source": "status",
                        "markout_60s": f.get("markout_60s"), "markout_300s": f.get("markout_300s"),
                        "markout_1800s": f.get("markout_1800s")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _same(a, b) -> bool:
    return (a["market"] == b["market"] and a["side"] == b["side"]
            and abs(float(a["price_cents"]) - float(b["price_cents"])) < 0.6
            and abs(round(float(a["count"])) - round(float(b["count"]))) < 1.0
            and abs(float(a["ts"]) - float(b["ts"])) <= 2.0)


def merge(existing: list, new: list) -> list:
    """New rows not already journaled. Status rows win over log rows."""
    added = []
    for r in sorted(new, key=lambda x: (x["ts_source"] != "status", x["ts"])):
        if any(_same(r, e) for e in existing) or any(_same(r, a) for a in added):
            continue
        added.append(r)
    return added


def harvest(out_dir=OUT_DIR, status_url=STATUS_URL, log_dir=LOG_DIR) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp = out / "fills_live.jsonl"
    existing = []
    if jp.exists():
        for line in jp.read_text().splitlines():
            try:
                existing.append(json.loads(line))
            except ValueError:
                pass
    recent = [e for e in existing if e["ts"] > (max((x["ts"] for x in existing), default=0) - 6 * 3600)]
    new = []
    status_ok = False
    try:
        with urllib.request.urlopen(status_url, timeout=10) as r:
            new += from_status(json.loads(r.read()))
            status_ok = True
    except Exception:
        pass
    logs = sorted(Path(log_dir).glob("lip.log*"))
    for p in logs:
        try:
            with p.open(errors="replace") as fh:
                for line in fh:
                    if "paper fill" in line:
                        row = parse_log_line(line)
                        if row:
                            new.append(row)
        except OSError:
            continue
    added = merge(recent, new)
    if added:
        with jp.open("a") as fh:
            for r in added:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return {"status_ok": status_ok, "seen": len(new), "added": len(added),
            "journal_total": len(existing) + len(added)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mm.ml.harvest")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--status-url", default=STATUS_URL)
    ap.add_argument("--log-dir", default=LOG_DIR)
    a = ap.parse_args(argv)
    print(json.dumps(harvest(a.out_dir, a.status_url, a.log_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
