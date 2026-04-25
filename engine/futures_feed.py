"""Futures reference-price feed — Yahoo Finance v8 API (free, no auth).

Maps Kalshi commodity tickers → CME/NYMEX/ICE futures and fetches live prices.
Reference prices feed into the quote loop as "fair value anchor" — if we can
see that Brent crude futures are at $96.50, we know the Kalshi T95 strike should
settle YES and the T97 strike should settle NO.

Runs every 60 sec via systemd timer. Writes futures_prices table.

We are NOT using this to TRADE. We are using this to:
  1. See implied fair on our Kalshi strikes
  2. Detect when our quotes are stale vs underlying moves
  3. Log direction/size signals for future alpha layers
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)


# ── Kalshi prefix → Yahoo Finance futures symbol ──────────────────────
#
# Verified 2026-04-21 against Kalshi's market `rules_primary` field:
#   - Kalshi commodity settlement EXPLICITLY NAMES a contract month for
#     Brent, NatGas, Copper, Corn, Wheat, Soybeans. We MUST use that
#     specific contract, not the Yahoo =F front-month, because they
#     can diverge materially (Brent =F was $90.70 while BRENTM6 = $95.05,
#     a $4.35 delta that flipped ITM/OTM on a T95 market).
#   - For HeatingOil, Gold, Silver, Cocoa, Coffee, Sugar, Kalshi's rule
#     doesn't name a contract, so we use =F as best approximation
#     (confidence: "approx").
#
# `confidence`: "exact" = Kalshi's named contract in rules_primary (tested match);
#               "close" = metals, Yahoo 1-min within ~0.5% of Kalshi settle (2026-04-20 test);
#               "unreliable" = softs, Yahoo daily 1.5-3.5% off Kalshi settle (2026-04-17 test).
#
# HOIL has no historical comparison yet (no settled markets to compare against). Use with caution.
# When Kalshi rolls contract months (typical 1-2 month cycle), UPDATE exact symbols.
#
# SOFTS: Yahoo CC=F/KC=F/SB=F diverge from Kalshi settlement by 1.5-3.5%. Not good enough
# for ITM/OTM flag on $1-$25 strike grids. Dashboard marks these as "?" rather than ITM/OTM.
FUTURES_MAP = {
    # Kalshi-named contracts (exact)
    "KXBRENTD":    {"yahoo": "BZM26.NYM", "name": "Brent Jun 2026",   "unit": "USD/bbl",    "confidence": "exact"},
    "KXCRUDE":     {"yahoo": "CLM26.NYM", "name": "WTI Jun 2026",     "unit": "USD/bbl",    "confidence": "exact"},
    "KXNATGASD":   {"yahoo": "NGM26.NYM", "name": "NatGas Jun 2026",  "unit": "USD/MMBtu",  "confidence": "exact"},
    "KXCOPPERD":   {"yahoo": "HGK26.CMX", "name": "Copper May 2026",  "unit": "USD/lb",     "confidence": "exact"},
    "KXCORNW":     {"yahoo": "ZCN26.CBT", "name": "Corn Jul 2026",    "unit": "cents/bu",   "confidence": "exact"},
    "KXWHEATW":    {"yahoo": "ZWN26.CBT", "name": "Wheat Jul 2026",   "unit": "cents/bu",   "confidence": "exact"},
    "KXSOYBEANW":  {"yahoo": "ZSN26.CBT", "name": "Soybean Jul 2026", "unit": "cents/bu",   "confidence": "exact"},
    # Metals — Yahoo =F within ~0.5% of Kalshi
    "KXGOLDD":     {"yahoo": "GC=F",      "name": "Gold",             "unit": "USD/oz",     "confidence": "close"},
    "KXSILVERD":   {"yahoo": "SI=F",      "name": "Silver",           "unit": "USD/oz",     "confidence": "close"},
    # Softs — Yahoo =F 1.5-3.5% off Kalshi; DON'T trust for ITM/OTM flag
    "KXCOCOAW":    {"yahoo": "CC=F",      "name": "Cocoa",            "unit": "USD/MT",     "confidence": "unreliable"},
    "KXCOFFEEW":   {"yahoo": "KC=F",      "name": "Coffee",           "unit": "cents/lb",   "confidence": "unreliable"},
    "KXSUGARW":    {"yahoo": "SB=F",      "name": "Sugar #11",        "unit": "cents/lb",   "confidence": "unreliable"},
    # HOIL — unknown (no historical compare done)
    "KXHOILW":     {"yahoo": "HO=F",      "name": "Heating oil",      "unit": "USD/gal",    "confidence": "unknown"},
}


def ensure_schema(db_path: str = settings.DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS futures_prices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kalshi_prefix   TEXT NOT NULL,
            yahoo_symbol    TEXT NOT NULL,
            name            TEXT NOT NULL,
            price           REAL NOT NULL,
            prev_close      REAL,
            change_pct      REAL,
            volume          INTEGER,
            fetched_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_futures_prefix_time
            ON futures_prices(kalshi_prefix, fetched_at DESC);
        """)
        conn.commit()
    finally:
        conn.close()


