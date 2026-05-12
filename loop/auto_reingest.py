"""Auto-reingest the corpus after an earnings market resolves.

When a market we just traded settles, the official transcript publishes 3-24h
later. The model's Poisson base-rate per (speaker, term) only improves if we
fold each new quarter back in. This loop runs after the reviewer and:

  1. For each ticker with a market resolved in the last `lookback_hours`,
     check whether we have a 'success' row in reingest_log for the same
     (ticker, settle_date). Skip if yes — never re-run a completed reingest.
  2. Snapshot the speaker's frequency_matrix BEFORE ingest.
  3. Call corpus/auto_ingest.auto_ingest(ticker) — fetches any missing
     quarters from Motley Fool / Insider Monkey, ingests, rebuilds matrix.
  4. Snapshot AFTER ingest. Compute λ_before, λ_after, Δλ per tracked term.
  5. Persist the diff to reingest_log so cron retries are correct:
       - status='success' if any quarter ingested
       - status='transcript_pending' if no new transcripts found (retry later)
       - status='no_speaker' if speaker is unknown (don't retry)
       - status='failed' for unexpected errors (retry with backoff)

Content-hash dedup: the ingest layer now hashes raw_text on INSERT and
rejects duplicates. So even if the (ticker, quarter) heuristic misses an
edge case, the same transcript text can't enter the corpus twice.

USAGE:
  python -m loop.auto_reingest                # one cycle (cron-friendly)
  python -m loop.auto_reingest --json
  python -m loop.auto_reingest --dry-run      # don't fetch/ingest, just plan
  python -m loop.auto_reingest --lookback 168 # 7-day lookback
  python -m loop.auto_reingest --simulate TICKER  # test path on one ticker

Cron: every 3h. Earnings transcripts publish irregularly so frequent retries
are cheap and keep the per-CEO matrix current.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

_log = logging.getLogger("auto_reingest")


SCHEMA = """
CREATE TABLE IF NOT EXISTS reingest_log (
    id                  INTEGER PRIMARY KEY,
    ticker              TEXT NOT NULL,
    settle_date         DATE NOT NULL,
    attempted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status              TEXT NOT NULL,         -- success | transcript_pending | no_speaker | failed
    quarters_added      INTEGER DEFAULT 0,
    corpus_n_before     INTEGER DEFAULT 0,
    corpus_n_after      INTEGER DEFAULT 0,
    lambda_diff_json    TEXT,                  -- [{term, base_rate_before, base_rate_after, delta}, ...]
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_reingest_log_ticker ON reingest_log(ticker, settle_date);
CREATE INDEX IF NOT EXISTS idx_reingest_log_status ON reingest_log(status);
"""

# Lookback / retry parameters
DEFAULT_LOOKBACK_HOURS = 48
MAX_RETRIES_BEFORE_GIVEUP = 8        # ~24h of 3-hourly cycles
RETRY_PENDING_MIN_HOURS = 2          # don't re-fetch within 2h of a pending attempt


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.executescript(SCHEMA)
    # transcripts.content_hash migration (idempotent)
    try:
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(transcripts)")]
        if "content_hash" not in tcols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN content_hash TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transcripts_content_hash "
                "ON transcripts(content_hash)"
            )
    except sqlite3.OperationalError as e:
        _log.warning(f"transcripts.content_hash migration skipped: {e}")
    conn.commit()
    return conn


def compute_content_hash(text: str) -> str:
    """sha256 over normalized whitespace — captures duplicate transcripts
    even if a minor reformat slipped past the URL-based dedup."""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── Snapshot helpers ────────────────────────────────────────────────────────
def _snapshot_speaker_lambdas(conn: sqlite3.Connection, speaker: str,
                              event_type: str) -> dict[str, dict]:
    """Pull current frequency_matrix rows for (speaker, event_type).

    Returns: {term: {base_rate, occurrences, total_events}}
    Empty dict if the matrix has no rows yet (first-ever ingest case).
    """
    rows = conn.execute(
        """SELECT term, base_rate, occurrences, total_events
           FROM frequency_matrix
           WHERE event_type=? AND (speaker=? OR speaker='' OR speaker IS NULL)""",
        (event_type, speaker),
    ).fetchall()
    return {
        t: {"base_rate": br, "occurrences": k, "total_events": n}
        for (t, br, k, n) in rows
    }


def _diff_lambdas(before: dict, after: dict) -> list[dict]:
    """Compute Δ per term. Includes terms present in EITHER snapshot."""
    all_terms = set(before) | set(after)
    out = []
    for t in sorted(all_terms):
        b = before.get(t, {})
        a = after.get(t, {})
        br_b = b.get("base_rate")
        br_a = a.get("base_rate")
        delta = None
        if br_a is not None and br_b is not None:
            delta = round(br_a - br_b, 4)
        elif br_a is not None:
            delta = round(br_a, 4)           # new term
        out.append({
            "term":              t,
            "base_rate_before":  None if br_b is None else round(br_b, 4),
            "base_rate_after":   None if br_a is None else round(br_a, 4),
            "delta":             delta,
            "k_before":          b.get("occurrences"),
            "k_after":           a.get("occurrences"),
            "n_before":          b.get("total_events"),
            "n_after":           a.get("total_events"),
        })
    return out


def _had_successful_reingest(conn: sqlite3.Connection,
                             ticker: str, settle_date: str) -> bool:
    r = conn.execute(
        """SELECT 1 FROM reingest_log
           WHERE ticker=? AND settle_date=? AND status='success' LIMIT 1""",
        (ticker, settle_date),
    ).fetchone()
    return r is not None


def _pending_attempt_count(conn: sqlite3.Connection,
                           ticker: str, settle_date: str) -> int:
    r = conn.execute(
        """SELECT COUNT(*) FROM reingest_log
           WHERE ticker=? AND settle_date=?
           AND status IN ('transcript_pending', 'failed')""",
        (ticker, settle_date),
    ).fetchone()
    return int(r[0] or 0)


def _last_attempt_age_hours(conn: sqlite3.Connection,
                            ticker: str, settle_date: str) -> float | None:
    r = conn.execute(
        """SELECT (julianday('now') - julianday(attempted_at)) * 24.0
           FROM reingest_log
           WHERE ticker=? AND settle_date=?
           ORDER BY attempted_at DESC LIMIT 1""",
        (ticker, settle_date),
    ).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _find_resolved_pending_reingest(conn: sqlite3.Connection,
                                    lookback_hours: int) -> list[dict]:
    """List (ticker, settle_date) pairs for markets resolved in lookback
    window that don't yet have a 'success' reingest_log row."""
    # Pull resolved trades + map to ticker / settle_date. The trades table
    # carries kalshi_market_id which we can parse for the underlying ticker
    # (event_type column on opportunities also works — using that joins
    # cleaner). resolved_at is the settle timestamp.
    rows = conn.execute(
        """SELECT DISTINCT
              t.kalshi_market_id,
              o.term AS term,
              date(t.resolved_at) AS settle_date,
              t.resolved_at
           FROM trades t
           LEFT JOIN opportunities o ON o.id = t.opportunity_id
           WHERE t.resolved_at IS NOT NULL
             AND t.resolved_at >= datetime('now', ?)
             AND t.outcome IN ('WIN', 'LOSS')""",
        (f'-{int(lookback_hours)} hours',),
    ).fetchall()

    candidates: dict[tuple[str, str], dict] = {}
    for kalshi_id, term, settle_date, _resolved in rows:
        try:
            from engine.scanner import classify_market
            cls = classify_market({"ticker": kalshi_id, "event_ticker": kalshi_id,
                                   "title": "", "rules": ""})
        except Exception:
            cls = {}
        et = cls.get("event_type", "")
        if not et.endswith("_earnings"):
            continue                                       # this loop is for earnings only
        ticker = et.replace("_earnings", "").upper()
        key = (ticker, settle_date)
        if key in candidates:
            continue
        if _had_successful_reingest(conn, ticker, settle_date):
            continue
        if _pending_attempt_count(conn, ticker, settle_date) >= MAX_RETRIES_BEFORE_GIVEUP:
            continue
        age_h = _last_attempt_age_hours(conn, ticker, settle_date)
        if age_h is not None and age_h < RETRY_PENDING_MIN_HOURS:
            continue                                       # too soon to retry
        candidates[key] = {
            "ticker":       ticker,
            "settle_date":  settle_date,
            "event_type":   et,
        }
    return list(candidates.values())


# ── Main loop ───────────────────────────────────────────────────────────────
def reingest_one(conn: sqlite3.Connection, *, ticker: str, settle_date: str,
                 event_type: str, dry_run: bool = False) -> dict:
    """Process one (ticker, settle_date). Writes a reingest_log row."""
    from corpus.sources import get_speaker, TICKER_SPEAKER_MAP

    speaker = TICKER_SPEAKER_MAP.get(ticker) or get_speaker(ticker)
    if not speaker or speaker == "unknown":
        _persist(conn, ticker=ticker, settle_date=settle_date,
                 status="no_speaker", n_before=0, n_after=0, lambdas=[],
                 notes=f"speaker unknown for {ticker}")
        return {"ticker": ticker, "settle_date": settle_date,
                "status": "no_speaker"}

    # n_before + λ_before
    n_before = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchone()[0]
    lambdas_before = _snapshot_speaker_lambdas(conn, speaker, event_type)

    if dry_run:
        return {
            "ticker": ticker, "settle_date": settle_date,
            "status": "dry_run", "n_before": n_before,
            "speaker": speaker, "event_type": event_type,
            "n_lambdas_before": len(lambdas_before),
        }

    # Real ingest. auto_ingest is idempotent: it queries _get_missing_quarters
    # and skips already-ingested ones, so calling it repeatedly is safe.
    try:
        from corpus.auto_ingest import auto_ingest
        res = auto_ingest(ticker)
    except Exception as e:
        _log.exception(f"auto_ingest({ticker}) raised")
        _persist(conn, ticker=ticker, settle_date=settle_date,
                 status="failed", n_before=n_before, n_after=n_before,
                 lambdas=[], notes=f"exception: {e}")
        return {"ticker": ticker, "settle_date": settle_date,
                "status": "failed", "error": str(e)}

    ingested = int(res.get("ingested") or 0)
    n_after = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchone()[0]
    lambdas_after = _snapshot_speaker_lambdas(conn, speaker, event_type)
    diff = _diff_lambdas(lambdas_before, lambdas_after)

    if ingested > 0:
        status = "success"
    else:
        status = "transcript_pending"
    _persist(conn, ticker=ticker, settle_date=settle_date, status=status,
             n_before=n_before, n_after=n_after, lambdas=diff,
             notes=f"auto_ingest status={res.get('status')} ingested={ingested}")
    moved = [d for d in diff if d["delta"] not in (None, 0)]
    return {
        "ticker":         ticker,
        "settle_date":    settle_date,
        "status":         status,
        "speaker":        speaker,
        "ingested":       ingested,
        "n_before":       n_before,
        "n_after":        n_after,
        "lambda_diff":    diff,
        "moved_terms":    moved,
    }


def _persist(conn: sqlite3.Connection, *, ticker: str, settle_date: str,
             status: str, n_before: int, n_after: int,
             lambdas: list[dict], notes: str = "") -> None:
    conn.execute(
        """INSERT INTO reingest_log
           (ticker, settle_date, status, quarters_added,
            corpus_n_before, corpus_n_after, lambda_diff_json, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, settle_date, status, max(0, n_after - n_before),
         n_before, n_after, json.dumps(lambdas, default=str), notes),
    )
    conn.commit()


def run_auto_reingest(*, lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
                      dry_run: bool = False) -> dict:
    conn = _get_conn()
    try:
        pending = _find_resolved_pending_reingest(conn, lookback_hours)
        results = []
        for p in pending:
            r = reingest_one(conn, ticker=p["ticker"], settle_date=p["settle_date"],
                             event_type=p["event_type"], dry_run=dry_run)
            results.append(r)
        return {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "lookback_hours": lookback_hours,
            "candidates":    len(pending),
            "succeeded":     sum(1 for r in results if r["status"] == "success"),
            "pending":       sum(1 for r in results if r["status"] == "transcript_pending"),
            "no_speaker":    sum(1 for r in results if r["status"] == "no_speaker"),
            "failed":        sum(1 for r in results if r["status"] == "failed"),
            "dry_run":       dry_run,
            "results":       results,
        }
    finally:
        conn.close()


def simulate_one(ticker: str, *, dry_run: bool = False) -> dict:
    """One-shot path used by --simulate. Bypasses the trade-table lookup
    and runs the reingest logic on a specific ticker as if it had just
    resolved today. Returns the same shape as run_auto_reingest().results[0].
    """
    conn = _get_conn()
    try:
        from corpus.sources import get_event_type
        et = get_event_type(ticker)
        if not et.endswith("_earnings"):
            return {"ticker": ticker, "status": "not_earnings_ticker",
                    "event_type": et}
        settle_date = datetime.now(timezone.utc).date().isoformat()
        # Allow re-simulating an already-successful ticker; explicitly skip
        # the success guard so we can demonstrate λ before/after.
        return reingest_one(conn, ticker=ticker, settle_date=settle_date,
                            event_type=et, dry_run=dry_run)
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only — do not fetch or ingest")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_HOURS,
                    help=f"hours of trade history to scan (default {DEFAULT_LOOKBACK_HOURS})")
    ap.add_argument("--simulate", type=str, default=None,
                    help="run reingest for one ticker as if resolved today")
    a = ap.parse_args()

    if a.simulate:
        r = simulate_one(a.simulate.upper(), dry_run=a.dry_run)
        if a.json:
            print(json.dumps(r, indent=2, default=str))
        else:
            print(f"\n━━━ AUTO-REINGEST SIMULATE: {a.simulate.upper()} ━━━")
            for k, v in r.items():
                if k == "lambda_diff":
                    print(f"  {k}: {len(v)} terms")
                    moved = [d for d in v if d["delta"] not in (None, 0)]
                    for d in moved[:6]:
                        print(f"    {d['term']:<20}  "
                              f"λ {d['base_rate_before']} → {d['base_rate_after']}  "
                              f"(Δ {d['delta']:+.4f})  k {d['k_before']}→{d['k_after']}  "
                              f"n {d['n_before']}→{d['n_after']}")
                else:
                    print(f"  {k}: {v}")
        return 0

    res = run_auto_reingest(lookback_hours=a.lookback, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
        return 0

    print(f"\n━━━ AUTO-REINGEST — {res['ts']} ━━━")
    print(f"  lookback:    {res['lookback_hours']}h")
    print(f"  candidates:  {res['candidates']}")
    print(f"  succeeded:   {res['succeeded']}  "
          f"pending:   {res['pending']}  "
          f"no_speaker:{res['no_speaker']}  "
          f"failed:    {res['failed']}")
    if res['dry_run']:
        print(f"  (dry-run — no fetch/ingest)")
    for r in res["results"][:10]:
        moved = r.get("moved_terms", []) or []
        print(f"  - {r['ticker']:<6}  {r['status']:<22}  "
              f"ingested={r.get('ingested',0)}  Δterms={len(moved)}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
