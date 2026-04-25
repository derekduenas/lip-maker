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


def find_recently_settled(hours_back: int = 24, db_path: str = settings.DB_PATH) -> list[dict]:
    """Query Kalshi for markets that settled in last N hours.

    Staggered 0.5s between series calls + exponential-backoff retry on 429.
    """
    import time
    k = KalshiClient()
    settled: list[dict] = []
    now = datetime.now(timezone.utc)
    cutoff_secs = hours_back * 3600

    for i, prefix in enumerate(FUTURES_MAP):
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

    recent = find_recently_settled(hours_back=30, db_path=db_path)
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

        # Our position at settle time (from fill_ledger cumulative)
        fills = conn.execute(
            """SELECT side, SUM(count) FROM fill_ledger
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
            """SELECT side, SUM(count * (CASE WHEN side='yes' THEN yes_price_cents ELSE no_price_cents END))
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

        # Rebate contribution (rough): share × reward × days_active. Not easily
        # recovered from snapshots without big query. Leave as 0 for now — this
        # row's "net_outcome_usd" reflects principal-only P&L.
        rebate = 0.0
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
    a = p.parse_args()
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
