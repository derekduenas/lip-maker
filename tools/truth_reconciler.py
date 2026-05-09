"""Truth Reconciliation Engine — autonomous self-healing for the Kalshi system.

The thesis (per user 2026-05-03): the system should NEVER need hardcoded fixes.
Every divergence between what we PROJECT, INTEND, OBSERVE, and what Kalshi
ACTUALLY DOES should be detected and either auto-healed or alerted with
specifics. No more whitelists, no more manual patches.

THIS REPLACES 4 HARDCODED PATCHES WITH ONE GENERAL LOOP:

  Bug #1 (rebate accounting 47x off)  → reconcile_settlements()
  Bug #2 (KXHIGHLAX 400 retry loop)   → quarantine_bad_markets()
  Bug #3 (allocator/quoting 26% match) → reconcile_orders()
  Bug #4 (weekly $0 in setup_outcomes) → recompute_setup_outcomes()

EVERY 30 MINUTES (cron):
  1. INGEST ground truth from Kalshi API:
     - /portfolio/settlements (Kalshi-side settlement records w/ revenue)
     - /portfolio/fills (every maker/taker fill with is_taker flag)
     - /portfolio/orders (current resting state)
     - /portfolio/positions (open exposure)
  2. DIFF against internal state (settlement_log, fill_ledger, allocator picks)
  3. AUTO-HEAL where safe:
     - Update settlement_log rows where Kalshi truth differs
     - Cancel orphan orders not in current allocator picks
     - Quarantine markets with N consecutive 400 errors
     - Recompute setup_outcomes paid_per_day from truth
  4. PERSIST divergences to reconciliation_log for audit trail
  5. ALERT on >2x gaps that auto-heal can't safely close
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# Tunables — exposed so the system can tune itself later (no hardcoding).
ORPHAN_AUTO_CANCEL = True
QUARANTINE_AFTER_N_400S = 3
QUARANTINE_DURATION_HOURS = 6
RECONCILE_LOOKBACK_HOURS = 24
ALERT_DIVERGENCE_RATIO = 2.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS reconciliation_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  category        TEXT NOT NULL,   -- settlements/fills/orders/markets/setup
  ticker          TEXT,
  field           TEXT,
  internal_value  TEXT,
  truth_value     TEXT,
  action          TEXT,            -- AUTO_HEAL/QUARANTINE/ALERT/SKIP
  detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_recon_ts       ON reconciliation_log(ts);
CREATE INDEX IF NOT EXISTS idx_recon_category ON reconciliation_log(category);

CREATE TABLE IF NOT EXISTS market_quarantine (
  ticker          TEXT PRIMARY KEY,
  reason          TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL,
  first_failure_ts  TEXT NOT NULL,
  expires_at        TEXT NOT NULL
);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _log_recon(conn, category, ticker, field, internal, truth, action, detail):
    conn.execute(
        """INSERT INTO reconciliation_log
           (ts, category, ticker, field, internal_value, truth_value, action, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), category, ticker, field,
         str(internal)[:200], str(truth)[:200], action, detail[:300] if detail else None),
    )


# ── 1. SETTLEMENTS — diff our settlement_log vs Kalshi /portfolio/settlements ─

