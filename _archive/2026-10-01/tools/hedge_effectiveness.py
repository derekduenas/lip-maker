"""Hedge effectiveness tracker (Track B.5).

Reads `hedge_log` rows (from `cross_venue.hedger`) and `settlement_log`
rows (from `tools.settlement_reconciler`), pairs them by Kalshi ticker,
and computes the basis residual: realized Kalshi PnL + simulated /
actual hedge PnL. If this residual is well-bounded, the hedge math
works and we can flip AUTO_HEDGE_ENABLED.

THE KEY RATIO
-------------
    basis_vol / position_vol = std(basis_residual) / std(kalshi_pnl)

Rule of thumb (from theory research, Hedging on Betting Markets, MDPI
2020): hedge if the ratio is < 0.30. Above that, the hedge instrument
moves too independently of the Kalshi outcome — the hedge introduces
its own risk faster than it removes adverse selection.

LOG-ONLY MODE
-------------
While AUTO_HEDGE_ENABLED is False, hedge PnL is *simulated*: take the
hedge_log row, look up the hedge instrument's price at fill time and
at settlement, compute (entry − settle) × qty as a synthetic hedge
PnL. This lets us validate the math before risking real broker calls.

When live hedging lands (B.3 IBKR adapter), the `hedge_fill_price` and
`hedge_exit_price` columns get populated by execution itself; this tool
will prefer real fills over synthetic when available.

OUTPUT
------
Table view (default) or CSV (--csv) per series with:
  series_prefix | n_fills | kalshi_pnl | hedge_pnl_synth | residual
  | basis_vol | position_vol | ratio | verdict

Verdict:
  ✓ GOOD    ratio < 0.30 — hedge is paying its way
  ⚠ MARGINAL 0.30 ≤ ratio < 0.60
  ✗ BAD     ratio ≥ 0.60 — hedge adds more variance than it removes
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS hedge_residual_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    series_prefix   TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    n_fills         INTEGER NOT NULL,
    kalshi_pnl_usd  REAL,
    hedge_pnl_usd   REAL,
    residual_usd    REAL,
    basis_vol_usd   REAL,
    position_vol_usd REAL,
    ratio           REAL,
    verdict         TEXT,
    computed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hedge_residual_series_time
    ON hedge_residual_log(series_prefix, window_end DESC);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def _hedge_pnl_synth(spot_at_entry: float, spot_at_settle: float,
                     hedge_qty_float: float, contract_size_units: float) -> float:
    """Simulated hedge PnL assuming perfect entry at hedge_log spot price
    and settlement at the hedge instrument's spot at Kalshi settle time.

    PnL_USD = qty × contract_size × (spot_settle − spot_entry)
              (positive qty = long futures; profit when price rises)
    """
    return float(hedge_qty_float) * float(contract_size_units) * (
        float(spot_at_settle) - float(spot_at_entry)
    )


def compute_per_series(db_path: str = settings.DB_PATH,
                       lookback_sec: int = 7 * 86400) -> list[dict]:
    """Aggregate basis residuals by series for the trailing window."""
    import time as _time
    cutoff_iso = datetime.fromtimestamp(
        _time.time() - lookback_sec, tz=timezone.utc
    ).isoformat()

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        # Pull hedge_log entries that would-have-fired and have a paired
        # settlement (so we know the Kalshi outcome). Inner join on ticker.
        # For fills without settlement yet (open positions) skip — we can
        # only score closed cycles.
        try:
            rows = conn.execute(
                """SELECT
                       h.kalshi_ticker, h.kalshi_side, h.kalshi_contracts,
                       h.kalshi_fill_price_c, h.kalshi_strike,
                       h.hedge_instrument, h.hedge_venue, h.spot,
                       h.hours_to_settle, h.hedge_qty_float,
                       s.kalshi_result, s.kalshi_settle_value,
                       s.our_realized_usd, s.rebate_earned_usd, s.close_time,
                       h.created_at
                   FROM hedge_log h
                   JOIN settlement_log s ON s.ticker = h.kalshi_ticker
                   WHERE h.status = 'would_have_fired'
                     AND h.created_at >= ?
                   ORDER BY s.close_time""",
                (cutoff_iso,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            # hedge_log or settlement_log may not exist yet
            _log.info(f"compute_per_series: skipping ({e})")
            return []
    finally:
        conn.close()

    # Look up contract_size from market_match for synthetic hedge PnL
    try:
        from cross_venue.market_match import HEDGE_MAP, series_prefix as _spfx
    except Exception:
        HEDGE_MAP = {}
        def _spfx(t: str) -> str:
            return t.split("-", 1)[0]

    by_series: dict[str, dict] = {}
    for row in rows:
        (ticker, side, signed_qty, fp_c, strike, instrument, venue, spot_entry,
         hrs, hedge_qty_float, k_result, k_settle_value, realized, rebate,
         close_time, hedge_created) = row
        prefix = _spfx(ticker)
        spec = HEDGE_MAP.get(prefix)
        contract_size = spec.contract_size_units if spec else 1.0

        # Synthetic hedge PnL — needs spot at settle. Use k_settle_value
        # when the Kalshi settlement is in the same units as spot (commodity
        # daily / weekly markets). Otherwise (binary outcome only), skip.
        hedge_pnl = None
        if (spot_entry is not None and k_settle_value is not None
                and hedge_qty_float is not None):
            hedge_pnl = _hedge_pnl_synth(
                spot_at_entry=spot_entry, spot_at_settle=k_settle_value,
                hedge_qty_float=hedge_qty_float,
                contract_size_units=contract_size,
            )

        kalshi_pnl = float(realized or 0.0) + float(rebate or 0.0)

        bucket = by_series.setdefault(prefix, {
            "n_fills": 0,
            "kalshi_pnls": [],
            "hedge_pnls": [],
            "residuals": [],
            "instrument": instrument,
            "venue": venue,
            "window_start": close_time,
            "window_end": close_time,
        })
        bucket["n_fills"] += 1
        bucket["kalshi_pnls"].append(kalshi_pnl)
        if hedge_pnl is not None:
            bucket["hedge_pnls"].append(hedge_pnl)
            bucket["residuals"].append(kalshi_pnl + hedge_pnl)
        # extend window
        if close_time < bucket["window_start"]:
            bucket["window_start"] = close_time
        if close_time > bucket["window_end"]:
            bucket["window_end"] = close_time

    out: list[dict] = []
    for prefix, b in by_series.items():
        n = b["n_fills"]
        kalshi_sum = sum(b["kalshi_pnls"])
        hedge_sum = sum(b["hedge_pnls"]) if b["hedge_pnls"] else None
        residual_sum = sum(b["residuals"]) if b["residuals"] else None

        # Vol calculations only sensible with 3+ points
        if len(b["residuals"]) >= 3 and len(b["kalshi_pnls"]) >= 3:
            basis_vol = statistics.pstdev(b["residuals"])
            position_vol = statistics.pstdev(b["kalshi_pnls"])
            ratio = (basis_vol / position_vol) if position_vol > 1e-6 else None
        else:
            basis_vol = None
            position_vol = None
            ratio = None

        if ratio is None:
            verdict = "INSUFFICIENT_DATA"
        elif ratio < 0.30:
            verdict = "GOOD"
        elif ratio < 0.60:
            verdict = "MARGINAL"
        else:
            verdict = "BAD"

        out.append({
            "series": prefix,
            "instrument": b["instrument"],
            "venue": b["venue"],
            "n_fills": n,
            "kalshi_pnl_usd": round(kalshi_sum, 2),
            "hedge_pnl_usd": round(hedge_sum, 2) if hedge_sum is not None else None,
            "residual_usd": round(residual_sum, 2) if residual_sum is not None else None,
            "basis_vol_usd": round(basis_vol, 3) if basis_vol is not None else None,
            "position_vol_usd": round(position_vol, 3) if position_vol is not None else None,
            "ratio": round(ratio, 3) if ratio is not None else None,
            "verdict": verdict,
            "window_start": b["window_start"],
            "window_end": b["window_end"],
        })
    out.sort(key=lambda r: -(r["ratio"] if r["ratio"] is not None else -1))
    return out


def persist_summary(rows: list[dict], db_path: str = settings.DB_PATH) -> None:
    """Write one hedge_residual_log row per series for trend visibility."""
    if not rows:
        return
    ensure_schema(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            for r in rows:
                if r["verdict"] == "INSUFFICIENT_DATA":
                    continue
                conn.execute(
                    """INSERT INTO hedge_residual_log
                       (series_prefix, window_start, window_end, n_fills,
                        kalshi_pnl_usd, hedge_pnl_usd, residual_usd,
                        basis_vol_usd, position_vol_usd, ratio, verdict,
                        computed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["series"], r["window_start"], r["window_end"], r["n_fills"],
                     r["kalshi_pnl_usd"], r["hedge_pnl_usd"], r["residual_usd"],
                     r["basis_vol_usd"], r["position_vol_usd"], r["ratio"],
                     r["verdict"], now_iso),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log.warning(f"persist_summary failed: {e}")


