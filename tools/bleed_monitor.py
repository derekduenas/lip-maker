"""Autonomous bleed monitor — Kalshi side.

Detects per-series net bleed in a recent window; auto-adds losing series
to runtime market_blacklist (TTL 6h). Live engine already consults
market_blacklist on every quote attempt — no service restart needed.

LOGIC:
  Every run (cron every 10 min):
    1. For each series with settlements in last LOOKBACK_HOURS:
        net = sum(our_realized_usd + rebate_earned_usd)
        loss_only = sum(our_realized_usd) where our_realized_usd < 0
    2. If net < -BLEED_THRESHOLD_USD AND rebate < 50% of |loss_only|:
        → blacklist all open tickers in that series for AUTO_BAN_HOURS
        → log diagnosis (series, n_settlements, net, rebate offset, top losing tickers)

GUARDS:
  - Min N settlements in window (avoid single-print false positives)
  - Skip series already in SERIES_BLOCKLIST (already permanently banned)
  - 12h cooldown on auto-bans for same series (don't oscillate)

REPORT (logged to bleed_monitor.log):
  ✅ no bleed detected — N series checked
  🚨 BLEED: KXFOOWEEKLY net=$-X (loss=$-Y, rebate=$+Z), banning N tickers 6h

USAGE:
  python bleed_monitor_kalshi.py             # check + act
  python bleed_monitor_kalshi.py --dry-run   # diagnose, no action
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

LIP_ROOT = Path("/root/lip-maker")
sys.path.insert(0, str(LIP_ROOT))

from config import settings  # type: ignore  (run on box)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_log = logging.getLogger("bleed_monitor")

LOOKBACK_HOURS       = 4         # window for per-series net calc
MIN_SETTLEMENTS      = 4         # need at least N settlements to consider
BLEED_THRESHOLD_USD  = 5.0       # net loss to trigger
REBATE_OFFSET_RATIO  = 0.50      # rebate < 50% of |loss| → ban
AUTO_BAN_HOURS       = 6         # blacklist TTL
COOLDOWN_HOURS       = 12        # don't re-ban same series within window

DB = settings.SQLITE_DB if hasattr(settings, "SQLITE_DB") else str(LIP_ROOT / "data" / "lip_maker.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ban_open_tickers(conn: sqlite3.Connection, series_prefix: str,
                      ban_hours: int, reason: str) -> list[str]:
    """Find currently-traded tickers in series, insert into market_blacklist."""
    expires = (datetime.now(timezone.utc) + timedelta(hours=ban_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # Anything we've filled in last 7 days OR resting orders = candidate
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM fill_ledger
           WHERE ticker LIKE ? AND datetime(created_at) > datetime('now','-7 days')""",
        (f"{series_prefix}%",),
    ).fetchall()
    tickers = [r[0] for r in rows]
    now_iso = _now_iso()
    for tkr in tickers:
        conn.execute(
            """INSERT OR REPLACE INTO market_blacklist
               (ticker, expires_at, reason, added_at) VALUES (?,?,?,?)""",
            (tkr, expires, reason, now_iso),
        )
    return tickers


