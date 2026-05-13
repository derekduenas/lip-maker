"""Per-series auto-prune for proven losers.

Cron daily. Scans settlement_log over last 7d, identifies series net-negative
beyond a threshold, and adds them to market_blacklist for 7 days. The
existing 10s blacklist recheck in quote_manager will cancel orders within
one heartbeat.

PHILOSOPHY:
  Bleed monitor's MTM check catches OPEN-position drift (mid-cycle).
  This catches CLOSED-cycle attribution: series whose ENTIRE 7d realized
  P&L is negative beyond noise. They're paying us less rebate than they
  cost in adverse selection. Prune.

USAGE:
  python series_auto_prune.py              # check + ban
  python series_auto_prune.py --dry-run    # report only
  python series_auto_prune.py --json
"""
from __future__ import annotations


# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "series_auto_prune")
except Exception:
    pass

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# Tunables — per-ticker pruner (existing)
LOOKBACK_DAYS    = 7
NET_THRESHOLD    = -5.0   # series with 7d net < -$5 = auto-ban
MIN_SETTLEMENTS  = 3      # need at least N settlements for confidence
BAN_HOURS        = 168    # 7-day ban (re-evaluated next prune cycle)

# Tunables — series-level escalator (2026-05-13).
# A series is a SERIES_BLOCKLIST CANDIDATE when N+ tickers in the same
# series have been auto-banned (any source: dead_slot, toxicity,
# series_auto_prune) within ESC_WINDOW_DAYS AND the series's window-net
# realized PnL is below ESC_NET_PNL_THRESHOLD AND rebate didn't cover
# the inventory cost. Found this gap 2026-05-13: KXTRUEV bled -$229
# while per-ticker bans churned (each new daily ticker started clean).
ESC_WINDOW_DAYS              = 14
ESC_MIN_DISTINCT_TICKERS     = 3
ESC_NET_PNL_THRESHOLD        = -50.0   # ABS larger than per-ticker prune; only escalate big leaks


# ── Series-level escalator (2026-05-13) ──────────────────────────────────
ESCALATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS series_blocklist_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    series          TEXT NOT NULL,
    detected_day    DATE NOT NULL,
    detected_at     TEXT NOT NULL,
    n_distinct_tickers INTEGER NOT NULL,
    net_pnl_usd     REAL NOT NULL,
    rebate_usd      REAL NOT NULL,
    realized_loss_usd REAL NOT NULL,
    tickers_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'UNREVIEWED',
    UNIQUE(series, detected_day)
);
CREATE INDEX IF NOT EXISTS idx_blocklist_cand_status
    ON series_blocklist_candidates(status);

