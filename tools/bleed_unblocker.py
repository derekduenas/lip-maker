"""BLEED-UNBLOCKER — auto-clear mtm_bleed blacklist entries when position settled.

Bug fixed today: zombie liquidator sells positions → realized loss → bleed
monitor sees the loss and blacklists ticker → AEGIS can't requote that
high-LIP market even though the bleed is over (position now = 0).

This cron runs every 10 min:
  1. Pull current Kalshi positions
  2. For each mtm_bleed entry where current position = 0 → DELETE
  3. The bleed monitor will re-blacklist if NEW positions actually bleed

Also clears toxicity entries older than 6h (give markets a chance to recover).
"""
import os, sys, sqlite3
from pathlib import Path

LIP_PATH = Path("/root/lip-maker")
LIP_DB = LIP_PATH / "data" / "lip_maker.db"


def main():
    os.chdir(str(LIP_PATH))
    sys.path.insert(0, str(LIP_PATH))
    from execution.kalshi_auth import KalshiClient
    c = KalshiClient()
    positions = []
    cursor = None
    while True:
        p = {"limit": 200}
        if cursor: p["cursor"] = cursor
        r = c.get("/portfolio/positions", params=p)
        positions.extend(r.get("market_positions") or [])
        cursor = r.get("cursor")
        if not cursor: break
    zero_pos = {p["ticker"] for p in positions if float(p.get("position_fp", 0)) == 0}

    conn = sqlite3.connect(str(LIP_DB), timeout=10)
    bleed = [r[0] for r in conn.execute(
        "SELECT ticker FROM market_blacklist WHERE reason LIKE 'mtm_bleed%'").fetchall()]
    stale = [t for t in bleed if t in zero_pos]
    if stale:
        ph = ",".join("?" * len(stale))
        conn.execute(f"DELETE FROM market_blacklist WHERE reason LIKE 'mtm_bleed%' AND ticker IN ({ph})", stale)
        conn.commit()
    # Also clear toxicity entries older than 6h
    tox_cleared = conn.execute(
        "DELETE FROM market_blacklist WHERE reason LIKE 'toxicity%' AND added_at < datetime('now', '-6 hours')"
    ).rowcount
    conn.commit()
    conn.close()
    print(f"  bleed: cleared {len(stale)} stale entries (position=0 now)")
    print(f"  toxicity: cleared {tox_cleared} entries (>6h old, retry)")


if __name__ == "__main__":
    main()