def _recently_banned(conn: sqlite3.Connection, series_prefix: str,
                     cooldown_h: int) -> bool:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=cooldown_h)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        """SELECT 1 FROM market_blacklist
           WHERE ticker LIKE ? AND added_at > ? AND reason LIKE 'bleed_monitor%'
           LIMIT 1""",
        (f"{series_prefix}%", cutoff),
    ).fetchone()
    return row is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    blocklist = getattr(settings, "SERIES_BLOCKLIST", set())

    conn = sqlite3.connect(DB, timeout=10.0)
    try:
        # Per-series bleed in window
        rows = conn.execute(
            f"""SELECT series_prefix,
                       COUNT(*)                               AS n,
                       COALESCE(SUM(our_realized_usd), 0)     AS realized,
                       COALESCE(SUM(rebate_earned_usd), 0)    AS rebate,
                       COALESCE(SUM(net_outcome_usd), 0)      AS net,
                       COALESCE(SUM(CASE WHEN our_realized_usd < 0
                                         THEN our_realized_usd ELSE 0 END), 0) AS loss_only
                FROM settlement_log
                WHERE datetime(close_time) > datetime('now','-{LOOKBACK_HOURS} hours')
                GROUP BY series_prefix
                HAVING n >= {MIN_SETTLEMENTS}
                ORDER BY net ASC""",
        ).fetchall()

        actions = 0
        for series, n, realized, rebate, net, loss_only in rows:
            if series in blocklist:
                continue
            if net >= -BLEED_THRESHOLD_USD:
                continue
            # Loss is real and rebate isn't covering enough
            offset = (rebate / abs(loss_only)) if loss_only < 0 else 1.0
            if offset >= REBATE_OFFSET_RATIO:
                _log.info(f"  {series}: net=${net:.2f} but rebate covers "
                          f"{offset*100:.0f}% of loss — leaving alone")
                continue
            if _recently_banned(conn, series, COOLDOWN_HOURS):
                _log.info(f"  {series}: in cooldown, skipping")
                continue

            reason = (f"bleed_monitor: series net=${net:.2f} over {LOOKBACK_HOURS}h "
                      f"({n} settlements, realized=${realized:.2f}, "
                      f"rebate=${rebate:.2f}, offset={offset*100:.0f}%)")
            if args.dry_run:
                _log.warning(f"🚨 [DRY] would ban {series}: {reason}")
            else:
                tickers = _ban_open_tickers(conn, series, AUTO_BAN_HOURS, reason)
                conn.commit()
                _log.warning(f"🚨 BANNED {series} for {AUTO_BAN_HOURS}h "
                             f"({len(tickers)} tickers): {reason}")
                actions += 1

        if actions == 0:
            _log.info(f"✅ no bleed: {len(rows)} series checked, none crossed threshold")
        else:
            _log.warning(f"🚨 {actions} series auto-banned this run")

        # ── MTM bleed check (open positions, no settlement required) ──
        # Loss ratio: (cost_basis - market_exposure) / cost_basis. If a
        # series has aggregate MTM loss > 30% of cost basis AND > $10
        # absolute, ban its open tickers for 6h.
        try:
            from execution.kalshi_auth import KalshiClient
            kc = KalshiClient()
            r = kc.get('/portfolio/positions', params={'limit':200})
            positions = r.get('market_positions', [])
            # Aggregate per series
            from collections import defaultdict
            agg = defaultdict(lambda: {"cost": 0.0, "exposure": 0.0, "n": 0})
            for p in positions:
                fp = float(p.get('position_fp', 0))
                if fp == 0:
                    continue
                tkr = p.get('ticker', '')
                # Series prefix = everything before the first dash-digit
                import re
                m = re.match(r'^(KX[A-Z]+)', tkr)
                if not m:
                    continue
                series = m.group(1)
                cost = float(p.get('total_traded_dollars', 0)) - float(p.get('realized_pnl_dollars', 0))
                exp  = float(p.get('market_exposure_dollars', 0))
                if cost <= 0:
                    continue
                agg[series]["cost"] += cost
                agg[series]["exposure"] += exp
                agg[series]["n"] += 1
            mtm_actions = 0
            for series, d in agg.items():
                if d["cost"] < 10:
                    continue
                loss = d["cost"] - d["exposure"]
                loss_ratio = loss / d["cost"]
                if loss_ratio < 0.30 or loss < 10:
                    continue
                if series in blocklist:
                    continue
                if _recently_banned(conn, series, COOLDOWN_HOURS):
                    _log.info(f"  MTM {series}: in cooldown")
                    continue
                reason = (f"mtm_bleed: {series} aggregate cost=${d['cost']:.2f} "
                          f"exposure=${d['exposure']:.2f} loss=${loss:.2f} "
                          f"({loss_ratio*100:.0f}%, n={d['n']} positions)")
                if args.dry_run:
                    _log.warning(f"🚨 [DRY-MTM] would ban {series}: {reason}")
                else:
                    tickers = _ban_open_tickers(conn, series, AUTO_BAN_HOURS, reason)
                    conn.commit()
                    _log.warning(f"🚨 MTM-BANNED {series} for {AUTO_BAN_HOURS}h "
                                 f"({len(tickers)} tickers): {reason}")
                    mtm_actions += 1
            if mtm_actions:
                _log.warning(f"🚨 {mtm_actions} series auto-banned via MTM check")
        except Exception as e:
            _log.warning(f"MTM check failed: {e}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
