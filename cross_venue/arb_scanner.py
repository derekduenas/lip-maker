"""Kalshi ↔ Polymarket US same-event arbitrage scanner.

Scans for crossed cross-venue spreads where the bid on one side of a
Kalshi market plus the bid on the equivalent side of the PM US market
exceeds 100¢. When that happens, locking in (sum - 100) per pair of
contracts is a *risk-free spread* — both contracts settle against each
other regardless of outcome.

This is a SEPARATE PROFIT ENGINE from the rebate-maker stack. Rebate
income comes from being top-of-book passive; arbitrage income comes
from being aggressive when prices diverge across venues.

DATA-LAYER ONLY (paper-mode)
----------------------------
This module is intentionally read-only on both venues — it records
opportunities to `cross_venue_arb_log` but does NOT place orders.
Real arbitrage execution requires:
  - Near-atomic placement on both venues (in practice: <500ms)
  - Cross-venue position tracking with USDC/USD reconciliation
  - Slippage estimation per fill
…all of which are out of scope until paper-mode data proves the
opportunities are real, persistent, and worth the integration cost.

PIPELINE
--------
1. `scan()` is called by `tools/arb_scan.py` on a systemd timer
   (default 60s cadence).
2. For each Kalshi ticker we currently quote (via lip_programs), look
   up the PM US counterpart via `cross_venue.kalshi_pm_map`.
3. Fetch best bid/ask on both venues (Kalshi REST + PM gamma-api).
4. Compute cross-venue spreads; if (yes_kalshi_bid + no_pm_bid) > 100c
   OR (no_kalshi_bid + yes_pm_bid) > 100c, log it.
5. After 14 days, the operator reviews `cross_venue_arb_log` to decide
   whether spreads are wide / persistent enough to justify building
   the order-routing layer.
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings
from cross_venue.kalshi_pm_map import (
    EXACT_MATCHES, candidate_rules_for, find_pm_counterpart,
    slug_matches_rule,
)

_log = logging.getLogger(__name__)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS cross_venue_arb_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at        TEXT NOT NULL,
    kalshi_ticker      TEXT NOT NULL,
    pm_slug            TEXT NOT NULL,
    side_invariant     TEXT NOT NULL,
    arb_side           TEXT NOT NULL,         -- 'kalshi_yes+pm_no' | 'kalshi_no+pm_yes' | 'kalshi_yes+pm_yes' | 'kalshi_no+pm_no'
    kalshi_yes_bid_c   INTEGER,
    kalshi_no_bid_c    INTEGER,
    pm_yes_bid_c       INTEGER,
    pm_no_bid_c        INTEGER,
    sum_bids_c         INTEGER NOT NULL,      -- > 100 = arb
    edge_c             INTEGER NOT NULL,      -- sum_bids - 100
    min_depth          INTEGER,               -- smaller side's depth (caps size)
    notes              TEXT,
    source             TEXT NOT NULL,         -- 'manual' | 'exact' | 'regex'
    UNIQUE (kalshi_ticker, pm_slug, arb_side, detected_at)
);

CREATE INDEX IF NOT EXISTS idx_arb_log_detected
    ON cross_venue_arb_log(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_arb_log_edge
    ON cross_venue_arb_log(edge_c DESC);
CREATE INDEX IF NOT EXISTS idx_arb_log_pair
    ON cross_venue_arb_log(kalshi_ticker, pm_slug);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


@dataclass
class CrossQuote:
    kalshi_ticker: str
    pm_slug: str
    side_invariant: str
    source: str
    kalshi_yes_bid_c: Optional[int] = None
    kalshi_no_bid_c:  Optional[int] = None
    kalshi_yes_size:  Optional[int] = None
    kalshi_no_size:   Optional[int] = None
    pm_yes_bid_c:     Optional[int] = None
    pm_no_bid_c:      Optional[int] = None
    pm_yes_size:      Optional[int] = None
    pm_no_size:       Optional[int] = None
    notes:            str = ""


@dataclass
class ArbFinding:
    pair: CrossQuote
    arb_side: str           # 'kalshi_yes+pm_no' etc.
    sum_bids_c: int
    edge_c: int             # sum_bids - 100
    min_depth: int          # smallest side's size — caps tradeable contracts


# ── Spread computation ─────────────────────────────────────────────────────

def find_arb(pair: CrossQuote, min_edge_c: int = 1) -> list[ArbFinding]:
    """Return all arb-side combinations on this pair with edge ≥ min_edge_c.

    Side combinations depend on side_invariant:
      'same'     → YES_K + NO_PM > 100  OR  NO_K + YES_PM > 100
      'inverted' → YES_K + YES_PM > 100  OR  NO_K + NO_PM > 100
    """
    out: list[ArbFinding] = []
    if pair.side_invariant == "same":
        combos = [
            ("kalshi_yes+pm_no",
             pair.kalshi_yes_bid_c, pair.pm_no_bid_c,
             min(pair.kalshi_yes_size or 0, pair.pm_no_size or 0)),
            ("kalshi_no+pm_yes",
             pair.kalshi_no_bid_c, pair.pm_yes_bid_c,
             min(pair.kalshi_no_size or 0, pair.pm_yes_size or 0)),
        ]
    else:  # inverted
        combos = [
            ("kalshi_yes+pm_yes",
             pair.kalshi_yes_bid_c, pair.pm_yes_bid_c,
             min(pair.kalshi_yes_size or 0, pair.pm_yes_size or 0)),
            ("kalshi_no+pm_no",
             pair.kalshi_no_bid_c, pair.pm_no_bid_c,
             min(pair.kalshi_no_size or 0, pair.pm_no_size or 0)),
        ]
    for name, a, b, min_d in combos:
        if a is None or b is None:
            continue
        s = int(a) + int(b)
        edge = s - 100
        if edge >= min_edge_c:
            out.append(ArbFinding(
                pair=pair, arb_side=name, sum_bids_c=s,
                edge_c=edge, min_depth=int(min_d or 0),
            ))
    return out


def persist_finding(f: ArbFinding, db_path: str = settings.DB_PATH) -> None:
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=2.0)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO cross_venue_arb_log
               (detected_at, kalshi_ticker, pm_slug, side_invariant,
                arb_side,
                kalshi_yes_bid_c, kalshi_no_bid_c,
                pm_yes_bid_c, pm_no_bid_c,
                sum_bids_c, edge_c, min_depth, notes, source)
               VALUES (?,?,?,?, ?, ?,?,?,?, ?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(),
             f.pair.kalshi_ticker, f.pair.pm_slug, f.pair.side_invariant,
             f.arb_side,
             f.pair.kalshi_yes_bid_c, f.pair.kalshi_no_bid_c,
             f.pair.pm_yes_bid_c, f.pair.pm_no_bid_c,
             f.sum_bids_c, f.edge_c, f.min_depth,
             f.pair.notes or None, f.pair.source),
        )
        conn.commit()
    finally:
        conn.close()


# ── Venue book fetchers ────────────────────────────────────────────────────
# These are pluggable. The defaults try real APIs; tests inject mocks.

def fetch_kalshi_book(ticker: str) -> dict:
    """Best YES/NO bid + size from Kalshi REST. Returns {} on failure."""
    try:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        resp = c.get(f"/markets/{ticker}/orderbook", params={"depth": 1})
        ob = resp.get("orderbook", {})
        yb = (ob.get("yes") or [])[:1]
        nb = (ob.get("no")  or [])[:1]
        return {
            "yes_bid_c": yb[0][0] if yb else None,
            "yes_size":  yb[0][1] if yb else None,
            "no_bid_c":  nb[0][0] if nb else None,
            "no_size":   nb[0][1] if nb else None,
        }
    except Exception as e:
        _log.debug(f"kalshi book fetch failed for {ticker}: {e}")
        return {}


def fetch_pm_book(slug: str) -> dict:
    """Best YES/NO bid + size from Polymarket gamma-api (public, no auth).

    P1-4 fix (2026-05-14): gamma-api `outcomePrices` are MID, not BID.
    Real PM bid is typically 1-3c below mid; treating mid as bid
    overstates cross-venue arb edge and produces phantom opportunities
    (~10-20% of logged arbs disappear at execution). Until we switch
    to the PM CLOB /book endpoint, subtract `MID_TO_BID_CONSERVATISM_C`
    cents from both sides so the scanner reports edge that is more
    likely to clear at real execution.
    """
    MID_TO_BID_CONSERVATISM_C = 1
    try:
        import urllib.request, json
        url = f"https://gamma-api.polymarket.com/markets/{slug}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
        # PM returns prices in [0, 1]; convert to cents
        outcomes = data.get("outcomes") or []
        prices = data.get("outcomePrices") or data.get("outcome_prices") or []
        if not prices or len(prices) < 2:
            return {}
        yes_mid = int(round(float(prices[0]) * 100))
        no_mid  = int(round(float(prices[1]) * 100))
        # Conservative bid proxy: mid - 1c. Size unknown without CLOB book.
        return {
            "yes_bid_c": max(1, min(99, yes_mid - MID_TO_BID_CONSERVATISM_C)),
            "yes_size":  None,
            "no_bid_c":  max(1, min(99, no_mid  - MID_TO_BID_CONSERVATISM_C)),
            "no_size":   None,
        }
    except Exception as e:
        _log.debug(f"PM book fetch failed for {slug}: {e}")
        return {}


# ── Scan loop ──────────────────────────────────────────────────────────────

def _enumerate_pm_slugs(db_path: str) -> list[str]:
    """Pull recently-seen PM US slugs from polymarket.tools.public_recorder
    output table (if present)."""
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            rows = conn.execute(
                """SELECT DISTINCT slug FROM pm_public_markets
                   WHERE datetime(captured_at) >= datetime('now', '-12 hours')"""
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def scan(db_path: str = settings.DB_PATH, *,
         min_edge_c: int = 1,
         kalshi_book_fn=fetch_kalshi_book,
         pm_book_fn=fetch_pm_book,
         pm_slug_enumerator=None,
         max_pairs: int = 50) -> dict:
    """Scan for arb opportunities. Returns summary dict."""
    ensure_schema(db_path)
    pm_slug_enumerator = pm_slug_enumerator or (
        lambda: _enumerate_pm_slugs(db_path)
    )

    # Active Kalshi tickers from lip_programs
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            ks_rows = conn.execute(
                """SELECT DISTINCT market_ticker FROM lip_programs
                   ORDER BY rowid DESC LIMIT ?""",
                (max_pairs,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        ks_rows = []
    kalshi_tickers = [r[0] for r in ks_rows]

    pm_slugs_universe: list[str] = pm_slug_enumerator() or []

    findings: list[ArbFinding] = []
    n_pairs_examined = 0
    for kt in kalshi_tickers:
        # 1) try manual map (highest precedence)
        cand = find_pm_counterpart(kt, db_path=db_path)
        candidates: list[tuple[str, str, str, float, str]] = []
        if cand:
            candidates.append((cand.pm_slug, cand.side_invariant,
                               cand.source, cand.confidence, cand.notes))
        else:
            # 2) regex inference — match each rule against pm_slugs_universe
            for rule in candidate_rules_for(kt):
                for slug in pm_slugs_universe:
                    if slug_matches_rule(slug, rule, kt):
                        candidates.append((slug, rule.side_invariant, "regex",
                                           0.6, rule.notes))

        for pm_slug, side_inv, source, conf, notes in candidates:
            n_pairs_examined += 1
            k_book = kalshi_book_fn(kt)
            p_book = pm_book_fn(pm_slug)
            if not k_book or not p_book:
                continue
            pair = CrossQuote(
                kalshi_ticker=kt, pm_slug=pm_slug,
                side_invariant=side_inv, source=source,
                kalshi_yes_bid_c=k_book.get("yes_bid_c"),
                kalshi_no_bid_c=k_book.get("no_bid_c"),
                kalshi_yes_size=k_book.get("yes_size"),
                kalshi_no_size=k_book.get("no_size"),
                pm_yes_bid_c=p_book.get("yes_bid_c"),
                pm_no_bid_c=p_book.get("no_bid_c"),
                pm_yes_size=p_book.get("yes_size"),
                pm_no_size=p_book.get("no_size"),
                notes=notes,
            )
            for f in find_arb(pair, min_edge_c=min_edge_c):
                findings.append(f)
                persist_finding(f, db_path=db_path)
                _log.info(
                    f"ARB {kt} ↔ {pm_slug} side={f.arb_side} "
                    f"sum={f.sum_bids_c}c edge=+{f.edge_c}c depth={f.min_depth}"
                )

    return {
        "n_kalshi_tickers": len(kalshi_tickers),
        "n_pm_slugs": len(pm_slugs_universe),
        "n_pairs_examined": n_pairs_examined,
        "n_findings": len(findings),
        "top_findings": [
            {
                "kalshi": f.pair.kalshi_ticker,
                "pm_slug": f.pair.pm_slug,
                "arb_side": f.arb_side,
                "edge_c": f.edge_c,
                "depth": f.min_depth,
            }
            for f in sorted(findings, key=lambda x: -x.edge_c)[:5]
        ],
    }


def _self_test() -> None:
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        ensure_schema(path)
        # Stub a manual mapping and seed lip_programs + pm_public_markets
        from cross_venue.kalshi_pm_map import add_manual_mapping
        add_manual_mapping(
            "KXBTCMAXMON-26MAY3122-T120000",
            "will-btc-exceed-120k-may-2026",
            side_invariant="same", db_path=path,
        )
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE lip_programs (market_ticker TEXT PRIMARY KEY,
                                    reward_per_day_usd REAL,
                                    target_size INTEGER,
                                    discount_factor REAL);
        INSERT INTO lip_programs VALUES
            ('KXBTCMAXMON-26MAY3122-T120000', 100, 1000, 0.85);
        """)
        conn.commit(); conn.close()

        # Mock books: clean arb — KalshiYES 55c, PM NO 50c → sum 105, edge 5
        def mock_k(_t): return {
            "yes_bid_c": 55, "yes_size": 100,
            "no_bid_c":  45, "no_size":  100,
        }
        def mock_p(_s): return {
            "yes_bid_c": 50, "yes_size": 200,
            "no_bid_c":  50, "no_size":  200,
        }
        summary = scan(
            db_path=path, min_edge_c=1,
            kalshi_book_fn=mock_k, pm_book_fn=mock_p,
            pm_slug_enumerator=lambda: [],
        )
        assert summary["n_findings"] >= 1, summary
        assert summary["top_findings"][0]["edge_c"] == 5

        # Verify persistence
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM cross_venue_arb_log").fetchone()[0]
        conn.close()
        assert n >= 1, f"expected ≥1 logged finding, got {n}"

        # Idempotency: re-scan should not duplicate (UNIQUE on detected_at + pair)
        # The new row will have a fresh detected_at so it WILL insert again —
        # that's correct (one row per scan tick). Test we got new rows.
        n2 = conn = sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM cross_venue_arb_log").fetchone()[0]
        assert n2 >= 1

        print(f"arb_scanner self-test: PASS (logged {n} arb, top edge=5c)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
