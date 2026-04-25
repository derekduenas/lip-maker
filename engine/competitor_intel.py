"""Competitor-density intelligence layer.

This is the DATA MOAT. Every LIP participant can compute their own rebate.
Only we have second-by-second `total_score` tracking across the full market
universe — which means only we can detect:

  - WITHDRAWAL events: total_score drops ≥30% within 5 min on a market where
    competitors were active. Smart money exiting before news — we flat too.
  - ENTRY events: total_score rises ≥50% within 5 min. Competitor entering —
    either they know something (flat), or they're farming blindly (hold).
  - CONSOLIDATION: total_score stable + high for 30+ min. Market is locked;
    our share is capped; consider moving capital elsewhere.

This runs as a periodic analyzer (cron/timer), NOT inline with the quote loop.
Quote loop reads from competitor_events table to decide withdrawals.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

DB_PATH = "/root/lip-maker/data/lip_maker.db"

# ── Tunables (tuned 2026-04-21 after first scan showed 2700 events/hr) ─
WITHDRAWAL_PCT     = 0.50   # total_score must drop ≥50%
ENTRY_PCT          = 1.00   # total_score must DOUBLE
DETECTION_WINDOW_MIN = 5    # within this window
MIN_BASELINE_SCORE = 50.0   # must have had meaningful competition first
MIN_ABS_CHANGE     = 25.0   # total_score delta must be ≥25 in absolute terms
MIN_SNAPSHOTS_BASELINE = 8  # baseline window needs this many points
COOLDOWN_MIN       = 3      # don't re-flag same market + type within this


def ensure_schema(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS competitor_events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker    TEXT NOT NULL,
            detected_at      TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            prev_score       REAL,
            curr_score       REAL,
            pct_change       REAL,
            our_share_before REAL,
            our_share_after  REAL,
            window_min       INTEGER,
            notes            TEXT,
            acted_on         INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_competitor_events_mkt_time
            ON competitor_events(market_ticker, detected_at);
        CREATE INDEX IF NOT EXISTS idx_competitor_events_type_time
            ON competitor_events(event_type, detected_at);
        """)
        conn.commit()
    finally:
        conn.close()


def analyze_market(ticker: str, lookback_min: int = 60,
                   db_path: str = DB_PATH) -> list[dict]:
    """Scan one market's recent snapshots for regime changes.

    Walks the snapshot timeseries, comparing each point to the rolling
    baseline (5 min prior). Flags withdrawals and entries.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT captured_at, total_score, our_score, snapshot_valid
               FROM lip_snapshots
               WHERE market_ticker = ?
                 AND datetime(captured_at) > datetime('now', ?)
               ORDER BY captured_at""",
            (ticker, f"-{lookback_min} minute"),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 10:
        return []

    events: list[dict] = []
    times = [datetime.fromisoformat(r[0]) for r in rows]
    scores = [r[1] or 0 for r in rows]
    our = [r[2] or 0 for r in rows]
    last_emit: dict[str, datetime] = {}   # cooldown tracker

    for i in range(5, len(rows)):
        t_now = times[i]
        # Find all samples within DETECTION_WINDOW_MIN before this point
        window_start = i
        while window_start > 0 and (t_now - times[window_start - 1]).total_seconds() / 60 < DETECTION_WINDOW_MIN:
            window_start -= 1
        if i - window_start < MIN_SNAPSHOTS_BASELINE:
            continue

        baseline_samples = scores[window_start:i]
        baseline_mean = sum(baseline_samples) / len(baseline_samples)
        curr = scores[i]

        if baseline_mean < MIN_BASELINE_SCORE:
            continue

        abs_delta = abs(curr - baseline_mean)
        if abs_delta < MIN_ABS_CHANGE:
            continue

        pct = (curr - baseline_mean) / baseline_mean

        our_share_before = (sum(our[window_start:i]) / len(baseline_samples)) / 2.0
        our_share_after  = our[i] / 2.0

        def _can_emit(etype: str) -> bool:
            prior = last_emit.get(etype)
            if prior and (t_now - prior).total_seconds() / 60 < COOLDOWN_MIN:
                return False
            last_emit[etype] = t_now
            return True

        if pct <= -WITHDRAWAL_PCT and _can_emit("withdrawal"):
            events.append({
                "market_ticker":    ticker,
                "detected_at":      t_now.isoformat(),
                "event_type":       "withdrawal",
                "prev_score":       round(baseline_mean, 2),
                "curr_score":       round(curr, 2),
                "pct_change":       round(pct, 3),
                "our_share_before": round(our_share_before, 3),
                "our_share_after":  round(our_share_after, 3),
                "window_min":       DETECTION_WINDOW_MIN,
                "notes":            f"baseline_n={len(baseline_samples)}",
            })
        elif pct >= ENTRY_PCT and _can_emit("entry"):
            events.append({
                "market_ticker":    ticker,
                "detected_at":      t_now.isoformat(),
                "event_type":       "entry",
                "prev_score":       round(baseline_mean, 2),
                "curr_score":       round(curr, 2),
                "pct_change":       round(pct, 3),
                "our_share_before": round(our_share_before, 3),
                "our_share_after":  round(our_share_after, 3),
                "window_min":       DETECTION_WINDOW_MIN,
                "notes":            f"baseline_n={len(baseline_samples)}",
            })

    return events


