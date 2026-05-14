"""Real-time unrealized PnL tracker (Phase 3.3).

Marks every open Kalshi position to microprice every cycle, writes a
row to `unrealized_pnl_snapshot`, alerts when any series accumulates
a rolling-1h unrealized loss > $50 (configurable).

Why this matters for live re-entry:
  - settlement_log only crystallizes PnL after the market closes
    (could be hours, days). Live we cannot wait that long.
  - Mark-to-microprice catches a bleeding series in 5-10 min instead
    of 5-7 days.
  - The alert path lets ramp_controller throttle / halt before another
    bleed-out compounds.

PIPELINE
--------
1. Run as a systemd timer every 5 min (deploy/unrealized-pnl.timer).
2. For each open inventory row: look up the latest microprice from
   book_microprice_history, compute unrealized = net_yes_contracts ×
   (mp_now − avg_entry_price).
3. Per-series totals → `unrealized_pnl_snapshot(ts, venue, series,
   unrealized_usd)`.
4. Compare to last 12 snapshots (rolling 1h); if delta > $50 loss,
   send alert via monitor.alerts.

USAGE
-----
    python monitor/unrealized_pnl.py            # one-shot run
    python monitor/unrealized_pnl.py --window-min 60 --alert-threshold 50
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS unrealized_pnl_snapshot (
    ts                  REAL NOT NULL,
    ts_iso              TEXT NOT NULL,
    venue               TEXT NOT NULL,         -- 'kalshi' | 'polymarket'
    series_prefix       TEXT NOT NULL,
    n_open_positions    INTEGER NOT NULL,
    net_contracts       INTEGER NOT NULL,      -- signed
    unrealized_usd      REAL NOT NULL,
    PRIMARY KEY (ts, venue, series_prefix)
);

CREATE INDEX IF NOT EXISTS idx_unreal_series_ts
    ON unrealized_pnl_snapshot(series_prefix, ts DESC);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def _latest_microprice_map(conn: sqlite3.Connection) -> dict[str, float]:
    """Most-recent microprice per ticker — single query."""
    rows = conn.execute(
        """SELECT ticker, microprice_c
           FROM book_microprice_history bm
           WHERE captured_ts = (
               SELECT MAX(captured_ts) FROM book_microprice_history bm2
               WHERE bm2.ticker = bm.ticker
           )"""
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def _series_prefix(ticker: str) -> str:
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def snapshot(db_path: str = settings.DB_PATH) -> dict:
    """Compute + persist one round of unrealized PnL snapshots.

    Returns summary dict with per-series totals and any alerts fired.
    """
    ensure_schema(db_path)
    import time as _time
    now = _time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        # Fetch open inventory rows (paper or live) from fill_ledger-derived
        # `inventory` table. Schema (per quote_manager._refresh_inventory):
        #   inventory(market_ticker, net_yes_contracts, gross_usd,
        #             avg_yes_entry, avg_no_entry, last_updated)
        try:
            inv_rows = conn.execute(
                """SELECT market_ticker, net_yes_contracts,
                          avg_yes_entry, avg_no_entry
                   FROM inventory
                   WHERE net_yes_contracts != 0"""
            ).fetchall()
        except sqlite3.OperationalError as e:
            _log.info(f"unrealized_pnl: inventory table missing ({e}); "
                      f"likely no positions yet (paper mode)")
            return {"per_series": {}, "alerts": [], "n_positions": 0}

        mp_map = _latest_microprice_map(conn)

        # Aggregate by series
        per_series: dict[str, dict] = {}
        for ticker, net_yes, avg_yes, avg_no in inv_rows:
            if net_yes is None or net_yes == 0:
                continue
            mp = mp_map.get(ticker)
            if mp is None:
                continue
            # Mark-to-microprice: profit = signed_qty × (mp_now − avg_entry)
            if net_yes > 0:
                # Long YES: profit = net_yes × (mp_now − avg_yes_entry_cents)
                entry_c = (float(avg_yes) * 100.0) if avg_yes is not None else mp
                unreal_usd = float(net_yes) * (mp - entry_c) / 100.0
            else:
                # Long NO (= short YES). NO contract value = (100 − mp).
                # avg_no is in dollars; entry NO price in cents:
                entry_c = (float(avg_no) * 100.0) if avg_no is not None else (100.0 - mp)
                unreal_usd = abs(float(net_yes)) * ((100.0 - mp) - entry_c) / 100.0

            prefix = _series_prefix(ticker)
            bucket = per_series.setdefault(prefix, {
                "n_open_positions": 0,
                "net_contracts": 0,
                "unrealized_usd": 0.0,
            })
            bucket["n_open_positions"] += 1
            bucket["net_contracts"] += int(net_yes)
            bucket["unrealized_usd"] += unreal_usd

        # Persist + alert
        alerts: list[dict] = []
        for prefix, b in per_series.items():
            conn.execute(
                """INSERT OR REPLACE INTO unrealized_pnl_snapshot
                   (ts, ts_iso, venue, series_prefix, n_open_positions,
                    net_contracts, unrealized_usd)
                   VALUES (?,?,?,?,?,?,?)""",
                (now, now_iso, "kalshi", prefix, b["n_open_positions"],
                 b["net_contracts"], round(b["unrealized_usd"], 2)),
            )
        conn.commit()

        # Rolling 1h delta per series — alert if drop > threshold
        threshold = -float(getattr(settings, "UNREALIZED_ALERT_USD", 50.0))
        for prefix, b in per_series.items():
            row = conn.execute(
                """SELECT unrealized_usd FROM unrealized_pnl_snapshot
                   WHERE series_prefix = ? AND venue = ?
                     AND ts >= ? AND ts < ?
                   ORDER BY ts ASC LIMIT 1""",
                (prefix, "kalshi", now - 3700, now - 3500),
            ).fetchone()
            if row is None:
                continue
            prior = float(row[0])
            delta = b["unrealized_usd"] - prior
            if delta < threshold:
                alerts.append({
                    "series": prefix, "delta_1h_usd": round(delta, 2),
                    "now_usd": round(b["unrealized_usd"], 2),
                    "prior_usd": round(prior, 2),
                })
                _log.warning(
                    f"UNREALIZED_BLEED {prefix}: "
                    f"Δ={delta:+.2f} (now ${b['unrealized_usd']:.2f}, "
                    f"1h_ago ${prior:.2f})"
                )
                try:
                    from monitor.alerts import send_alert
                    send_alert(
                        subject=f"Unrealized bleed: {prefix}",
                        body=f"{prefix} unrealized PnL dropped "
                             f"${delta:.2f} in last 1h; now ${b['unrealized_usd']:.2f}",
                    )
                except Exception:
                    pass
    finally:
        conn.close()

    return {
        "per_series": per_series,
        "alerts": alerts,
        "n_positions": sum(b["n_open_positions"] for b in per_series.values()),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=settings.DB_PATH)
    p.add_argument("--alert-threshold", type=float, default=None,
                   help="override $/series 1h drop threshold")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-series print")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.alert_threshold is not None:
        settings.UNREALIZED_ALERT_USD = a.alert_threshold

    summary = snapshot(db_path=a.db)
    if not a.quiet:
        print(f"# unrealized_pnl_snapshot — {datetime.now(timezone.utc).isoformat()}")
        if not summary["per_series"]:
            print("# no open positions / no microprice history (likely paper-only)")
        for prefix, b in sorted(summary["per_series"].items(),
                                key=lambda x: x[1]["unrealized_usd"]):
            print(f"  {prefix:18s}  positions={b['n_open_positions']:>3d}  "
                  f"net={b['net_contracts']:+6d}  "
                  f"unrealized=${b['unrealized_usd']:+9.2f}")
        if summary["alerts"]:
            print(f"\n{len(summary['alerts'])} ALERT(S) FIRED — see logs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
