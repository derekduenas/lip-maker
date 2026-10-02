"""Forward parity check — compare our model vs CME FedWatch on TODAY's data.

For each upcoming FOMC, scrape Investing.com (CME FedWatch mirror):
  - ZQ contract settle (FedWatch's underlying)
  - Published bucket probabilities

Run our decision_probs() on each contract's settle, compare to CME's
published. Log to dislocation.db parity_log.

Usage:
    python -m tools.dislocation_parity_check          # run + log + print
    python -m tools.dislocation_parity_check --gate   # also evaluate gate
    python -m tools.dislocation_parity_check --json

Cron nightly. After ~30 days of accumulated samples (n>=30 next-meeting
points), gate evaluates: passes when |our - cme| < 1pp on >=90%.

NOTE: Only the NEXT FOMC has a clean "pre-meeting target" (current Fed
funds target). For meetings beyond next, CME publishes path-conditional
marginals while we assume the current target as pre — these comparisons
are biased and excluded from the gate by default (--include-far to
include them).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dislocation.pricing.fed_funds import FOMCContext, decision_probs

_log = logging.getLogger(__name__)


INVESTING_URL = "https://www.investing.com/central-banks/fed-rate-monitor"
DB_PATH = Path("data/dislocation.db")
MONTH_CODES = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}
MONTH_NAMES = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
               "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


@dataclass
class ParityRow:
    snapshot_date: dt.date
    fomc_date:     dt.date
    contract:      str
    zq_settle:     float
    target_lower:  float
    cme_prob:      float
    our_prob:      float
    abs_err_pp:    float
    is_next_meeting: bool


def fetch_investing_fed() -> str:
    """Pull the public FedWatch mirror page."""
    r = requests.get(
        INVESTING_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh)"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def parse_meetings(html: str) -> list[tuple[dt.date, float]]:
    """Extract [(meeting_date, zq_future_price), ...] in markup order."""
    pattern = (r'Meeting Time:.*?<i>([A-Z][a-z]{2}) (\d{1,2}), ?(\d{4})'
               r'[^<]*</i>.*?Future Price:.*?<i>([0-9.]+)</i>')
    hits = re.findall(pattern, html, re.DOTALL)
    out = []
    seen = set()
    for mon, day, year, px in hits:
        try:
            d = dt.date(int(year), MONTH_NAMES[mon], int(day))
            if d in seen:
                continue
            seen.add(d)
            out.append((d, float(px)))
        except (ValueError, KeyError):
            continue
    return out


def parse_prob_tables(html: str) -> list[list[tuple[float, float]]]:
    """Extract probability tables. Returns list (per meeting) of
    [(target_lower_decimal, prob), ...]."""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for tab in soup.find_all("table"):
        rows = tab.find_all("tr")
        if not rows:
            continue
        # Need header with "Target Rate" and "Current Probability"
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if not (header_cells and "Target Rate" in header_cells[0]):
            continue
        bucket_probs = []
        for r in rows[1:]:
            cells = [c.get_text(strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            # cells[0]: "3.25 - 3.50"  cells[1]: "6.4%"
            m1 = re.match(r"([\d.]+)\s*-\s*([\d.]+)", cells[0])
            if not m1:
                continue
            target_lower = float(m1.group(1)) / 100.0
            prob_txt = cells[1].rstrip("%").strip()
            if prob_txt in ("", "—", "-"):
                continue
            try:
                p = float(prob_txt) / 100.0
            except ValueError:
                continue
            bucket_probs.append((round(target_lower, 4), p))
        if bucket_probs:
            result.append(bucket_probs)
    return result


def detect_current_target(prob_tables: list[list[tuple[float, float]]]) -> tuple[float, float]:
    """Infer current Fed target band from the next-meeting table's modal bucket."""
    if not prob_tables:
        return 0.0350, 0.0375  # fallback
    # Top-prob bucket of the FIRST (soonest) meeting is most likely the current target
    top = max(prob_tables[0], key=lambda x: x[1])
    return top[0], round(top[0] + 0.0025, 4)


