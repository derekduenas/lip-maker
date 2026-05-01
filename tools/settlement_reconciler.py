"""Settlement reconciliation — learn from actual outcomes vs our predictions.

Each daily/weekly market settles on a specific date. When it settles, Kalshi
publishes a `settlement_value` (the close price of the referenced contract's
1-minute candle). We compare:

  1. Our ITM/OTM prediction from futures_feed at settle time (recorded pre-settle)
  2. Kalshi's actual settlement value
  3. Our P&L per position (from fill_ledger × settle outcome)

Over 5-7 days of settled markets, we build per-commodity calibration:
  - Did our futures reference match Kalshi's settle within X%?
  - Which markets cost us $ (principal loss > rebate earned)?
  - Which markets we should AVOID or UNDERWEIGHT going forward?

Run daily at 22:00 UTC (1h after daily settle) via cron.
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
from execution.kalshi_auth import KalshiClient
from engine.futures_feed import FUTURES_MAP

_log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker               TEXT NOT NULL,
    series_prefix        TEXT NOT NULL,
    close_time           TEXT NOT NULL,
    strike               REAL,
    kalshi_settle_value  REAL,
    kalshi_result        TEXT,
    futures_fair         REAL,
    futures_confidence   TEXT,
    predicted_result     TEXT,
    prediction_correct   INTEGER,
    delta_kalshi_futures REAL,
    our_position_yes     INTEGER,
    our_position_no      INTEGER,
    our_realized_usd     REAL,
    rebate_earned_usd    REAL,
    net_outcome_usd      REAL,
    recorded_at          TEXT NOT NULL,
    UNIQUE(ticker)
);
CREATE INDEX IF NOT EXISTS idx_settle_prefix_time ON settlement_log(series_prefix, close_time);
"""


# Each captured snapshot represents this many seconds of presence on
# average. Heartbeat fires every 30s; on_book_update throttled to 1/5s.
# Calibrated 2026-04-28 against Kalshi UI ground truth across 8 series:
#   Multiplier=10 → ratio=0.30 (3x undercount)
#   Multiplier=30 → ratio=0.90 (close to truth, slight under)
# 30 = heartbeat cadence = upper bound on snapshot interval.
# Per-series variation 0.18-0.68 suggests true rate is market-specific
# (busy markets = more book-driven snaps = lower effective interval).
# Future: persist per-snapshot interval and use it directly.
SNAPSHOT_REPRESENTS_SECONDS = 30.0