def fetch_yahoo_quote(symbol: str, timeout: float = 5.0) -> Optional[dict]:
    """Fetch latest quote via Yahoo Finance v8 chart endpoint (no auth)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        _log.warning(f"yahoo fetch failed for {symbol}: {e}")
        return None

    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None:
        return None
    return {
        "price":       float(price),
        "prev_close":  float(prev) if prev is not None else None,
        "change_pct":  (float(price) - float(prev)) / float(prev) if prev else None,
        "volume":      meta.get("regularMarketVolume"),
    }


def sync_prices(db_path: str = settings.DB_PATH, delay_sec: float = 0.3) -> dict:
    ensure_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    success = 0
    failed = 0
    try:
        for kalshi_prefix, meta in FUTURES_MAP.items():
            q = fetch_yahoo_quote(meta["yahoo"])
            if q is None:
                failed += 1
                continue
            conn.execute(
                """INSERT INTO futures_prices
                   (kalshi_prefix, yahoo_symbol, name, price, prev_close,
                    change_pct, volume, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (kalshi_prefix, meta["yahoo"], meta["name"],
                 q["price"], q["prev_close"], q["change_pct"],
                 q["volume"], now),
            )
            success += 1
            time.sleep(delay_sec)  # rate-limit kindness to Yahoo
        conn.commit()
    finally:
        conn.close()
    return {"success": success, "failed": failed}


def latest_prices(db_path: str = settings.DB_PATH) -> dict:
    """Return most recent price per kalshi_prefix."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT kalshi_prefix, yahoo_symbol, name, price, change_pct, fetched_at
               FROM futures_prices fp
               WHERE fetched_at = (SELECT MAX(fetched_at) FROM futures_prices
                                   WHERE kalshi_prefix = fp.kalshi_prefix)
               ORDER BY kalshi_prefix"""
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: {"yahoo": r[1], "name": r[2], "price": r[3],
                   "change_pct": r[4], "at": r[5]} for r in rows}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--show", action="store_true", help="show latest prices only")
    a = p.parse_args()
    if a.show:
        prices = latest_prices()
        print(f"\n══ FUTURES REFERENCE PRICES ══")
        for prefix, info in prices.items():
            arrow = "↑" if (info.get("change_pct") or 0) >= 0 else "↓"
            pct = (info.get("change_pct") or 0) * 100
            print(f"  {prefix:15s}  {info['yahoo']:6s}  {info['name']:18s}  "
                  f"${info['price']:>9.4f}  {arrow}{abs(pct):>5.2f}%  "
                  f"at {info['at'][:19]}")
        return
    s = sync_prices()
    print(f"futures_sync  success={s['success']}  failed={s['failed']}")


if __name__ == "__main__":
    main()
