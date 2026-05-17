"""Order flow direction tracker — proactive toxicity detection (#105).

THE PROBLEM:
  Existing toxicity_filter (#88/#89) uses a 24-hour window with 6+ fills
  needed to fire. By the time it triggers we've already eaten the loss.

THIS TOOL:
  Watches FAST directional flow in 60-second rolling windows. If ≥3
  consecutive maker fills come in on the same side within 60s, that's
  someone aggressively hitting us with informed flow → blacklist for 2
  hours so we don't keep getting picked off.

DIFFERENCE FROM #88/#89:
  - #88/#89: 24h window, 6+ fills, 95% imbalance. Captures PERSISTENT
    adverse markets (recurring bleed pattern).
  - #105: 60s window, 3+ fills, 100% same-side. Catches FAST FLOW the
    moment it starts, before the second/third hit lands.

GUARDS:
  - Only counts MAKER fills (is_taker=0). Our fills should always be
    maker since we're posting resting LIP quotes.
  - Skips markets that already have a recent #105 blacklist entry
    (dedup within 30 min) to avoid log spam.
  - Skips if already blacklisted by another source.
  - Short blacklist (BLACKLIST_HOURS=2) so a market that re-balances
    can come back fast — this is preventive, not punitive.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────
WINDOW_SEC          = 60       # rolling window for directional flow
MIN_FILLS           = 3        # need this many same-side fills to fire
BLACKLIST_HOURS     = 2        # short — preventive not punitive
DEDUP_MIN           = 30       # skip if already-flagged in last 30 min


SCHEMA = """
CREATE TABLE IF NOT EXISTS order_flow_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    side            TEXT NOT NULL,    -- 'yes' or 'no'
    n_fills         INTEGER NOT NULL,
    n_contracts     INTEGER NOT NULL,
    window_sec      INTEGER NOT NULL,
    action          TEXT NOT NULL,    -- 'blacklisted' | 'skipped:<reason>'
    UNIQUE(ticker, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_orderflow_ticker ON order_flow_log(ticker);
CREATE INDEX IF NOT EXISTS idx_orderflow_time   ON order_flow_log(detected_at);
"""


def scan(db_path: str = settings.DB_PATH) -> dict:
    # Ensure schemas exist
    from execution.market_blacklist import init_schema as _init_bl
    _init_bl(db_path)

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.executescript(SCHEMA)
    conn.commit()

    now = datetime.now(timezone.utc)
    # AUDIT FIX: normalize to Z suffix to match Kalshi created_at format
    # ('YYYY-MM-DDTHH:MM:SS.fffZ'). Otherwise lexical comparison of '+00:00'
    # vs 'Z' could fail at boundary moments (Z=0x5A > +=0x2B).
    cutoff = (now - timedelta(seconds=WINDOW_SEC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dedup_cutoff = (now - timedelta(minutes=DEDUP_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Group fills in last 60s by ticker, count per side. We require maker
    # fills (is_taker=0) since we're a market maker — taker fills shouldn't
    # exist for us. Defensive filter against NULL.
    rows = conn.execute(
        """SELECT ticker,
                  SUM(CASE WHEN side='yes' THEN 1 ELSE 0 END) AS yes_n,
                  SUM(CASE WHEN side='no'  THEN 1 ELSE 0 END) AS no_n,
                  SUM(CASE WHEN side='yes' THEN count ELSE 0 END) AS yes_c,
                  SUM(CASE WHEN side='no'  THEN count ELSE 0 END) AS no_c
           FROM fill_ledger
           WHERE created_at > ?
             AND COALESCE(is_taker, 0) = 0
           GROUP BY ticker""",
        (cutoff,),
    ).fetchall()

    findings = []
    for ticker, yes_n, no_n, yes_c, no_c in rows:
        yes_n, no_n = int(yes_n or 0), int(no_n or 0)
        total_n = yes_n + no_n
        if total_n < MIN_FILLS:
            continue
        # Require 100% same-side (proactive — even one opposite fill = mixed)
        if yes_n == total_n:
            side = "yes"
            n_contracts = int(yes_c or 0)
        elif no_n == total_n:
            side = "no"
            n_contracts = int(no_c or 0)
        else:
            continue  # mixed flow — not a signal

        # Dedup: skip if we've already logged a #105 entry for this ticker recently
        recent = conn.execute(
            """SELECT 1 FROM order_flow_log
               WHERE ticker = ? AND datetime(detected_at) > datetime(?)""",
            (ticker, dedup_cutoff),
        ).fetchone()
        if recent:
            continue

        # Skip if already blacklisted by some other source
        existing_bl = conn.execute(
            """SELECT reason FROM market_blacklist
               WHERE ticker = ? AND datetime(expires_at) > datetime('now')""",
            (ticker,),
        ).fetchone()
        if existing_bl:
            action = f"skipped:already_blacklisted ({existing_bl[0][:30]})"
        else:
            # When GRADUATED_THROTTLE is on, skip the binary blacklist
            # write — graduated companion below handles severity tiers
            # (≥5 fills → size_scale=0.10, near-full block). When off,
            # retain legacy binary behavior.
            if not getattr(settings, "GRADUATED_THROTTLE", False):
                expires = (now + timedelta(hours=BLACKLIST_HOURS)).isoformat()
                conn.execute(
                    """INSERT INTO market_blacklist
                       (ticker, expires_at, reason, added_at)
                       VALUES (?, ?, ?, ?)""",
                    (ticker, expires,
                     f"orderflow:{side}_side_{total_n}fills_{n_contracts}c_in_{WINDOW_SEC}s",
                     now.isoformat()),
                )
                action = "blacklisted"
                _log.warning(f"orderflow {ticker}: {total_n}x {side.upper()} fills "
                             f"({n_contracts} contracts) in last {WINDOW_SEC}s "
                             f"→ blacklisted {BLACKLIST_HOURS}h")
            else:
                action = "graduated_throttle"
                _log.info(f"orderflow {ticker}: {total_n}x {side.upper()} fills "
                          f"({n_contracts}c/{WINDOW_SEC}s) → graduated throttle")

            # A.3 graduated companion: same-side sequential fills are the
            # cleanest one-sided-toxicity signal we have. Apply a per-side
            # bias by tilting size_scale + tick_offset asymmetrically. For
            # consistency with vpin/toxicity we still write a single
            # market_throttle row; the per-side tilt is left to caller
            # (quote_manager applies tick_offset to BOTH sides for now,
            # B.6 will refine to per-side asymmetric pull).
            try:
                from monitor.market_throttle import upsert as _mt_upsert
                # 3-fills 60s baseline → mid-tier throttle; >5 fills → near-full
                if total_n >= 5:
                    ss, toff = 0.10, 2
                else:
                    ss, toff = 0.30, 1
                _mt_upsert(
                    ticker, size_scale=ss, tick_offset=toff,
                    source="order_flow",
                    ttl_sec=BLACKLIST_HOURS * 3600,
                    detail=f"{side}_side n={total_n} c={n_contracts} "
                           f"window={WINDOW_SEC}s",
                    db_path=db_path,
                )
            except Exception as _e:
                _log.debug(f"order_flow graduated upsert failed for {ticker}: {_e}")

        conn.execute(
            """INSERT OR IGNORE INTO order_flow_log
               (ticker, detected_at, side, n_fills, n_contracts, window_sec, action)
               VALUES (?,?,?,?,?,?,?)""",
            (ticker, now.isoformat(), side, total_n, n_contracts, WINDOW_SEC, action),
        )
        findings.append({
            "ticker": ticker, "side": side,
            "n_fills": total_n, "n_contracts": n_contracts,
            "action": action,
        })

    conn.commit()

    # Summary stats
    last_24h = conn.execute(
        """SELECT COUNT(DISTINCT ticker) FROM order_flow_log
           WHERE datetime(detected_at) > datetime('now', '-24 hour')
             AND action = 'blacklisted'"""
    ).fetchone()[0]

    conn.close()
    return {
        "candidates_scanned": len(rows),
        "new_findings":       len(findings),
        "findings":           findings,
        "last_24h_blocks":    last_24h,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    a = p.parse_args()
    r = scan()
    print(f"\n══ ORDER FLOW DIRECTION SCAN ══")
    print(f"  candidates scanned:   {r['candidates_scanned']}")
    print(f"  new flags this cycle: {r['new_findings']}")
    print(f"  blocked last 24h:     {r['last_24h_blocks']}")
    if r["findings"]:
        print(f"\n  ⚡ Markets flagged:")
        for f in r["findings"]:
            print(f"    {f['ticker']:<40s}  {f['side'].upper()}: {f['n_fills']} fills / "
                  f"{f['n_contracts']} contracts → {f['action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
