"""Toxicity Filter — adverse-selection detector per market.

DIAGNOSIS (2026-04-22): Fill-ledger analysis showed 48% of our markets (15/31)
had 100% one-sided fill patterns. That's the fingerprint of adverse selection:
informed flow hits ONE side of our quote, pockets the spread, leaves us holding
naked directional risk that settles against us.

This module detects the pattern and recommends blacklist entries.

V1 (current): LOG-ONLY. Writes to toxicity_log table. Operator reviews before
enabling auto-action. Consistent with the `signal ≠ action` pattern from
competitor-intel: detection first, actuator second, with multi-signal gate.

V2 (after 3 days of log data): auto-blacklist with guards.

Guards to prevent false positives:
  - Min 10 fills in window (thin data is not a signal)
  - Lookback window: 12h (capture cycle of informed flow over a session)
  - Exclude just-settled or about-to-settle markets (natural end-of-life
    tends to be one-sided)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# ── V2 Tunables (2026-04-22 Toxicity Hunter audit) ──
# Two-tier gate: small-fill markets need EXTREME imbalance (effectively 100%);
# larger-fill markets accept lower threshold. 100% at 6 fills is unambiguous
# informed flow, no need to wait 12.
MIN_FILLS           = 6       # 2026-04-22 reduced 12→6 — 100% imbalance at 6 = signal
TIGHT_IMBAL_AT_MIN  = 0.95    # for 6-11 fill markets (tighter — small-sample protection)
IMBAL_THRESHOLD     = 0.85    # for 12+ fill markets (existing threshold)
LOOKBACK_HOURS      = 24      # 2026-04-22 extended 12h→24h — toxic patterns persistent
ACTION_MODE         = "blacklist"  # V2 LIVE: auto-blacklist toxic markets
BLACKLIST_HOURS     = 12      # shorter than V1 plan's 24h; limits false-positive damage
SKIP_NEAR_SETTLE_MIN = 120    # skip blacklist if market settles in < 2h
USE_FUTURES_GUARD   = True    # skip if dominant side = futures-indicated winning side


SCHEMA = """
CREATE TABLE IF NOT EXISTS toxicity_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT NOT NULL,
    detected_at        TEXT NOT NULL,
    yes_fills          INTEGER NOT NULL,
    no_fills           INTEGER NOT NULL,
    total_fills        INTEGER NOT NULL,
    imbalance_pct      REAL NOT NULL,
    dominant_side      TEXT NOT NULL,   -- "yes" or "no"
    lookback_hours     INTEGER NOT NULL,
    action_taken       TEXT NOT NULL,   -- "logged_only" | "blacklisted"
    UNIQUE(ticker, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_toxicity_ticker ON toxicity_log(ticker);
CREATE INDEX IF NOT EXISTS idx_toxicity_time   ON toxicity_log(detected_at);
"""


def _on_winning_side(ticker: str, dominant: str, conn: sqlite3.Connection) -> tuple[bool, str]:
    """Cross-reference toxicity dominant-side vs futures-indicated winning side.

    Returns (is_winning, reason):
      - (True, "...") if dominant side matches futures-implied settle direction
      - (False, "...") if dominant side is on losing direction
      - (False, "no_futures_data") if we can't evaluate (be cautious, treat as loser)
    """
    import re
    try:
        from engine.futures_feed import FUTURES_MAP
    except Exception:
        return False, "futures_feed_unavailable"

    prefix = next((p for p in FUTURES_MAP if ticker.startswith(p)), None)
    if not prefix:
        return False, "no_futures_mapping"

    m = re.search(r"-T([\d.]+)$", ticker)
    if not m:
        return False, "no_strike"
    strike = float(m.group(1))

    fair_row = conn.execute(
        """SELECT price FROM futures_prices
           WHERE kalshi_prefix = ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (prefix,),
    ).fetchone()
    if not fair_row or fair_row[0] is None:
        return False, "no_futures_price"
    fair = fair_row[0]

    # Futures-implied settle direction
    expected = "yes" if fair > strike else "no"
    is_winning = (dominant == expected)
    return is_winning, (f"futures={fair:.2f} strike={strike} → expects {expected}, "
                        f"dominant={dominant} → {'WIN' if is_winning else 'LOSE'}")