def _estimate_rebate(conn: sqlite3.Connection, ticker: str) -> float:
    """#120 — compute estimated LIP rebate from lip_snapshots data.

    Kalshi LIP payout formula:
      payout_per_kalshi_snapshot = (our_score / total_score) × (period_reward / period_seconds)
      total_payout = SUM(payout_per_kalshi_snapshot) over all of Kalshi's
                     1-second snapshots in the period

    Our snapshots are sparser (heartbeat 30s + book-driven). Each of OUR
    snapshots represents ~SNAPSHOT_REPRESENTS_SECONDS of Kalshi-side
    snapshots, so we scale up.

    Tries was_resting=1 filter first (post-phantom-fix data). If empty
    (pre-fix settlement), falls back to snapshot_valid=1 only — pre-fix
    we WERE resting whenever we had quotes, the column just wasn't tracked.

    Returns 0.0 if any required data is missing — never raises.
    """
    def _query(strict: bool) -> tuple | None:
        try:
            extra = "AND s.was_resting = 1" if strict else ""
            return conn.execute(
                f"""SELECT
                     COALESCE(SUM(CASE WHEN s.total_score > 0
                                       THEN s.our_score * 1.0 / s.total_score
                                       ELSE 0 END), 0) AS sum_share,
                     p.period_reward_usd,
                     p.start_date,
                     p.end_date,
                     COUNT(*) AS n_snaps
                   FROM lip_snapshots s
                   JOIN lip_programs p ON p.market_ticker = s.market_ticker
                   WHERE s.market_ticker = ?
                     AND s.snapshot_valid = 1
                     {extra}
                   GROUP BY p.market_ticker""",
                (ticker,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None

    row = _query(strict=True)
    if not row or not row[4] or row[4] == 0:
        # Pre-phantom-fix data — was_resting column wasn't tracked. Fall
        # back to all valid snapshots.
        row = _query(strict=False)
    if not row or not row[0] or not row[1]:
        return 0.0
    sum_share = float(row[0])
    period_reward = float(row[1])
    try:
        sd = datetime.fromisoformat((row[2] or "").replace("Z", "+00:00"))
        ed = datetime.fromisoformat((row[3] or "").replace("Z", "+00:00"))
        period_seconds = max(1, (ed - sd).total_seconds())
    except Exception:
        period_seconds = 86400.0  # fallback to 1 day
    rebate = sum_share * period_reward / period_seconds * SNAPSHOT_REPRESENTS_SECONDS
    return round(rebate, 4)


def backfill_rebates(db_path: str = settings.DB_PATH,
                     force: bool = False) -> dict:
    """One-shot backfill — recompute rebate_earned_usd for existing
    settlement_log rows. By default only updates rows still at 0;
    pass force=True to recompute every row (use after multiplier change)."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        if force:
            rows = conn.execute("SELECT ticker FROM settlement_log").fetchall()
        else:
            rows = conn.execute(
                """SELECT ticker FROM settlement_log
                   WHERE rebate_earned_usd = 0 OR rebate_earned_usd IS NULL"""
            ).fetchall()
        updated = 0
        total_rebate = 0.0
        for (tkr,) in rows:
            r = _estimate_rebate(conn, tkr)
            if r > 0:
                conn.execute(
                    """UPDATE settlement_log
                       SET rebate_earned_usd = ?,
                           net_outcome_usd = COALESCE(our_realized_usd, 0) + ?
                       WHERE ticker = ?""",
                    (r, r, tkr),
                )
                updated += 1
                total_rebate += r
        conn.commit()
    finally:
        conn.close()
    return {
        "rows_scanned": len(rows),
        "rows_updated": updated,
        "total_rebate_backfilled": round(total_rebate, 2),
    }


def _all_relevant_series(db_path: str) -> list[str]:
    """#128 (2026-04-28): return union of FUTURES_MAP + all enrolled
    series + any series we've actually traded on (fill_ledger). Without
    this, settlement_log was COMMODITY-ONLY and our political/event PnL
    was invisible.
    """
    series = set(FUTURES_MAP.keys())
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            # Currently enrolled
            rows = conn.execute(
                """SELECT DISTINCT series_ticker FROM lip_programs
                   WHERE enrolled = 1 AND series_ticker IS NOT NULL"""
            ).fetchall()
            series.update(r[0] for r in rows if r[0])
            # Historically traded (catches series that settled after enrollment ended)
            rows = conn.execute(
                """SELECT DISTINCT substr(ticker, 1, instr(ticker || '-', '-') - 1) AS s
                   FROM fill_ledger WHERE ticker IS NOT NULL"""
            ).fetchall()
            series.update(r[0] for r in rows if r[0])
        finally:
            conn.close()
    except sqlite3.OperationalError:
        pass
    return sorted(series)


def find_recently_settled(hours_back: int = 24, db_path: str = settings.DB_PATH) -> list[dict]:
    """Query Kalshi for markets that settled in last N hours, across
    ALL relevant series (commodity + political/event/macro).

    Staggered 0.5s between series calls + exponential-backoff retry on 429.
    """
    import time
    k = KalshiClient()
    settled: list[dict] = []
    now = datetime.now(timezone.utc)
    cutoff_secs = hours_back * 3600

    series_list = _all_relevant_series(db_path)
    _log.info(f"settlement scan: querying {len(series_list)} series "
              f"({len(FUTURES_MAP)} commodity + {len(series_list)-len(FUTURES_MAP)} other)")

    for i, prefix in enumerate(series_list):
        if i > 0:
            time.sleep(0.5)  # stagger to avoid 429
        # Retry with exponential backoff on 429
        backoff = 2.0
        r = None
        for attempt in range(4):
            try:
                r = k.get_unauth("/markets", params={
                    "series_ticker": prefix, "status": "settled", "limit": 50
                })
                break
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 3:
                    _log.info(f"429 on {prefix}, backoff {backoff}s (attempt {attempt+1})")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                _log.warning(f"failed to fetch settled {prefix}: {e}")
                break
        if r is None:
            continue

        for m in r.get("markets", []):
            close = m.get("close_time", "")
            if not close:
                continue
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            except Exception:
                continue
            if (now - close_dt).total_seconds() > cutoff_secs:
                continue
            settled.append({
                "ticker":       m.get("ticker"),
                "prefix":       prefix,
                "close_time":   close,
                "settle_value": m.get("settlement_value") or m.get("expiration_value"),
                "result":       m.get("result"),
                "title":        m.get("title", ""),
            })
    return settled


def reconcile(db_path: str = settings.DB_PATH) -> dict:
    """Record settlements + compute accuracy vs our futures prediction."""
    import re
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()

    recent = find_recently_settled(hours_back=72, db_path=db_path)  # #128b 2026-04-29: bump 30→72 to survive 2-day reconciler outages
    new_rows = 0
    correct = 0
    predicted_count = 0   # only rows where we had futures data
    total_recorded = 0
    total_net_outcome = 0.0

    for s in recent:
        tkr = s["ticker"]
        # Skip if already recorded
        if conn.execute("SELECT 1 FROM settlement_log WHERE ticker=?", (tkr,)).fetchone():
            continue

        # Extract strike
        m = re.search(r"-T([\d.]+)$", tkr)
        strike = float(m.group(1)) if m else None

        # Our position at settle time (from fill_ledger cumulative).
        # 2026-04-29 #143: prefer count_real (REAL preserves Kalshi's
        # fractional count_fp, e.g. 12.87) over legacy count INTEGER.
        # ~20% of fills are fractional; int() truncation lifetime-
        # understated PnL by ~10x on a $700 gap.
        fills = conn.execute(
            """SELECT side, SUM(COALESCE(count_real, count)) FROM fill_ledger
               WHERE ticker = ? GROUP BY side""",
            (tkr,),
        ).fetchall()
        yes_n = sum(v for side, v in fills if side == "yes")
        no_n  = sum(v for side, v in fills if side == "no")

        # Realized P&L from this ticker:
        # If YES +X and settle=YES: +X * $1 - cost
        # If NO +X and settle=NO:   +X * $1 - cost
        # If settles opposite: cost is total loss
        # Cost = sum(count × price) per side
        cost_rows = conn.execute(
            """SELECT side, SUM(COALESCE(count_real, count) *
                                (CASE WHEN side='yes' THEN yes_price_cents ELSE no_price_cents END))
               FROM fill_ledger WHERE ticker=? GROUP BY side""",
            (tkr,),
        ).fetchall()
        yes_cost = sum(v / 100.0 for side, v in cost_rows if side == "yes") if cost_rows else 0
        no_cost  = sum(v / 100.0 for side, v in cost_rows if side == "no")  if cost_rows else 0

        settle_val = s["settle_value"]
        kalshi_result = s["result"]  # "yes" or "no"

        # Payout per side: YES pays $1 if result=yes, else $0; NO pays $1 if result=no
        if kalshi_result == "yes":
            yes_payout = yes_n * 1.0
            no_payout  = 0
        elif kalshi_result == "no":
            yes_payout = 0
            no_payout  = no_n * 1.0
        else:
            yes_payout = no_payout = 0
        realized = (yes_payout - yes_cost) + (no_payout - no_cost)

        # Compare against our futures-based prediction
        fair = None
        conf = FUTURES_MAP.get(s["prefix"], {}).get("confidence", "unknown")
        predicted_result = None
        prediction_correct = None
        delta_kf = None

        # Lookup latest futures price near close_time from futures_prices table
        try:
            fair_row = conn.execute(
                """SELECT price FROM futures_prices
                   WHERE kalshi_prefix = ? AND fetched_at < ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (s["prefix"], s["close_time"]),
            ).fetchone()
            if fair_row:
                fair = fair_row[0]
                if strike is not None:
                    predicted_result = "yes" if fair > strike else "no"
                    prediction_correct = 1 if predicted_result == kalshi_result else 0
                if settle_val is not None:
                    delta_kf = settle_val - fair
        except Exception:
            pass

        # #120 (2026-04-28): compute estimated rebate from lip_snapshots.
        # Formula matches Kalshi's LIP payout:
        #   payout = SUM(our_score / total_score) over all valid snapshots
        #            × period_reward_usd / period_seconds
        # Each snapshot represents ~1 second of presence. Under-counts when
        # our snapshot rate < Kalshi's 1Hz, but under-estimate is preferable
        # to over-estimate for safety-gate decisions. Verify against Kalshi
        # UI ground truth for known markets after first run.
        rebate = _estimate_rebate(conn, tkr)
        net = realized + rebate

        conn.execute(
            """INSERT OR IGNORE INTO settlement_log
               (ticker, series_prefix, close_time, strike, kalshi_settle_value,
                kalshi_result, futures_fair, futures_confidence, predicted_result,
                prediction_correct, delta_kalshi_futures, our_position_yes,
                our_position_no, our_realized_usd, rebate_earned_usd,
                net_outcome_usd, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tkr, s["prefix"], s["close_time"], strike, settle_val,
             kalshi_result, fair, conf, predicted_result,
             prediction_correct, delta_kf, int(yes_n), int(no_n),
             round(realized, 4), rebate, round(net, 4),
             datetime.now(timezone.utc).isoformat()),
        )
        new_rows += 1
        total_recorded += 1
        if prediction_correct is not None:
            predicted_count += 1
            if prediction_correct == 1:
                correct += 1
        total_net_outcome += net

    conn.commit()

    # Aggregate per-series summary. COALESCE to handle markets with NULL
    # prediction_correct (no futures price at settle time).
    per_series = {}
    rows = conn.execute(
        """SELECT series_prefix,
                  COUNT(*) AS n,
                  COUNT(prediction_correct) AS n_with_pred,
                  COALESCE(SUM(prediction_correct), 0) AS n_correct,
                  COALESCE(SUM(our_realized_usd), 0) AS total_realized,
                  AVG(ABS(delta_kalshi_futures)) AS avg_delta
           FROM settlement_log
           GROUP BY series_prefix"""
    ).fetchall()
    for prefix, n, n_pred, n_c, total, avg_delta in rows:
        # Only compute WR if we have markets where a prediction was actually made
        wr = round(n_c / n_pred, 3) if n_pred and n_pred > 0 else None
        per_series[prefix] = {
            "settled_markets":   n,
            "predicted_markets": n_pred or 0,
            "prediction_wr":     wr,
            "total_realized":    round(total or 0, 2),
            "avg_futures_delta": round(avg_delta or 0, 4) if avg_delta is not None else None,
        }

    conn.close()
    return {
        "new_rows":         new_rows,
        "newly_predicted":  predicted_count,
        "newly_correct":    correct,
        "new_accuracy":     round(correct / predicted_count, 3) if predicted_count else None,
        "new_net_outcome":  round(total_net_outcome, 2),
        "per_series":       per_series,
    }


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true",
                   help="Recompute rebate_earned_usd for existing rows where it's 0")
    p.add_argument("--force", action="store_true",
                   help="With --backfill: recompute ALL rows, not just zeros")
    a = p.parse_args()
    if a.backfill:
        b = backfill_rebates(force=a.force)
        print(f"\n══ REBATE BACKFILL ══")
        print(f"  rows scanned:           {b['rows_scanned']}")
        print(f"  rows updated:           {b['rows_updated']}")
        print(f"  total rebate backfilled: ${b['total_rebate_backfilled']}")
        return
    r = reconcile()
    print(f"\n══ SETTLEMENT RECONCILIATION ══")
    print(f"  new markets recorded: {r['new_rows']}")
    if r['new_rows'] > 0:
        if r.get('newly_predicted', 0) > 0:
            print(f"  predictions made:     {r['newly_predicted']}/{r['new_rows']} (remaining had no futures data)")
            print(f"  prediction accuracy:  {r['newly_correct']}/{r['newly_predicted']}  ({(r['new_accuracy'] or 0)*100:.1f}%)")
            print(f"  ⚠ CAVEAT: predictions use futures-price AT settle time, not hours ahead.")
            print(f"    High accuracy = our futures feed tracks underlying, NOT that we can forecast direction.")
        print(f"  realized P&L total:   ${r['new_net_outcome']:+.2f}")
    print(f"\n  per-series calibration (all-time):")
    print(f"  {'prefix':15s}  {'n':>3s}  {'acc':>6s}  {'realized':>10s}  {'avg_delta':>9s}")
    for prefix, stats in sorted(r["per_series"].items()):
        acc = f"{(stats['prediction_wr'] or 0)*100:.0f}%" if stats['prediction_wr'] is not None else "-"
        delta_str = f"${stats['avg_futures_delta']:>7.4f}" if stats['avg_futures_delta'] is not None else "      -"
        print(f"  {prefix:15s}  {stats['settled_markets']:>3}  {acc:>6}  "
              f"${stats['total_realized']:>+8.2f}  {delta_str}")


if __name__ == "__main__":
    main()