def contract_for(d: dt.date) -> str:
    return f"ZQ{MONTH_CODES[d.month]}{d.year % 100:02d}"


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parity_log (
            snapshot_date   TEXT NOT NULL,
            fomc_date       TEXT NOT NULL,
            contract        TEXT NOT NULL,
            zq_settle       REAL NOT NULL,
            target_lower    REAL NOT NULL,
            cme_prob        REAL NOT NULL,
            our_prob        REAL NOT NULL,
            abs_err_pp      REAL NOT NULL,
            is_next_meeting INTEGER NOT NULL,
            PRIMARY KEY (snapshot_date, fomc_date, target_lower)
        )
    """)
    conn.commit()
    return conn


def run_one_snapshot() -> list[ParityRow]:
    html = fetch_investing_fed()
    meetings = parse_meetings(html)
    prob_tables = parse_prob_tables(html)
    if not meetings or not prob_tables:
        _log.error("no meetings or prob tables parsed — site format may have changed")
        return []

    cur_lower, cur_upper = detect_current_target(prob_tables)
    today = dt.date.today()
    rows: list[ParityRow] = []
    n_pairs = min(len(meetings), len(prob_tables))
    next_meeting_date = meetings[0][0] if meetings else None

    for i in range(n_pairs):
        fomc_date, zq_price = meetings[i]
        bucket_probs = prob_tables[i]
        contract = contract_for(fomc_date)
        is_next = (fomc_date == next_meeting_date)

        # Build buckets list spanning what CME shows (auto-derive)
        decision_buckets = sorted({b for b, _ in bucket_probs})
        ctx = FOMCContext(
            current_target_lower=cur_lower,
            current_target_upper=cur_upper,
            fomc_date=fomc_date,
            contract_month_start=fomc_date.replace(day=1),
            contract_month_end=_month_end(fomc_date),
            decision_buckets=decision_buckets,
        )
        try:
            our_probs = decision_probs(zq_price, ctx)
        except Exception as e:
            _log.warning(f"decision_probs failed for {fomc_date}: {e}")
            continue

        for target_lower, cme_p in bucket_probs:
            our_p = our_probs.get(target_lower, 0.0)
            err_pp = abs(our_p - cme_p) * 100.0
            rows.append(ParityRow(
                snapshot_date=today,
                fomc_date=fomc_date,
                contract=contract,
                zq_settle=zq_price,
                target_lower=target_lower,
                cme_prob=cme_p,
                our_prob=our_p,
                abs_err_pp=err_pp,
                is_next_meeting=is_next,
            ))
    return rows


def _month_end(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    nxt = dt.date(d.year, d.month + 1, 1)
    return nxt - dt.timedelta(days=1)


def log_to_db(conn: sqlite3.Connection, rows: list[ParityRow]) -> int:
    n = 0
    for r in rows:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO parity_log
                  (snapshot_date, fomc_date, contract, zq_settle, target_lower,
                   cme_prob, our_prob, abs_err_pp, is_next_meeting, is_path_dependent)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                r.snapshot_date.isoformat(), r.fomc_date.isoformat(), r.contract,
                r.zq_settle, r.target_lower, r.cme_prob, r.our_prob,
                r.abs_err_pp, int(r.is_next_meeting), int(not r.is_next_meeting),
            ))
            n += 1
        except Exception as e:
            _log.warning(f"db insert failed: {e}")
    conn.commit()
    return n


def evaluate_gate(conn: sqlite3.Connection,
                  threshold_pp: float = 1.0,
                  next_only: bool = True,
                  min_n: int = 30) -> dict:
    where = "WHERE is_next_meeting = 1" if next_only else ""
    rows = conn.execute(f"""
        SELECT abs_err_pp FROM parity_log {where}
    """).fetchall()
    n = len(rows)
    if n == 0:
        return {"n": 0, "passes": False, "reason": "no_data"}
    within = sum(1 for (e,) in rows if e < threshold_pp)
    pct = 100.0 * within / n
    mae = sum(e for (e,) in rows) / n
    return {
        "n": n, "within_n": within, "pct_within": round(pct, 2),
        "mae_pp": round(mae, 3), "threshold_pp": threshold_pp,
        "min_n_required": min_n, "next_only": next_only,
        "passes": (n >= min_n and pct >= 90.0),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gate", action="store_true",
                    help="Evaluate accumulated parity gate after this snapshot.")
    ap.add_argument("--include-far", action="store_true",
                    help="Include non-next meetings in gate (biased — see docstring).")
    ap.add_argument("--no-log", action="store_true",
                    help="Skip DB log; print only.")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    rows = run_one_snapshot()
    if not rows:
        print("ERROR: no parity rows produced")
        return 2

    out = {
        "snapshot_date": rows[0].snapshot_date.isoformat(),
        "n_rows": len(rows),
        "n_meetings": len({r.fomc_date for r in rows}),
        "meetings": [
            {
                "fomc_date": d.isoformat(),
                "contract": next(r.contract for r in rows if r.fomc_date == d),
                "zq_settle": next(r.zq_settle for r in rows if r.fomc_date == d),
                "is_next": next(r.is_next_meeting for r in rows if r.fomc_date == d),
                "buckets": [
                    {"target_lower": r.target_lower, "cme_prob": round(r.cme_prob, 4),
                     "our_prob": round(r.our_prob, 4), "abs_err_pp": round(r.abs_err_pp, 3)}
                    for r in rows if r.fomc_date == d
                ],
                "max_err_pp": round(max(r.abs_err_pp for r in rows if r.fomc_date == d), 3),
            }
            for d in sorted({r.fomc_date for r in rows})
        ],
    }

    if not args.no_log:
        conn = init_db(args.db)
        n_logged = log_to_db(conn, rows)
        out["n_logged"] = n_logged
        if args.gate:
            out["gate"] = evaluate_gate(conn, next_only=not args.include_far)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"\n=== Forward Parity Check {out['snapshot_date']} ===")
        print(f"Rows: {out['n_rows']}  meetings: {out['n_meetings']}")
        print()
        for m in out["meetings"]:
            tag = " [NEXT]" if m["is_next"] else ""
            print(f"FOMC {m['fomc_date']} ({m['contract']}, ZQ={m['zq_settle']:.4f}){tag}")
            for b in m["buckets"]:
                marker = "✓" if b["abs_err_pp"] < 1.0 else "✗"
                print(f"  {b['target_lower']*100:.2f}% bucket: "
                      f"cme={b['cme_prob']*100:>5.1f}%  our={b['our_prob']*100:>5.1f}%  "
                      f"err={b['abs_err_pp']:>5.2f}pp  {marker}")
            print(f"  → max_err: {m['max_err_pp']:.2f}pp")
        if "gate" in out:
            g = out["gate"]
            print()
            label = "PASSES" if g["passes"] else "NOT YET MET"
            print(f"PARITY GATE ({'next-meeting only' if g.get('next_only') else 'all meetings'}): "
                  f"{label}")
            print(f"  n={g['n']}/{g['min_n_required']}  "
                  f"within {g['threshold_pp']}pp: {g['pct_within']:.1f}%  "
                  f"MAE={g.get('mae_pp', 'n/a')}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