def _near_settle(ticker: str) -> bool:
    """Parse close-time from Kalshi ticker and check if we're within SKIP_NEAR_SETTLE_MIN.

    Daily commodity ticker format: PREFIX-YYMMMDDHH-T{strike}
    Where HH=17 means 17:00 ET = 21:00 UTC (commodity daily close).
    """
    import re
    m = re.match(r"^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T", ticker)
    if not m:
        return False  # can't parse — treat as safe-to-blacklist
    yy, mmm, dd, hh = m.groups()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    month = months.get(mmm.upper())
    if not month:
        return False
    try:
        # ET hour in ticker; convert to UTC (EDT = UTC-4, EST = UTC-5)
        # For Apr-Oct use EDT (-4). Conservative: always use EDT in 2026 data.
        close_utc = datetime(2000 + int(yy), month, int(dd), int(hh) + 4,
                             tzinfo=timezone.utc)
        minutes_until_settle = (close_utc - datetime.now(timezone.utc)).total_seconds() / 60
        return 0 < minutes_until_settle < SKIP_NEAR_SETTLE_MIN
    except Exception:
        return False


def scan(db_path: str = settings.DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=LOOKBACK_HOURS)).isoformat()

    rows = conn.execute(
        """SELECT ticker,
                  SUM(CASE WHEN side='yes' THEN count ELSE 0 END) AS yes_c,
                  SUM(CASE WHEN side='no'  THEN count ELSE 0 END) AS no_c,
                  COUNT(*) AS fill_events
           FROM fill_ledger
           WHERE created_at > ?
           GROUP BY ticker
           HAVING yes_c + no_c >= ?""",
        (cutoff, MIN_FILLS),
    ).fetchall()

    findings = []
    for tkr, ync, nnc, ne in rows:
        ync = int(ync or 0)
        nnc = int(nnc or 0)
        total = ync + nnc
        imbal = abs(ync - nnc) / total if total > 0 else 0
        # Two-tier gate (Toxicity Hunter audit): small-fill markets (6-11)
        # need extreme imbalance; 12+ fill markets accept the standard threshold.
        # 100% imbalance at fill 6 is unambiguous informed flow.
        threshold = TIGHT_IMBAL_AT_MIN if total < 12 else IMBAL_THRESHOLD
        if imbal < threshold:
            continue

        dominant = "yes" if ync > nnc else "no"
        # Check if already ACTED ON recently (dedup within 4h).
        # 2026-04-22 FIX: exclude log-only entries. V1 logs from before V2 went
        # live were dedup'ing V2's evaluation → markets like WHEAT-T604.99 with
        # 20 NO/0 YES fills never got auto-blacklisted. Now we only dedup
        # against entries that took a REAL action (blacklisted).
        recent = conn.execute(
            """SELECT 1 FROM toxicity_log
               WHERE ticker = ?
                 AND datetime(detected_at) > datetime('now', '-4 hour')
                 AND action_taken = 'blacklisted'""",
            (tkr,),
        ).fetchone()
        if recent:
            continue

        action = "logged_only"
        skip_reason = None

        if ACTION_MODE == "blacklist":
            # Guard 1: skip if market settles within SKIP_NEAR_SETTLE_MIN
            if _near_settle(tkr):
                skip_reason = "near_settle_window"
            # Guard 2: skip if dominant side is futures-indicated WINNING side
            elif USE_FUTURES_GUARD:
                is_winning, why = _on_winning_side(tkr, dominant, conn)
                if is_winning:
                    skip_reason = f"on_winning_side ({why})"
            # Already blacklisted? skip
            if not skip_reason:
                existing = conn.execute(
                    "SELECT 1 FROM market_blacklist WHERE ticker = ?",
                    (tkr,),
                ).fetchone()
                if existing:
                    skip_reason = "already_blacklisted"

            if not skip_reason:
                # When GRADUATED_THROTTLE is on, skip the binary blacklist
                # write — graduated companion below handles all severity
                # tiers (0.95+ imbalance writes size_scale=0.05, effectively
                # a near-full block). When off, retain legacy binary behavior.
                if not getattr(settings, "GRADUATED_THROTTLE", False):
                    conn.execute(
                        """INSERT INTO market_blacklist
                           (ticker, expires_at, reason, added_at)
                           VALUES (?, ?, ?, ?)""",
                        (tkr,
                         (now + timedelta(hours=BLACKLIST_HOURS)).isoformat(),
                         f"toxicity:{dominant}_side_imbal_{int(imbal*100)}pct",
                         now.isoformat()),
                    )
                    action = "blacklisted"
                else:
                    action = "graduated_throttle"

                # A.3 graduated companion: write a market_throttle row at the
                # same time. When GRADUATED_THROTTLE is on, quote_manager
                # applies the (size_scale, tick_offset) instead of dropping
                # the whole quote. Tiers reflect imbalance severity:
                #   imbal 0.85–0.90 → size_scale 0.40, tick 1
                #   imbal 0.90–0.95 → size_scale 0.20, tick 2
                #   imbal 0.95+    → size_scale 0.05, tick 2  (near-full block)
                try:
                    from monitor.market_throttle import upsert as _mt_upsert
                    if imbal >= 0.95:
                        ss, toff = 0.05, 2
                    elif imbal >= 0.90:
                        ss, toff = 0.20, 2
                    else:
                        ss, toff = 0.40, 1
                    _mt_upsert(
                        tkr, size_scale=ss, tick_offset=toff,
                        source="toxicity",
                        ttl_sec=BLACKLIST_HOURS * 3600,
                        detail=f"imbal={int(imbal*100)}% dominant={dominant} "
                               f"y={ync} n={nnc}",
                        db_path=db_path,
                    )
                except Exception as _e:
                    _log.debug(f"toxicity_filter graduated upsert failed for {tkr}: {_e}")
            else:
                action = f"skipped:{skip_reason}"

        conn.execute(
            """INSERT OR IGNORE INTO toxicity_log
               (ticker, detected_at, yes_fills, no_fills, total_fills,
                imbalance_pct, dominant_side, lookback_hours, action_taken)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tkr, now.isoformat(), ync, nnc, total,
             round(imbal, 3), dominant, LOOKBACK_HOURS, action),
        )
        findings.append({
            "ticker": tkr, "yes": ync, "no": nnc,
            "imbalance": round(imbal*100, 0), "dominant": dominant,
            "action": action,
        })

    conn.commit()

    # ── Series-level cascade detection (Toxicity Hunter audit) ──
    # If a series has ≥2 negative-PnL settlements in past 48h, blacklist
    # ALL currently-enrolled markets in that series for 24h. Stops cases
    # like KXSILVERD where each strike was evaluated independently and
    # the series bled $12.91 across 50 markets before any single market
    # got blacklisted on its own merits.
    series_findings = []
    if ACTION_MODE == "blacklist":
        cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        loser_series = conn.execute(
            """SELECT series_prefix,
                      COUNT(*) AS n_losers,
                      ROUND(SUM(our_realized_usd), 2) AS net_loss
               FROM settlement_log
               WHERE datetime(close_time) > datetime(?)
                 AND our_realized_usd < 0
               GROUP BY series_prefix
               HAVING n_losers >= 2""",
            (cutoff_48h,),
        ).fetchall()
        expires_iso = (now + timedelta(hours=BLACKLIST_HOURS)).isoformat()
        for series, n_losers, net_loss in loser_series:
            cur = conn.execute(
                """INSERT OR IGNORE INTO market_blacklist
                   (ticker, expires_at, reason, added_at)
                   SELECT market_ticker, ?, ?, ?
                   FROM lip_programs
                   WHERE series_ticker = ? AND enrolled = 1
                     AND market_ticker NOT IN (
                         SELECT ticker FROM market_blacklist
                         WHERE datetime(expires_at) > datetime('now')
                     )""",
                (expires_iso,
                 f"toxicity:series_cascade_{n_losers}losers_${net_loss}",
                 now.isoformat(),
                 series),
            )
            new_blocked = cur.rowcount
            if new_blocked > 0:
                series_findings.append({
                    "series": series, "n_losers": n_losers,
                    "net_loss": net_loss, "new_blocked": new_blocked,
                })
                _log.warning(f"series_cascade {series}: {n_losers} losers "
                             f"net=${net_loss}, blacklisted {new_blocked} markets")
        conn.commit()

    # Summary stats
    tox_24h = conn.execute(
        """SELECT COUNT(DISTINCT ticker) FROM toxicity_log
           WHERE datetime(detected_at) > datetime('now', '-24 hour')"""
    ).fetchone()[0]

    conn.close()
    return {
        "new_findings":     len(findings),
        "findings":         findings,
        "series_findings":  series_findings,
        "action_mode":      ACTION_MODE,
        "toxic_markets_24h": tox_24h,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    a = p.parse_args()
    r = scan()
    print(f"\n══ TOXICITY SCAN ══")
    mode_note = "AUTO-BLACKLIST with guards (V2 live since 2026-04-22)" if r['action_mode'] == "blacklist" else "log-only"
    print(f"  mode: {r['action_mode']}  ({mode_note})")
    print(f"  new findings this cycle: {r['new_findings']}")
    print(f"  distinct toxic markets last 24h: {r['toxic_markets_24h']}")
    if r["findings"]:
        print(f"\n  🔴 Markets flagged this cycle:")
        for f in r["findings"]:
            print(f"    {f['ticker'][:38]:<40s}  {f['dominant'].upper()}:{f['yes'] if f['dominant']=='yes' else f['no']}/{f['no'] if f['dominant']=='yes' else f['yes']}  "
                  f"imbal={int(f['imbalance']):>3}%  → {f['action']}")
    if r.get("series_findings"):
        print(f"\n  🔴 SERIES CASCADE blacklists this cycle:")
        for s in r["series_findings"]:
            print(f"    {s['series']}: {s['n_losers']} losers, net=${s['net_loss']}, "
                  f"blacklisted {s['new_blocked']} markets")


if __name__ == "__main__":
    main()
