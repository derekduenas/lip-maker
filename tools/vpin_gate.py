"""VPIN Gate — formalized adverse-selection detector (Easley/López de Prado).

For each market with recent fills, compute the volume imbalance:
  VPIN = |buy_volume - sell_volume| / total_volume

Buy volume = fills where we BOUGHT YES (someone hit our YES bid → YES going to us).
Sell volume = fills where we BOUGHT NO (someone hit our NO bid → NO going to us).

When VPIN > VPIN_THRESHOLD on N+ fills, sharp informed flow is hitting one side.
We add the market to market_blacklist for VPIN_BAN_MINUTES so quote_manager
stops posting quotes there. Auto-expires; market re-enters next refresh.

This formalizes our ad-hoc toxicity filter using academic VPIN methodology.
Per agent research (Bürgi/Whelan, Easley/Lopez de Prado): VPIN > 0.65 is the
empirical adverse-flow threshold in maker-side decisions on event markets.

USAGE:
  python tools/vpin_gate.py            # one-shot scan
  Run via systemd timer every 5 min for continuous protection.
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

VPIN_THRESHOLD = 0.65          # |buy_vol - sell_vol| / total > this = toxic
MIN_FILLS_FOR_VPIN = 6         # need at least N fills to compute meaningful VPIN
VPIN_LOOKBACK_MIN = 60         # rolling window for fills
VPIN_BAN_MINUTES = 30          # how long to ban a flagged market
SKIP_BROAD_MARKETS = True      # broad-index markets are less adverse-selected


def _is_broad_market(ticker: str) -> bool:
    """Per Stanford paper: broad-index markets show much less informed price impact
    than single-name. Don't VPIN-blacklist them as readily."""
    broad_prefixes = ("KXMIDTERMMOV", "KXMIDTERM", "KXSENPARL", "KXFETTERMAN",
                      "KXCONGRESS", "KXHOUSERACE", "KXTRUMPACT", "KXMETGALA")
    return ticker.startswith(broad_prefixes)


def scan(db_path: str = settings.DB_PATH) -> dict:
    """Scan recent fills, compute VPIN per market, blacklist toxic ones."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=VPIN_LOOKBACK_MIN)).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=VPIN_BAN_MINUTES)).isoformat()
    conn = sqlite3.connect(db_path, timeout=10.0)
    out = {"scanned": 0, "flagged": 0, "skipped_broad": 0, "already_blocked": 0}
    try:
        # Fill schema: ticker, side (yes/no), count, is_taker, created_at.
        # is_taker=0 → WE were maker; someone hit our quote on `side`.
        # If side=yes && is_taker=0: someone sold us YES (adverse if YES will lose)
        # If side=no && is_taker=0: someone sold us NO (adverse if NO will lose)
        # Imbalance = which side they're stuffing into us.
        rows = conn.execute(
            """SELECT ticker,
                      SUM(CASE WHEN side='yes' AND COALESCE(is_taker,0)=0 THEN count ELSE 0 END) AS yes_to_us,
                      SUM(CASE WHEN side='no'  AND COALESCE(is_taker,0)=0 THEN count ELSE 0 END) AS no_to_us,
                      COUNT(*) AS n_fills
               FROM fill_ledger
               WHERE datetime(created_at) >= datetime(?)
               GROUP BY ticker
               HAVING n_fills >= ?""",
            (cutoff, MIN_FILLS_FOR_VPIN),
        ).fetchall()
        out["scanned"] = len(rows)
        for ticker, yes_buys, no_buys, n_fills in rows:
            yes_buys = yes_buys or 0
            no_buys = no_buys or 0
            total = yes_buys + no_buys
            if total == 0:
                continue
            vpin = abs(yes_buys - no_buys) / total
            if vpin < VPIN_THRESHOLD:
                continue
            if SKIP_BROAD_MARKETS and _is_broad_market(ticker):
                out["skipped_broad"] += 1
                continue
            # When GRADUATED_THROTTLE is on, the graduated layer below
            # handles all VPIN tiers (including the highest as size_scale=0).
            # Skip the legacy binary blacklist write to avoid double-blocking.
            if not getattr(settings, "GRADUATED_THROTTLE", False):
                # Already blocked?
                existing = conn.execute(
                    "SELECT 1 FROM market_blacklist WHERE ticker=? AND datetime(expires_at) > datetime('now')",
                    (ticker,),
                ).fetchone()
                if existing:
                    out["already_blocked"] += 1
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO market_blacklist(ticker, reason, expires_at, added_at) "
                    "VALUES(?, ?, ?, ?)",
                    (ticker, f"vpin={vpin:.2f} (yes_to_us={yes_buys}, no_to_us={no_buys}, n={n_fills})",
                     expires, datetime.now(timezone.utc).isoformat()),
                )
                out["flagged"] += 1
                _log.info(f"VPIN BAN {ticker}  vpin={vpin:.3f}  (y={yes_buys}/n={no_buys})  "
                          f"→ blacklist {VPIN_BAN_MINUTES}min")

        # A.3 (2026-05-14): graduated throttle ladder.
        # In addition to the binary blacklist (legacy, fires only at
        # VPIN ≥ 0.65 + ≥ MIN_FILLS_FOR_VPIN), also write market_throttle
        # rows for VPIN ≥ 0.50 so quote_manager can apply graduated
        # size_scale + tick_offset. Active only when GRADUATED_THROTTLE
        # feature flag is on (else dormant data).
        try:
            from monitor.market_throttle import vpin_tier, upsert as _mt_upsert
        except Exception:
            vpin_tier = None
        if vpin_tier is not None:
            # Re-iterate the same rows but with the lower (0.50) cutoff
            graduated_n = 0
            for ticker, yes_buys, no_buys, n_fills in rows:
                yes_buys = yes_buys or 0
                no_buys = no_buys or 0
                total = yes_buys + no_buys
                if total == 0 or n_fills < MIN_FILLS_FOR_VPIN:
                    continue
                vpin = abs(yes_buys - no_buys) / total
                if SKIP_BROAD_MARKETS and _is_broad_market(ticker):
                    continue
                tier = vpin_tier(vpin)
                if tier is None:
                    continue
                ss, off, detail = tier
                _mt_upsert(
                    ticker, size_scale=ss, tick_offset=off,
                    source="vpin", ttl_sec=VPIN_BAN_MINUTES * 60,
                    detail=f"{detail} y={yes_buys} n={no_buys} f={n_fills}",
                    db_path=db_path,
                )
                graduated_n += 1
            out["graduated_throttled"] = graduated_n
        conn.commit()
    finally:
        conn.close()
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--threshold", type=float, default=VPIN_THRESHOLD)
    a = p.parse_args()
    if a.threshold != VPIN_THRESHOLD:
        VPIN_THRESHOLD = a.threshold
    res = scan()
    print(f"vpin_gate: scanned={res['scanned']} flagged={res['flagged']} "
          f"skipped_broad={res['skipped_broad']} already_blocked={res['already_blocked']}")