def persist_events(events: list[dict], db_path: str = DB_PATH) -> int:
    """Insert events, skipping duplicates (same market + time + type)."""
    if not events:
        return 0
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        for e in events:
            exists = conn.execute(
                """SELECT 1 FROM competitor_events
                   WHERE market_ticker=? AND detected_at=? AND event_type=?""",
                (e["market_ticker"], e["detected_at"], e["event_type"]),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO competitor_events
                   (market_ticker, detected_at, event_type, prev_score, curr_score,
                    pct_change, our_share_before, our_share_after, window_min, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (e["market_ticker"], e["detected_at"], e["event_type"],
                 e["prev_score"], e["curr_score"], e["pct_change"],
                 e["our_share_before"], e["our_share_after"],
                 e["window_min"], e["notes"]),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def scan_all_markets(lookback_min: int = 60, db_path: str = DB_PATH) -> dict:
    """Scan every market with recent snapshots for events."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        tickers = [r[0] for r in conn.execute(
            """SELECT DISTINCT market_ticker FROM lip_snapshots
               WHERE datetime(captured_at) > datetime('now', ?)""",
            (f"-{lookback_min} minute",),
        ).fetchall()]
    finally:
        conn.close()

    summary = {"markets_scanned": len(tickers),
               "withdrawals": 0, "entries": 0, "new_events": 0,
               "recent_withdrawals": [], "recent_entries": []}

    all_events: list[dict] = []
    for t in tickers:
        evs = analyze_market(t, lookback_min=lookback_min, db_path=db_path)
        all_events.extend(evs)

    for e in all_events:
        if e["event_type"] == "withdrawal":
            summary["withdrawals"] += 1
        elif e["event_type"] == "entry":
            summary["entries"] += 1

    summary["new_events"] = persist_events(all_events, db_path=db_path)

    # Top-5 most recent of each type
    all_events.sort(key=lambda x: x["detected_at"], reverse=True)
    summary["recent_withdrawals"] = [e for e in all_events if e["event_type"] == "withdrawal"][:5]
    summary["recent_entries"]     = [e for e in all_events if e["event_type"] == "entry"][:5]
    return summary


def active_withdrawals(active_window_min: int = 15,
                       db_path: str = DB_PATH) -> list[str]:
    """Return markets with a withdrawal event in the last N minutes.

    Quote loop reads this to decide WHICH markets to flat immediately.
    """
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT market_ticker FROM competitor_events
               WHERE event_type = 'withdrawal'
                 AND datetime(detected_at) > datetime('now', ?)""",
            (f"-{active_window_min} minute",),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def auto_blacklist_withdrawals(blacklist_hours: int = 2,
                                active_window_min: int = 5,
                                db_path: str = DB_PATH) -> int:
    """Inject withdrawal-flagged markets into market_blacklist.

    GUARDS (don't follow noise):
      1. Only blacklist if our_share_before < 25% (if we dominate, a smaller
         competitor exiting = more room for us, NOT toxic flow).
      2. Only blacklist if absolute score drop ≥ 100 points (filters out
         thin-market percentage noise).
      3. Require ≥2 withdrawal events in last 10 min (sustained exodus, not
         single-point).

    These guards protect us from de-quoting our own winners.
    """
    ensure_schema(db_path)
    inserted = 0
    conn = sqlite3.connect(db_path)
    try:
        # Aggregate withdrawals per market with guards applied
        rows = conn.execute(
            """SELECT market_ticker,
                      COUNT(*)                                AS n_events,
                      AVG(our_share_before)                   AS avg_our_share,
                      AVG(prev_score - curr_score)            AS avg_abs_drop,
                      MAX(detected_at)                        AS last_event
               FROM competitor_events
               WHERE event_type = 'withdrawal'
                 AND datetime(detected_at) > datetime('now', ?)
                 AND acted_on = 0
               GROUP BY market_ticker
               HAVING n_events >= 2
                  AND (avg_our_share IS NULL OR avg_our_share < 0.25)
                  AND avg_abs_drop >= 100""",
            (f"-{active_window_min*2} minute",),
        ).fetchall()

        for tkr, n_events, share, abs_drop, last_event in rows:
            existing = conn.execute(
                "SELECT expires_at FROM market_blacklist WHERE ticker = ?",
                (tkr,),
            ).fetchone()
            if existing:
                continue
            reason = f"competitor_withdrawal (n={n_events}, drop={abs_drop:.0f})"
            conn.execute(
                """INSERT OR REPLACE INTO market_blacklist
                   (ticker, expires_at, reason, added_at)
                   VALUES (?, datetime('now', ?), ?, datetime('now'))""",
                (tkr, f"+{blacklist_hours} hour", reason),
            )
            inserted += 1

        # Mark ALL recent withdrawals acted_on (whether we blacklisted or not)
        conn.execute(
            """UPDATE competitor_events
                  SET acted_on = 1
                WHERE event_type = 'withdrawal'
                  AND datetime(detected_at) > datetime('now', ?)
                  AND acted_on = 0""",
            (f"-{active_window_min} minute",),
        )
        conn.commit()
    finally:
        conn.close()
    return inserted


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-min", type=int, default=60)
    p.add_argument("--ticker", type=str, default=None, help="scan single market")
    a = p.parse_args()

    if a.ticker:
        evs = analyze_market(a.ticker, lookback_min=a.lookback_min)
        print(f"\n{a.ticker}  ({len(evs)} events in last {a.lookback_min}min)")
        for e in evs:
            print(f"  {e['detected_at'][:19]}  {e['event_type']:12s}  "
                  f"{e['prev_score']:.1f}→{e['curr_score']:.1f}  "
                  f"({e['pct_change']*100:+.0f}%)  "
                  f"our_share: {e['our_share_before']*100:.1f}%→{e['our_share_after']*100:.1f}%")
        return

    s = scan_all_markets(lookback_min=a.lookback_min)
    # NOTE: auto_blacklist_withdrawals is defined but NOT called.
    # Observation-only mode for now — operator reviews dashboard; auto-action
    # gated until we have settlement-window + cross-market correlation signals.
    print(f"\n══ COMPETITOR INTEL SCAN (last {a.lookback_min}min) ══")
    print(f"  markets scanned:     {s['markets_scanned']}")
    print(f"  withdrawal events:   {s['withdrawals']}")
    print(f"  entry events:        {s['entries']}")
    print(f"  new events persisted:{s['new_events']}")

    if s["recent_withdrawals"]:
        print(f"\n  🔴 RECENT WITHDRAWALS (follow them out):")
        for e in s["recent_withdrawals"]:
            print(f"    {e['detected_at'][:19]}  {e['market_ticker'][:36]:<38s}  "
                  f"score {e['prev_score']:.0f}→{e['curr_score']:.0f} ({e['pct_change']*100:+.0f}%)")

    if s["recent_entries"]:
        print(f"\n  🟢 RECENT ENTRIES (new competitor or farmer):")
        for e in s["recent_entries"]:
            print(f"    {e['detected_at'][:19]}  {e['market_ticker'][:36]:<38s}  "
                  f"score {e['prev_score']:.0f}→{e['curr_score']:.0f} ({e['pct_change']*100:+.0f}%)")


if __name__ == "__main__":
    main()