def emit_table(rows: list[dict]) -> None:
    if not rows:
        print("(no paired hedge_log × settlement_log data — paper-only or empty)")
        return
    print(f"{'series':18s} {'instr':6s} {'venue':7s} {'n':>4s} "
          f"{'kalshi$':>9s} {'hedge$':>9s} {'resid$':>9s} "
          f"{'b_vol':>7s} {'p_vol':>7s} {'ratio':>6s} {'verdict':>12s}")
    for r in rows:
        h = f"{r['hedge_pnl_usd']:+.2f}" if r['hedge_pnl_usd'] is not None else "n/a"
        rs = f"{r['residual_usd']:+.2f}" if r['residual_usd'] is not None else "n/a"
        bv = f"{r['basis_vol_usd']:.2f}" if r['basis_vol_usd'] is not None else "n/a"
        pv = f"{r['position_vol_usd']:.2f}" if r['position_vol_usd'] is not None else "n/a"
        rat = f"{r['ratio']:.2f}" if r['ratio'] is not None else "n/a"
        print(f"{r['series']:18s} {(r['instrument'] or '?'):6s} "
              f"{(r['venue'] or '?'):7s} {r['n_fills']:>4d} "
              f"{r['kalshi_pnl_usd']:>+9.2f} {h:>9s} {rs:>9s} "
              f"{bv:>7s} {pv:>7s} {rat:>6s} {r['verdict']:>12s}")


def emit_csv(rows: list[dict]) -> None:
    if not rows:
        return
    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=7,
                   help="lookback window in days (default 7)")
    p.add_argument("--db", default=settings.DB_PATH)
    p.add_argument("--csv", action="store_true")
    p.add_argument("--persist", action="store_true",
                   help="Write summary rows to hedge_residual_log")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    rows = compute_per_series(db_path=a.db, lookback_sec=a.days * 86400)
    print(f"# hedge effectiveness — last {a.days}d "
          f"(generated {datetime.now(timezone.utc).isoformat()})", file=sys.stderr)
    print(f"# verdict: GOOD ratio<0.30, MARGINAL 0.30-0.60, BAD ratio>=0.60",
          file=sys.stderr)
    if a.csv:
        emit_csv(rows)
    else:
        emit_table(rows)
    if a.persist:
        persist_summary(rows, db_path=a.db)
        n_written = sum(1 for r in rows if r["verdict"] != "INSUFFICIENT_DATA")
        print(f"# wrote {n_written} rows to hedge_residual_log", file=sys.stderr)
    return 0 if rows else 2


if __name__ == "__main__":
    sys.exit(main())
