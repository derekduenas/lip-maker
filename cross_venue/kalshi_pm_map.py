"""Kalshi ↔ Polymarket US event mapping.

When the same event trades on both venues — BTC monthly strikes, NBA
game outcomes, midterms, CPI — the two books quote independently
and prices diverge regularly. If (Kalshi YES bid + PM NO bid) > 100c,
that's a free arbitrage: lock in (bid_yes + bid_no − 100) per pair
of contracts.

NO PUBLIC API EXPOSES SAME-EVENT PAIRING. Kalshi tickers like
"KXBTCMINMON-26MAY-T100000" don't share any identifier with PM US
slugs like "will-btc-be-above-100k-may-2026". So we resolve by:

  1. Hand-curated `EXACT_MATCHES` for known recurring series
  2. Pattern matchers per series-prefix that infer the PM slug
  3. A `manual_map` SQLite table the operator can populate live

The map is purely data — no I/O on import. Consumed by
`cross_venue.arb_scanner` (`tools/arb_scan.py`).

USAGE
-----
    from cross_venue.kalshi_pm_map import find_pm_counterpart
    pair = find_pm_counterpart("KXBTCMINMON-26MAY3122-T100000")
    if pair:
        kalshi_ticker, pm_slug, side_invariant = pair
"""
from __future__ import annotations

import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


# ── Hand-curated mappings ──────────────────────────────────────────────────
# Each entry pairs a Kalshi ticker pattern with a PM slug pattern. The
# `side_invariant` describes how to translate sides:
#   "same"        → Kalshi YES == PM YES (same direction outcome)
#   "inverted"    → Kalshi YES == PM NO  (e.g. Kalshi "will X happen", PM "will not-X")
# An arb opportunity exists when:
#   side_invariant=same:    KalshiYESBid + PM_NOBid > 100c   (sell both YES)
#                       OR  KalshiNOBid  + PM_YESBid > 100c  (sell both NO)
#   side_invariant=inverted: KalshiYESBid + PM_YESBid > 100c
#                       OR  KalshiNOBid  + PM_NOBid > 100c

@dataclass(frozen=True)
class MappingRule:
    name: str                          # human label
    kalshi_pattern: str                # regex on Kalshi ticker
    pm_slug_pattern: str               # regex on PM slug
    side_invariant: str = "same"       # "same" | "inverted"
    notes: str = ""


