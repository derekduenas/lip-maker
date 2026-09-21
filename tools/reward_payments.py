"""Record RECONCILED Kalshi reward payments — the only reward figures this
system is allowed to treat as money (2026-09-21).

Everything else in the codebase produces *estimates*: our snapshot model's
view of what a LIP pool should have paid us. Those are useful for ranking and
useless as evidence. This tool is the single door through which a real payment
enters, and it is deliberately manual: a human (or an authenticated
reconciliation job) must point at an independent Kalshi record.

    # one payment
    python tools/reward_payments.py record \
        --ticker KXBRENTD-26JUN0117-T100 --amount 4.37 --source kalshi_statement

    # a batch from a statement export (CSV: ticker,amount_usd)
    python tools/reward_payments.py import --csv statement.csv --source kalshi_statement

    # what is actually on record
    python tools/reward_payments.py status

Recording a payment also calibrates: engine/calibration_ewma.update() accepts
provenance='paid' only, and this is what supplies it.

PRIVACY: statement files are account records. Keep them out of this
repository — pass a path outside the tree, or pipe on stdin.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine import reward_provenance as prov

_log = logging.getLogger(__name__)


def _calibrate(ticker: str, amount_usd: float, db_path: str) -> float | None:
    """Feed one reconciled payment into the per-series calibration."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT reward_per_day_usd FROM lip_programs WHERE market_ticker = ? "
                "ORDER BY start_date DESC LIMIT 1", (ticker,)).fetchone()
            prefix_row = conn.execute(
                "SELECT series_prefix FROM settlement_log WHERE ticker = ?",
                (ticker,)).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None
    pool_per_day = float(row[0]) if row and row[0] else 0.0
    prefix = prefix_row[0] if prefix_row and prefix_row[0] else ticker.split("-", 1)[0]
    if pool_per_day <= 0:
        return None
    from engine.calibration_ewma import update as cal_update
    return cal_update(key=prefix, predicted_usd=pool_per_day,
                      actual_usd=amount_usd, provenance="paid", db_path=db_path)


def record_one(ticker: str, amount_usd: float, source: str, db_path: str) -> dict:
    res = prov.record_payment(ticker, amount_usd, source=source, db_path=db_path)
    if not res["updated"]:
        _log.warning(f"{ticker}: no settlement_log row — payment not attached. "
                     f"Run the settlement reconciler first.")
        return res
    res["calibration"] = _calibrate(ticker, amount_usd, db_path)
    return res


def status(db_path: str) -> dict:
    prov.ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(CASE WHEN reward_provenance='paid'
                                        THEN rebate_paid_usd ELSE 0 END), 0),
                      COALESCE(SUM(COALESCE(rebate_estimated_usd, 0)), 0),
                      SUM(CASE WHEN reward_provenance='paid' THEN 1 ELSE 0 END)
                 FROM settlement_log""").fetchone()
    finally:
        conn.close()
    return {"settlement_rows": row[0] or 0, "paid_usd": round(float(row[1] or 0), 4),
            "estimated_usd": round(float(row[2] or 0), 4),
            "rows_with_payment": row[3] or 0}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=settings.DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record one reconciled payment")
    r.add_argument("--ticker", required=True)
    r.add_argument("--amount", type=float, required=True)
    r.add_argument("--source", required=True, choices=sorted(prov.PAID_SOURCES))

    i = sub.add_parser("import", help="import a statement CSV (ticker,amount_usd)")
    i.add_argument("--csv", required=True, help="path OUTSIDE this repository")
    i.add_argument("--source", required=True, choices=sorted(prov.PAID_SOURCES))

    sub.add_parser("status", help="show paid vs estimated totals")

    a = ap.parse_args()
    if a.cmd == "record":
        print(record_one(a.ticker, a.amount, a.source, a.db))
        return 0
    if a.cmd == "import":
        n = 0
        total = 0.0
        with open(a.csv, newline="") as fh:
            for row in csv.DictReader(fh):
                tkr = (row.get("ticker") or "").strip()
                amt = row.get("amount_usd") or row.get("amount")
                if not tkr or amt is None:
                    continue
                record_one(tkr, float(amt), a.source, a.db)
                n += 1
                total += float(amt)
        print(f"imported {n} payments totalling ${total:.2f}")
        return 0
    s = status(a.db)
    print(f"settlement rows:      {s['settlement_rows']}")
    print(f"rows with a payment:  {s['rows_with_payment']}")
    print(f"PAID (reconciled):    ${s['paid_usd']:.2f}")
    print(f"ESTIMATED (model):    ${s['estimated_usd']:.2f}   <- not money")
    if s["paid_usd"] <= 0:
        print("\nNo reconciled payments on record. Every reward number in this "
              "system is currently a model estimate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
