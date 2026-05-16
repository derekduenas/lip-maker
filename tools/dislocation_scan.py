"""CLI runner for the cross-venue dislocation harvester (Prong 3).

Usage:
    python -m tools.dislocation_scan                  # one-shot scan
    python -m tools.dislocation_scan --json           # JSON output (cron-friendly)
    python -m tools.dislocation_scan --domain macro_fed
    python -m tools.dislocation_scan --bankroll 5000

Default behavior:
    PAPER_MODE → log candidates, no execution.
    REVIEW_MODE → also write actionable candidates to data/dislocation.db
                  for manual approval.
    LIVE_MODE → would auto-execute (NOT YET IMPLEMENTED — refuse).

Cron deployment (5-min interval):
    */5 * * * * cd /home/user/lip-maker && python -m tools.dislocation_scan --json >> logs/dislocation.log 2>&1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Allow running as `python tools/dislocation_scan.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dislocation.config import (
    BANKROLL_USD,
    DB_PATH,
    LIVE_MODE,
    LOG_PATH,
    PAPER_MODE,
    REVIEW_MODE,
    TOP_N_CANDIDATES,
)
from dislocation.event_universe import Domain
from dislocation.scanners.base import Candidate
from dislocation.scanners.macro_fed import MacroFedScanner
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger("dislocation_scan")


SCANNER_REGISTRY = {
    Domain.MACRO_FED: MacroFedScanner,
    # Domain.WEATHER:    WeatherScanner,        # TODO Sprint 2
    # Domain.BIO_FDA:    BioFDAScanner,         # TODO Sprint 3
    # Domain.EARNINGS:   EarningsScanner,       # TODO (overlaps Prong 2)
    # Domain.ELECTION:   ElectionScanner,       # TODO Sprint 4
}


# ── DB ───────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dislocation_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    domain          TEXT    NOT NULL,
    pair_id         TEXT    NOT NULL,
    description     TEXT,
    p_a             REAL,
    p_b             REAL,
    raw_spread_pp   REAL,
    cost_pp         REAL,
    edge_pp         REAL,
    days_to_settle  REAL,
    position_usd    REAL,
    expected_pnl    REAL,
    net_pnl         REAL,
    direction       TEXT,
    sized_usd       REAL,
    rejected        INTEGER,
    actionable      INTEGER,
    explanation     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_pair_ts
    ON dislocation_candidates (pair_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_dc_actionable
    ON dislocation_candidates (actionable, timestamp);

CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL,
    enqueued_at     TEXT    NOT NULL,
    reviewed_at     TEXT,
    decision        TEXT,    -- 'approve' / 'reject' / NULL
    notes           TEXT,
    FOREIGN KEY (candidate_id) REFERENCES dislocation_candidates(id)
);
"""


def _ensure_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA_SQL)


def _persist_candidate(conn: sqlite3.Connection, c: Candidate) -> int:
    cur = conn.execute(
        """
        INSERT INTO dislocation_candidates (
            timestamp, domain, pair_id, description,
            p_a, p_b, raw_spread_pp, cost_pp, edge_pp,
            days_to_settle, position_usd, expected_pnl, net_pnl, direction,
            sized_usd, rejected, actionable, explanation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            c.timestamp.isoformat(),
            c.pair.domain.value,
            c.pair.pair_id,
            c.pair.description,
            c.spread.p_a, c.spread.p_b,
            c.spread.raw_spread_pp, c.spread.cost_pp, c.spread.edge_pp,
            c.spread.days_to_settle, c.spread.position_usd,
            c.spread.expected_pnl_usd, c.spread.net_pnl_usd, c.spread.direction,
            c.decision.final_usd, int(c.decision.rejected), int(c.actionable),
            json.dumps(c.explain()),
        ),
    )
    return cur.lastrowid


def _enqueue_for_review(conn: sqlite3.Connection, candidate_id: int) -> None:
    conn.execute(
        """
        INSERT INTO review_queue (candidate_id, enqueued_at)
        VALUES (?, ?)
        """,
        (candidate_id, dt.datetime.utcnow().isoformat()),
    )


# ── Scan ─────────────────────────────────────────────────────────────────
def run_scan(
    *,
    bankroll: float,
    domains: list[Domain],
    persist: bool = True,
) -> list[Candidate]:
    kalshi = KalshiClient()
    out: list[Candidate] = []
    for d in domains:
        cls = SCANNER_REGISTRY.get(d)
        if cls is None:
            _log.warning(f"no scanner registered for domain {d.value}")
            continue
        try:
            scanner = cls(kalshi, bankroll=bankroll)
        except TypeError:
            scanner = cls(bankroll=bankroll)
        out.extend(scanner.scan())

    out.sort(key=lambda c: -c.spread.edge_pp)

    if persist:
        _ensure_db()
        with sqlite3.connect(DB_PATH) as conn:
            for c in out:
                cid = _persist_candidate(conn, c)
                if c.actionable and REVIEW_MODE:
                    _enqueue_for_review(conn, cid)
            conn.commit()

    return out


# ── Output ───────────────────────────────────────────────────────────────
def _print_human(candidates: list[Candidate]) -> None:
    actionable = [c for c in candidates if c.actionable]
    print(f"\n=== Dislocation Scan @ {dt.datetime.utcnow().isoformat()} ===")
    print(f"Mode: PAPER={PAPER_MODE} REVIEW={REVIEW_MODE} LIVE={LIVE_MODE}")
    print(f"Total scanned: {len(candidates)}   Actionable: {len(actionable)}")
    print()
    if not actionable:
        print("(no actionable candidates above edge threshold)")
        return
    print(f"{'pair':<40} {'edge_pp':>8} {'days':>5} {'size$':>7} {'net$':>7} {'dir':<20}")
    print("-" * 90)
    for c in actionable[:TOP_N_CANDIDATES]:
        print(
            f"{c.pair.pair_id:<40} "
            f"{c.spread.edge_pp:>8.2f} "
            f"{c.spread.days_to_settle:>5.1f} "
            f"{c.decision.final_usd:>7.0f} "
            f"{c.spread.net_pnl_usd:>7.2f} "
            f"{c.spread.direction:<20}"
        )


def _print_json(candidates: list[Candidate]) -> None:
    print(json.dumps({
        "ts": dt.datetime.utcnow().isoformat(),
        "mode": {"paper": PAPER_MODE, "review": REVIEW_MODE, "live": LIVE_MODE},
        "total": len(candidates),
        "actionable": sum(1 for c in candidates if c.actionable),
        "candidates": [c.explain() for c in candidates[:TOP_N_CANDIDATES]],
    }, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=BANKROLL_USD)
    ap.add_argument("--domain", action="append", choices=[d.value for d in Domain],
                    help="Scan only specified domain(s). Default: all registered.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(LOG_PATH, mode="a"),
        ] if Path(LOG_PATH).parent.exists() else [logging.StreamHandler(sys.stderr)],
    )

    if LIVE_MODE:
        _log.error("LIVE_MODE is set but auto-execution is NOT IMPLEMENTED. Refusing.")
        return 2

    if args.domain:
        domains = [Domain(d) for d in args.domain]
    else:
        domains = list(SCANNER_REGISTRY.keys())

    candidates = run_scan(
        bankroll=args.bankroll,
        domains=domains,
        persist=not args.no_persist,
    )

    if args.json:
        _print_json(candidates)
    else:
        _print_human(candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