EXACT_MATCHES: list[MappingRule] = [
    # BTC monthly strike markets — both venues run these regularly
    MappingRule(
        name="BTC monthly above strike",
        kalshi_pattern=r"^KXBTCMAXMON-.*-T(?P<strike>\d+)$",
        pm_slug_pattern=r"will-btc-(?:price-be|exceed|hit).*?(?P<strike>\d{4,6})",
        side_invariant="same",
        notes="Kalshi BTC MAX (will BTC reach strike) ↔ PM 'will BTC exceed X'",
    ),
    MappingRule(
        name="BTC monthly below strike",
        kalshi_pattern=r"^KXBTCMINMON-.*-T(?P<strike>\d+)$",
        pm_slug_pattern=r"will-btc-(?:fall-below|stay-below|drop-below).*?(?P<strike>\d{4,6})",
        side_invariant="same",
    ),
    # ETH monthly
    MappingRule(
        name="ETH monthly above strike",
        kalshi_pattern=r"^KXETHMAXMON-.*-T(?P<strike>\d+)$",
        pm_slug_pattern=r"will-eth-(?:price-be|exceed|hit).*?(?P<strike>\d{3,5})",
        side_invariant="same",
    ),
    # Fed rate decisions — Kalshi KXFED* ↔ PM rate cut / hike slugs
    MappingRule(
        name="Fed rate decision (cut)",
        kalshi_pattern=r"^KXFED(?:RATE)?-.*?(?P<month>FOMC|MEET)",
        pm_slug_pattern=r"will-fed-(?:cut|hike|hold)-rates.*?(?P<month>\d{4}-\d{2}|fomc)",
        side_invariant="same",
        notes="multi-outcome on both sides; per-strike pairing usually 1:1 by basis-points label",
    ),
    # CPI YoY prints
    MappingRule(
        name="CPI YoY threshold",
        kalshi_pattern=r"^KXCPIYOY-.*-T(?P<level>[\d.]+)$",
        pm_slug_pattern=r"will-cpi-yoy-be-(?:above|below).*?(?P<level>[\d.]+)",
        side_invariant="same",
    ),
    # 2026 Midterms — control of House / Senate
    MappingRule(
        name="2026 House majority",
        kalshi_pattern=r"^KXMIDTERM.*HOUSE",
        pm_slug_pattern=r"2026-midterm.*house",
        side_invariant="same",
    ),
    MappingRule(
        name="2026 Senate majority",
        kalshi_pattern=r"^KXMIDTERM.*SENATE",
        pm_slug_pattern=r"2026-midterm.*senate",
        side_invariant="same",
    ),
]


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS kalshi_pm_manual_map (
    kalshi_ticker      TEXT PRIMARY KEY,
    pm_slug            TEXT NOT NULL,
    side_invariant     TEXT NOT NULL,         -- 'same' | 'inverted'
    notes              TEXT,
    confidence         REAL DEFAULT 1.0,      -- 0-1, operator-set
    added_at           TEXT NOT NULL,
    last_verified_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_kalshi_pm_map_slug
    ON kalshi_pm_manual_map(pm_slug);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def add_manual_mapping(
    kalshi_ticker: str, pm_slug: str, *,
    side_invariant: str = "same", confidence: float = 1.0,
    notes: str = "", db_path: str = settings.DB_PATH,
) -> None:
    """Operator-driven mapping (highest precedence over regex inference)."""
    if side_invariant not in ("same", "inverted"):
        raise ValueError(f"side_invariant must be 'same' or 'inverted'")
    ensure_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=2.0)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO kalshi_pm_manual_map
               (kalshi_ticker, pm_slug, side_invariant, notes,
                confidence, added_at, last_verified_at)
               VALUES (?,?,?,?,?,?,?)""",
            (kalshi_ticker, pm_slug, side_invariant, notes or None,
             float(confidence), now, now),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class PmCandidate:
    kalshi_ticker: str
    pm_slug: str
    side_invariant: str       # 'same' | 'inverted'
    source: str               # 'manual' | 'exact' | 'regex'
    confidence: float
    notes: str = ""


def find_pm_counterpart(kalshi_ticker: str,
                        db_path: str = settings.DB_PATH
                        ) -> Optional[PmCandidate]:
    """Resolve Kalshi → PM US counterpart.

    Resolution order:
      1. kalshi_pm_manual_map (operator override, highest confidence)
      2. EXACT_MATCHES regex rules (return first hit)
      3. None — no counterpart known

    NOTE: returns None for slug-pattern rules until a PM-side market
    list is provided to the caller; rule-based resolution requires
    enumeration of PM slugs (cf. arb_scanner.py which fetches the list).
    """
    # 1) manual map
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                """SELECT pm_slug, side_invariant, notes, confidence
                   FROM kalshi_pm_manual_map
                   WHERE kalshi_ticker = ?""",
                (kalshi_ticker,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return PmCandidate(
                kalshi_ticker=kalshi_ticker, pm_slug=row[0],
                side_invariant=row[1], source="manual",
                confidence=float(row[3] or 1.0),
                notes=row[2] or "",
            )
    except sqlite3.OperationalError:
        pass

    # 2) exact rule match (returns the rule, NOT a resolved slug — the
    # caller (arb_scanner) is expected to enumerate PM slugs and match
    # the regex against them).
    return None


def candidate_rules_for(kalshi_ticker: str) -> list[MappingRule]:
    """Return all EXACT_MATCHES rules whose kalshi_pattern matches."""
    return [r for r in EXACT_MATCHES if re.search(r.kalshi_pattern, kalshi_ticker)]


def slug_matches_rule(pm_slug: str, rule: MappingRule,
                      kalshi_ticker: str) -> bool:
    """Check whether `pm_slug` satisfies the rule, optionally cross-
    referencing strike/level captured groups from kalshi_ticker."""
    m_pm = re.search(rule.pm_slug_pattern, pm_slug, re.IGNORECASE)
    if not m_pm:
        return False
    m_k = re.search(rule.kalshi_pattern, kalshi_ticker)
    if not m_k:
        return True   # rule matched PM by name, kalshi by prefix only
    # Compare named groups present in both
    k_groups = m_k.groupdict()
    pm_groups = m_pm.groupdict()
    for k, kv in k_groups.items():
        if k in pm_groups and pm_groups[k] is not None:
            if str(kv).lower() != str(pm_groups[k]).lower():
                return False
    return True


def _self_test() -> None:
    # Rule lookup
    rules = candidate_rules_for("KXBTCMINMON-26MAY3122-T100000")
    assert any(r.name.startswith("BTC monthly below") for r in rules), rules

    # Slug match (positive) — strike groups align
    rule = [r for r in EXACT_MATCHES if r.name.startswith("BTC monthly below")][0]
    assert slug_matches_rule("will-btc-fall-below-100000-may-2026", rule,
                             "KXBTCMINMON-26MAY3122-T100000")

    # Slug match (strike mismatch — should fail)
    assert not slug_matches_rule("will-btc-fall-below-90000-may-2026", rule,
                                  "KXBTCMINMON-26MAY3122-T100000")

    # Manual map round-trip
    import os, tempfile
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        ensure_schema(p)
        add_manual_mapping(
            "KXBTCMAXMON-26MAY3122-T120000",
            "will-btc-exceed-120k-may-2026",
            side_invariant="same", confidence=0.95,
            notes="operator-verified 2026-05-14", db_path=p,
        )
        cand = find_pm_counterpart("KXBTCMAXMON-26MAY3122-T120000", db_path=p)
        assert cand is not None and cand.source == "manual"
        assert cand.pm_slug == "will-btc-exceed-120k-may-2026"
        assert cand.side_invariant == "same"
        print(f"kalshi_pm_map self-test: PASS "
              f"({len(EXACT_MATCHES)} exact rules; manual round-trip OK)")
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