CREATE TABLE IF NOT EXISTS series_blocklist_overlay (
    series          TEXT PRIMARY KEY,
    added_at        TEXT NOT NULL,
    source          TEXT NOT NULL,
    reason          TEXT NOT NULL
);
"""

ALERTS_LOG_PATH = "/root/lip-maker/logs/alerts.log"


def find_blocklist_candidates(
    conn: sqlite3.Connection, *,
    window_days:           int   = ESC_WINDOW_DAYS,
    min_distinct_tickers:  int   = ESC_MIN_DISTINCT_TICKERS,
    max_net_pnl_usd:       float = ESC_NET_PNL_THRESHOLD,
) -> list[dict]:
    """Identify series whose pattern matches a SERIES_BLOCKLIST escalation:
      (1) >= min_distinct_tickers tickers banned in market_blacklist (ANY
          source — dead_slot, toxicity, series_auto_prune) within window
      (2) settlement_log net (realized + rebate) over window < max_net_pnl_usd
      (3) rebate captured < |realized_loss| (rebate didn't cover inventory)

    Returns list of dicts; deterministic ordering (most-loss first).
    """
    # (1) Per-series ban counts from market_blacklist
    ban_rows = conn.execute(
        f"""SELECT substr(ticker, 1, instr(ticker||'-','-')-1) AS series,
                   COUNT(DISTINCT ticker) AS n_banned,
                   GROUP_CONCAT(DISTINCT ticker) AS ticker_list
            FROM market_blacklist
            WHERE added_at >= datetime('now', '-{int(window_days)} days')
            GROUP BY series
            HAVING n_banned >= {int(min_distinct_tickers)}"""
    ).fetchall()
    if not ban_rows:
        return []

    candidates: list[dict] = []
    for series, n_banned, ticker_list in ban_rows:
        if not series:
            continue
        # (2) + (3): pull all settlements across the series in the window
        agg = conn.execute(
            f"""SELECT COALESCE(SUM(rebate_earned_usd), 0.0) AS rebate,
                       COALESCE(SUM(our_realized_usd),  0.0) AS realized,
                       COUNT(*) AS n_settles
                FROM settlement_log
                WHERE close_time >= datetime('now', '-{int(window_days)} days')
                  AND substr(ticker, 1, instr(ticker||'-','-')-1) = ?""",
            (series,),
        ).fetchone()
        rebate, realized, n_settles = agg
        net = float(rebate or 0) + float(realized or 0)
        # Condition (2): net loss exceeds threshold
        if net >= max_net_pnl_usd:
            continue
        # Condition (3): rebate didn't cover the inventory cost (loss > rebate)
        # If realized >= 0 (somehow), rebate trivially covers — skip.
        if realized >= 0:
            continue
        if abs(realized) <= float(rebate or 0):
            continue
        candidates.append({
            "series":              series,
            "n_distinct_tickers":  int(n_banned),
            "n_settles":           int(n_settles or 0),
            "net_pnl_usd":         round(net, 2),
            "rebate_usd":          round(float(rebate or 0), 2),
            "realized_loss_usd":   round(float(realized), 2),
            "tickers":             [t for t in (ticker_list or "").split(",") if t][:20],
        })
    candidates.sort(key=lambda c: c["net_pnl_usd"])
    return candidates


def _persist_candidate_and_alert(
    conn: sqlite3.Connection, c: dict, *, dry_run: bool,
) -> tuple[bool, bool]:
    """Insert into series_blocklist_candidates (idempotent on (series, day))
    and append to alerts.log on first detection of the day. Returns
    (was_new_candidate, alert_emitted)."""
    detected_day = datetime.now(timezone.utc).date().isoformat()
    detected_at  = datetime.now(timezone.utc).isoformat()
    if dry_run:
        return (False, False)
    try:
        conn.execute(
            """INSERT INTO series_blocklist_candidates
                 (series, detected_day, detected_at, n_distinct_tickers,
                  net_pnl_usd, rebate_usd, realized_loss_usd, tickers_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (c["series"], detected_day, detected_at,
             c["n_distinct_tickers"], c["net_pnl_usd"],
             c["rebate_usd"], c["realized_loss_usd"],
             json.dumps(c["tickers"])),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return (False, False)   # already detected today (idempotent)

    line = (f"{detected_at}  CRITICAL  SERIES BLOCKLIST CANDIDATE: "
            f"{c['series']} | n_banned={c['n_distinct_tickers']} in "
            f"{ESC_WINDOW_DAYS}d | net_pnl=${c['net_pnl_usd']:+.2f} | "
            f"rebate=${c['rebate_usd']:+.2f} | "
            f"realized=${c['realized_loss_usd']:+.2f} | "
            f"tickers={c['tickers'][:5]}\n")
    try:
        with open(ALERTS_LOG_PATH, "a") as f:
            f.write(line)
    except Exception as e:
        _log.warning(f"alert log write failed: {e}")
    return (True, True)


def _maybe_auto_block(conn: sqlite3.Connection, c: dict) -> bool:
    """If SERIES_AUTO_BLOCKLIST_ENABLED is True, insert into the runtime
    overlay table (which engine/lip_discovery.py reads alongside the
    static SERIES_BLOCKLIST). Returns True if added."""
    if not getattr(settings, "SERIES_AUTO_BLOCKLIST_ENABLED", False):
        return False
    try:
        conn.execute(
            """INSERT OR IGNORE INTO series_blocklist_overlay
                 (series, added_at, source, reason)
               VALUES (?, ?, ?, ?)""",
            (c["series"],
             datetime.now(timezone.utc).isoformat(),
             "auto_escalator",
             f"net_pnl=${c['net_pnl_usd']:+.2f} over {ESC_WINDOW_DAYS}d "
             f"({c['n_distinct_tickers']} tickers banned, "
             f"realized=${c['realized_loss_usd']:+.2f} > rebate=${c['rebate_usd']:+.2f})"),
        )
        conn.commit()
        return True
    except Exception as e:
        _log.warning(f"auto_block insert failed for {c['series']}: {e}")
        return False


def escalate_candidates(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Run the escalator. Mutates DB unless dry_run. Returns summary."""
    conn.executescript(ESCALATOR_SCHEMA)
    cands = find_blocklist_candidates(conn)
    new_alerts = 0
    auto_blocked: list[str] = []
    for c in cands:
        is_new, _ = _persist_candidate_and_alert(conn, c, dry_run=dry_run)
        if is_new:
            new_alerts += 1
            if not dry_run and _maybe_auto_block(conn, c):
                auto_blocked.append(c["series"])
    return {
        "candidates":   cands,
        "new_alerts":   new_alerts,
        "auto_blocked": auto_blocked,
        "auto_block_enabled": bool(
            getattr(settings, "SERIES_AUTO_BLOCKLIST_ENABLED", False)
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out: dict = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "dry_run":     a.dry_run,
        "lookback_d":  LOOKBACK_DAYS,
        "threshold":   NET_THRESHOLD,
        "candidates":  [],
        "banned":      [],
        "skipped":     [],
    }

    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        # Find loser series
        rows = conn.execute(f"""
            SELECT substr(ticker, 1, instr(ticker||'-','-')-1) AS series,
                   COUNT(*) AS n,
                   ROUND(SUM(rebate_earned_usd) + SUM(our_realized_usd), 2) AS net
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-{LOOKBACK_DAYS} days')
            GROUP BY series
            HAVING n >= {MIN_SETTLEMENTS} AND net < {NET_THRESHOLD}
            ORDER BY net
        """).fetchall()

        # Already in hard blocklist? skip
        hard_block = settings.SERIES_BLOCKLIST

        # Already in runtime blacklist (some ticker)? skip
        runtime_blocked = set()
        try:
            for r in conn.execute(
                "SELECT DISTINCT substr(ticker,1,instr(ticker||'-','-')-1) FROM market_blacklist "
                "WHERE expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            ).fetchall():
                if r[0]:
                    runtime_blocked.add(r[0])
        except Exception:
            pass

        expires_at = (datetime.now(timezone.utc) +
                      timedelta(hours=BAN_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        added_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for series, n, net in rows:
            cand = {"series": series, "n": n, "net": net}
            out["candidates"].append(cand)

            if any(series.startswith(b) for b in hard_block):
                out["skipped"].append({**cand, "reason": "in_hard_blocklist"})
                continue
            if series in runtime_blocked:
                out["skipped"].append({**cand, "reason": "already_runtime_banned"})
                continue

            # Find all open tickers in this series to ban
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT market_ticker FROM lip_programs "
                "WHERE substr(market_ticker,1,instr(market_ticker||'-','-')-1) = ? "
                "AND datetime(end_date) > datetime('now')",
                (series,),
            ).fetchall()]

            if not a.dry_run:
                reason = (f"series_auto_prune: {series} 7d net=${net:.2f} "
                          f"({n} settlements, threshold ${NET_THRESHOLD})")
                for t in tickers:
                    conn.execute(
                        "INSERT OR REPLACE INTO market_blacklist "
                        "(ticker, expires_at, reason, added_at) VALUES (?, ?, ?, ?)",
                        (t, expires_at, reason, added_at),
                    )
                conn.commit()
            out["banned"].append({**cand, "tickers_banned": len(tickers)})

        # ── Series-level escalator (additive, separate signal) ──
        esc = escalate_candidates(conn, dry_run=a.dry_run)
        out["escalator"] = esc

    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"\n━━━ SERIES AUTO-PRUNE — "
              f"{'DRY' if a.dry_run else 'LIVE'} ━━━")
        print(f"  Lookback: {LOOKBACK_DAYS}d  threshold: net<${NET_THRESHOLD:.2f}  "
              f"min_settles: {MIN_SETTLEMENTS}")
        if not out["candidates"]:
            print(f"  No losing series found 🎉")
        else:
            print(f"\n  CANDIDATES ({len(out['candidates'])}):")
            for c in out["candidates"]:
                print(f"    {c['series']:<25}  n={c['n']:>3}  net=${c['net']:>+7.2f}")
            if out["banned"]:
                print(f"\n  BANNED ({len(out['banned'])}):")
                for b in out["banned"]:
                    print(f"    🚨 {b['series']:<25}  {b['tickers_banned']} tickers blacklisted {BAN_HOURS}h")
            if out["skipped"]:
                print(f"\n  SKIPPED ({len(out['skipped'])}):")
                for s in out["skipped"]:
                    print(f"    {s['series']:<25}  reason={s['reason']}")

        # Escalator block — clearly distinguished from per-ticker prune
        esc = out.get("escalator", {})
        cands = esc.get("candidates", [])
        ab = esc.get("auto_block_enabled", False)
        print(f"\n  ━━━ SERIES ESCALATOR  (window={ESC_WINDOW_DAYS}d, "
              f"min_tickers={ESC_MIN_DISTINCT_TICKERS}, "
              f"net<${ESC_NET_PNL_THRESHOLD:.0f}) "
              f"AUTO_BLOCK={'ON' if ab else 'OFF'} ━━━")
        if not cands:
            print(f"  No SERIES_BLOCKLIST candidates 🎉")
        else:
            print(f"\n  CANDIDATES ({len(cands)}, {esc.get('new_alerts',0)} new alerts):")
            for c in cands:
                print(f"    🚨 {c['series']:<25}  "
                      f"banned_tickers={c['n_distinct_tickers']:>2}  "
                      f"net=${c['net_pnl_usd']:>+8.2f}  "
                      f"realized=${c['realized_loss_usd']:>+8.2f}  "
                      f"rebate=${c['rebate_usd']:>+6.2f}")
            if esc.get("auto_blocked"):
                print(f"\n  AUTO-BLOCKED ({len(esc['auto_blocked'])}): "
                      f"{', '.join(esc['auto_blocked'])}")
            elif not ab:
                print(f"  (operator review required — add to SERIES_BLOCKLIST "
                      f"in config/settings.py manually, OR flip "
                      f"SERIES_AUTO_BLOCKLIST_ENABLED=True)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
