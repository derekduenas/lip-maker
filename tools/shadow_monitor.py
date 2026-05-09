"""Shadow Monitor — daily verification that shadow mode is capturing complete data."""

import json
import sqlite3
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH


def daily_check() -> dict:
    """Run daily shadow data verification."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.utcnow()
    yesterday = (now - timedelta(hours=24)).isoformat()

    print("SHADOW MONITOR — DAILY CHECK")
    print(f"As of: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 55)

    # Shadow trades in last 24h
    recent = conn.execute(
        "SELECT * FROM shadow_trades WHERE placed_at > ?",
        (yesterday,)
    ).fetchall()

    # All shadow trades ever
    total = conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE outcome != 'OPEN'"
    ).fetchone()[0]
    wins = conn.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE outcome = 'WIN'"
    ).fetchone()[0]
    losses = conn.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE outcome = 'LOSS'"
    ).fetchone()[0]
    open_t = conn.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE outcome = 'OPEN'"
    ).fetchone()[0]

    print(f"\n  Shadow trades (last 24h):  {len(recent)}")
    print(f"  Shadow trades (all time):  {total}")
    print(f"  Resolved: {wins}W / {losses}L / {open_t} open")

    if wins + losses > 0:
        wr = wins / (wins + losses) * 100
        print(f"  Win rate: {wr:.0f}%")

    # Check data completeness
    cols = conn.execute("PRAGMA table_info(shadow_trades)").fetchall()
    col_names = [c[1] for c in cols]
    print(f"\n  Table columns: {len(col_names)}")

    # Check for any null fields in recent trades
    if recent:
        print(f"\n  Recent shadow trade details:")
        for row in recent:
            # Map to dict
            trade = dict(zip(col_names, row))
            print(f"    {trade.get('term','?'):<20} {trade.get('side','?'):<3} "
                  f"@{(trade.get('price',0) or 0)*100:.0f}c "
                  f"x{trade.get('contracts',0)} [{trade.get('outcome','?')}]")

    # Check real trades too
    print(f"\n  Real trades (paper):")
    real = conn.execute(
        "SELECT term, side, price, contracts, outcome, pnl FROM trades ORDER BY id"
    ).fetchall()
    for r in real:
        pnl = f"${r[5]:+.2f}" if r[5] else "--"
        print(f"    {r[0]:<20} {r[1]:<3} @{r[2]*100:.0f}c x{r[3]} [{r[4]}] {pnl}")

    # Check last scan log
    print(f"\n  Server scan status:")
    log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "last_morning_scan.json")
    if os.path.exists(log_path):
        with open(log_path) as f:
            scan = json.load(f)
        print(f"    Last scan: {scan.get('timestamp', '?')[:19]}")
        print(f"    Scanned: {scan.get('scanned', 0)} markets")
        print(f"    Qualified: {scan.get('qualified', 0)}")
        print(f"    Opportunities: {scan.get('opportunities', 0)}")
        print(f"    Trades: {scan.get('trades_placed', 0)}")
        print(f"    No corpus: {scan.get('skipped_no_corpus', 0)}")
    else:
        print("    No scan log found locally (check server)")

    conn.close()

    print()
    print("=" * 55)
    result = {
        "timestamp": now.isoformat(),
        "shadow_24h": len(recent),
        "shadow_total": total,
        "wins": wins,
        "losses": losses,
        "open": open_t,
    }

    if total == 0 and (now - datetime(2026, 4, 18)).days >= 3:
        print("WARNING: No shadow trades after 3+ days.")
        print("  Check: are markets clearing 15pp edge + dual confirmation?")
        print("  Check: is SHADOW_MODE=True in server .env?")

    return result


if __name__ == "__main__":
    daily_check()