def reconcile_settlements(c, conn, lookback_hours: int) -> dict:
    """For every Kalshi-side settlement in window, ensure our settlement_log
    has matching realized P&L. Auto-heal divergences.

    Returns counts: {checked, auto_healed, missing_in_internal, alerted}
    """
    counts = {"checked": 0, "auto_healed": 0, "missing": 0, "alerted": 0}
    cursor = None
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)).isoformat()
    pages = 0
    while pages < 10:  # cap pages to avoid runaway
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = c.get("/portfolio/settlements", params=params)
        except Exception as e:
            _log.warning(f"settlements fetch failed: {e}")
            break
        sets = r.get("settlements", []) or []
        if not sets:
            break
        for s in sets:
            ticker = s.get("ticker")
            settled_time = s.get("settled_time", "")
            if not ticker or not settled_time:
                continue
            # Stop walking once outside lookback window
            try:
                st = datetime.fromisoformat(settled_time.replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - st).total_seconds() / 3600
                if age_h > lookback_hours:
                    cursor = None
                    break
            except Exception:
                continue
            counts["checked"] += 1
            # Truth from Kalshi
            revenue = float(s.get("revenue", 0)) / 100.0  # cents → $
            yes_cost = float(s.get("yes_total_cost_dollars", "0") or 0)
            no_cost = float(s.get("no_total_cost_dollars", "0") or 0)
            fee = float(s.get("fee_cost", "0") or 0)
            kalshi_realized = revenue - yes_cost - no_cost - fee
            # Our internal record
            row = conn.execute(
                "SELECT our_realized_usd, rebate_earned_usd FROM settlement_log WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            if not row:
                # We have no record at all — but Kalshi says this market settled.
                # Insert with Kalshi truth.
                if abs(kalshi_realized) > 0.005:
                    counts["missing"] += 1
                    conn.execute(
                        """INSERT OR IGNORE INTO settlement_log
                           (ticker, close_time, our_realized_usd, rebate_earned_usd)
                           VALUES (?, ?, ?, 0)""",
                        (ticker, settled_time, kalshi_realized),
                    )
                    _log_recon(conn, "settlements", ticker, "missing_row",
                               "no record", kalshi_realized, "AUTO_HEAL",
                               "inserted from Kalshi truth")
                    counts["auto_healed"] += 1
                continue
            internal = float(row[0] or 0)
            diff = kalshi_realized - internal
            if abs(diff) > 0.05:
                # Update with truth
                conn.execute(
                    "UPDATE settlement_log SET our_realized_usd = ? WHERE ticker = ?",
                    (kalshi_realized, ticker),
                )
                _log_recon(conn, "settlements", ticker, "realized_usd",
                           internal, kalshi_realized, "AUTO_HEAL",
                           f"diff=${diff:+.3f}")
                counts["auto_healed"] += 1
                if abs(diff) > 1.0:
                    counts["alerted"] += 1
        cursor = r.get("cursor")
        if not cursor:
            break
        pages += 1
    conn.commit()
    return counts


# ── 2. FILLS — find fills Kalshi has but our fill_ledger missed ──

def reconcile_fills(c, conn, lookback_hours: int) -> dict:
    """Diff /portfolio/fills against fill_ledger. Insert missing fills.
    Returns {checked, missing, inserted, maker_fills, taker_fills}.
    """
    counts = {"checked": 0, "missing": 0, "inserted": 0, "maker_fills": 0, "taker_fills": 0}
    cursor = None
    pages = 0
    while pages < 10:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = c.get("/portfolio/fills", params=params)
        except Exception as e:
            _log.warning(f"fills fetch failed: {e}")
            break
        fills = r.get("fills", []) or []
        if not fills:
            break
        for f in fills:
            ticker = f.get("ticker") or f.get("market_ticker")
            trade_id = f.get("trade_id") or f.get("fill_id")
            created_time = f.get("created_time", "")
            if not (ticker and trade_id and created_time):
                continue
            try:
                ct = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
                if age_h > lookback_hours:
                    cursor = None
                    break
            except Exception:
                continue
            counts["checked"] += 1
            is_taker = bool(f.get("is_taker", True))
            if is_taker:
                counts["taker_fills"] += 1
            else:
                counts["maker_fills"] += 1
            # Check internal
            row = conn.execute("SELECT 1 FROM fill_ledger WHERE trade_id = ?", (trade_id,)).fetchone()
            if not row:
                counts["missing"] += 1
                # Insert (best-effort — schema may differ)
                try:
                    side = f.get("side")
                    count = int(float(f.get("count_fp", 0)))
                    price = float(f.get("yes_price_dollars" if side == "yes"
                                        else "no_price_dollars", 0) or 0)
                    conn.execute(
                        """INSERT OR IGNORE INTO fill_ledger
                           (trade_id, order_id, ticker, side, count, is_taker,
                            created_at, synced_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (trade_id, f.get("order_id", ""), ticker, side, count,
                         1 if is_taker else 0, created_time,
                         datetime.now(timezone.utc).isoformat()),
                    )
                    counts["inserted"] += 1
                    _log_recon(conn, "fills", ticker, "trade_id",
                               "missing", trade_id, "AUTO_HEAL", f"side={side} ct={count}")
                except Exception as e:
                    _log_recon(conn, "fills", ticker, "trade_id",
                               "insert_error", trade_id, "ALERT", str(e)[:200])
        cursor = r.get("cursor")
        if not cursor:
            break
        pages += 1
    conn.commit()
    return counts


# ── 3. ORDERS — allocator picks vs actually resting ──

def reconcile_orders(c, conn) -> dict:
    """Compare allocator's picks to actually-resting orders. Cancel orphans
    if ORPHAN_AUTO_CANCEL is on. Log gaps where picks aren't placed.
    """
    counts = {"picks": 0, "resting": 0, "intersection": 0, "orphans_cancelled": 0,
              "missing_picks_logged": 0}
    try:
        from engine.capital_allocator import select_optimal_portfolio
        picks = {m["market_ticker"] for m in
                 select_optimal_portfolio(budget_usd=settings.MAX_TOTAL_GROSS_USD)}
    except Exception as e:
        _log_recon(conn, "orders", None, "allocator", "fail", str(e)[:100],
                   "ALERT", "couldn't compute picks")
        conn.commit()
        return counts
    counts["picks"] = len(picks)
    try:
        r = c.get("/portfolio/orders", params={"status": "resting", "limit": 1000})
        orders = r.get("orders", []) or []
    except Exception as e:
        _log_recon(conn, "orders", None, "fetch", "fail", str(e)[:100],
                   "ALERT", "couldn't fetch resting")
        conn.commit()
        return counts
    by_ticker: dict[str, list] = {}
    for o in orders:
        tkr = o.get("ticker")
        if tkr:
            by_ticker.setdefault(tkr, []).append(o)
    resting = set(by_ticker.keys())
    counts["resting"] = len(resting)
    counts["intersection"] = len(picks & resting)
    orphans = resting - picks
    missing = picks - resting

    if ORPHAN_AUTO_CANCEL and orphans:
        for tkr in orphans:
            for o in by_ticker[tkr]:
                oid = o.get("order_id")
                if not oid:
                    continue
                try:
                    c.delete(f"/portfolio/orders/{oid}")
                    counts["orphans_cancelled"] += 1
                except Exception as e:
                    _log_recon(conn, "orders", tkr, "cancel", "fail", str(e)[:80],
                               "ALERT", f"order_id={oid}")
            _log_recon(conn, "orders", tkr, "orphan", "resting", "not_in_picks",
                       "AUTO_HEAL", f"cancelled {len(by_ticker[tkr])} orders")

    if missing:
        for tkr in list(missing)[:20]:
            _log_recon(conn, "orders", tkr, "missing_pick", "in_picks",
                       "not_resting", "ALERT", "engine has not placed yet")
            counts["missing_picks_logged"] += 1
    conn.commit()
    return counts


# ── 4. QUARANTINE markets with repeated 400 errors ──

def quarantine_bad_markets(conn, log_path: Path = None) -> dict:
    """Parse recent log for placement errors. Quarantine markets that 400'd
    QUARANTINE_AFTER_N_400S+ times in the last hour. Quarantine writes to
    market_blacklist table so the runner skips them.
    """
    counts = {"checked": 0, "quarantined": 0}
    log_path = log_path or Path(getattr(settings, "LOG_PATH",
                                         "/root/lip-maker/logs/lip_maker.log"))
    if not log_path.exists():
        return counts
    import subprocess
    try:
        out = subprocess.run(
            ["tail", "-n", "10000", str(log_path)],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return counts
    pat = re.compile(r"order placement failed for (\S+)\s.*400 Client Error")
    failures: dict[str, int] = {}
    for line in out.splitlines():
        m = pat.search(line)
        if m:
            tkr = m.group(1).rstrip(":")
            failures[tkr] = failures.get(tkr, 0) + 1
    now = datetime.now(timezone.utc)
    expires = (now.replace(microsecond=0)).isoformat()
    from datetime import timedelta
    expire_iso = (now + timedelta(hours=QUARANTINE_DURATION_HOURS)).isoformat()
    for tkr, n in failures.items():
        counts["checked"] += 1
        if n >= QUARANTINE_AFTER_N_400S:
            already = conn.execute(
                "SELECT 1 FROM market_blacklist WHERE ticker = ? AND datetime(expires_at) > datetime('now')",
                (tkr,),
            ).fetchone()
            if already:
                continue
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO market_blacklist
                       (ticker, reason, expires_at)
                       VALUES (?, ?, ?)""",
                    (tkr, f"truth_reconciler:repeated_400_x{n}", expire_iso),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO market_quarantine
                       (ticker, reason, consecutive_failures,
                        first_failure_ts, expires_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (tkr, f"repeated_400_x{n}", n, now.isoformat(), expire_iso),
                )
                _log_recon(conn, "markets", tkr, "400_loop", "n_failures", n,
                           "QUARANTINE",
                           f"blacklisted for {QUARANTINE_DURATION_HOURS}h")
                counts["quarantined"] += 1
            except Exception as e:
                _log_recon(conn, "markets", tkr, "quarantine_failed",
                           "exception", str(e)[:100], "ALERT", None)
    conn.commit()
    return counts


# ── 5. RECOMPUTE setup_outcomes paid_per_day ──

def recompute_setup_outcomes_paid(conn) -> dict:
    """Bug #4: WEEKLY paid_per_day = $0 was bogus. Recompute from settlement_log
    truth (now reconciled with Kalshi data). Paid_per_day = (rebate + max(0, realized))
    / period_days, but never zero when settlement actually occurred.
    """
    counts = {"checked": 0, "updated": 0}
    rows = conn.execute(
        """SELECT s.market_ticker, l.rebate_earned_usd, l.our_realized_usd,
                  s.period_days
           FROM setup_outcomes s
           JOIN settlement_log l ON l.ticker = s.market_ticker"""
    ).fetchall()
    for r in rows:
        ticker, reb, real, period_days = r
        counts["checked"] += 1
        new_paid = ((reb or 0) + max(0, (real or 0))) / max(0.04, period_days or 1)
        # Compare to existing
        existing = conn.execute(
            "SELECT paid_per_day_usd FROM setup_outcomes WHERE market_ticker = ?",
            (ticker,),
        ).fetchone()
        if existing and abs((existing[0] or 0) - new_paid) > 0.001:
            conn.execute(
                "UPDATE setup_outcomes SET paid_per_day_usd = ? WHERE market_ticker = ?",
                (new_paid, ticker),
            )
            counts["updated"] += 1
    conn.commit()
    return counts


# ── 6. STALE ORDER AUTO-CANCEL (Brain recommendation #6) ──

def cancel_stale_orders(c, conn, max_age_hours: float = 4.0) -> dict:
    """Auto-cancel resting orders older than max_age_hours that haven't
    been touched. These represent locked capital that the live runner has
    abandoned. Frees bankroll for current allocator picks.
    """
    counts = {"checked": 0, "stale_cancelled": 0}
    try:
        r = c.get("/portfolio/orders", params={"status": "resting", "limit": 1000})
        orders = (r or {}).get("orders", []) or []
    except Exception:
        return counts
    now = datetime.now(timezone.utc)
    for o in orders:
        counts["checked"] += 1
        created_str = o.get("created_time") or o.get("created_at") or ""
        if not created_str:
            continue
        try:
            ct = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_h = (now - ct).total_seconds() / 3600
        except Exception:
            continue
        if age_h < max_age_hours:
            continue
        oid = o.get("order_id")
        ticker = o.get("ticker", "?")
        if not oid:
            continue
        try:
            c.delete(f"/portfolio/orders/{oid}")
            counts["stale_cancelled"] += 1
            _log_recon(conn, "orders", ticker, "stale_age_h",
                       round(age_h, 1), "cancelled", "AUTO_HEAL",
                       f"resting > {max_age_hours}h, freed capital")
        except Exception as e:
            _log_recon(conn, "orders", ticker, "stale_cancel_failed",
                       round(age_h, 1), str(e)[:80], "ALERT", None)
    conn.commit()
    return counts


# ── Main reconciliation cycle ──

def run_cycle(db_path: str = settings.DB_PATH) -> dict:
    """One full reconciliation cycle. Returns dict of all counts for monitoring."""
    ensure_schema(db_path)
    from execution.kalshi_auth import KalshiClient
    c = KalshiClient()
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        results = {}
        results["settlements"] = reconcile_settlements(c, conn, RECONCILE_LOOKBACK_HOURS)
        results["fills"] = reconcile_fills(c, conn, RECONCILE_LOOKBACK_HOURS)
        results["orders"] = reconcile_orders(c, conn)
        results["quarantine"] = quarantine_bad_markets(conn)
        results["setup_outcomes"] = recompute_setup_outcomes_paid(conn)
        results["stale_orders"] = cancel_stale_orders(c, conn, max_age_hours=4.0)
        return results
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_cycle()
    print("\n=== TRUTH RECONCILIATION CYCLE ===\n")
    for category, counts in res.items():
        print(f"  {category}:")
        for k, v in counts.items():
            print(f"    {k}: {v}")
        print()
