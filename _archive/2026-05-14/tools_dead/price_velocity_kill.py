"""Price Velocity Kill (Sprint 3C — god-mode)

Polls top-N highest-reward enrolled markets every 2 min. If mid moves
≥8c in <5 min, blacklists for 30 min. Catches breaking news without
external API.

Why: news_kill catches macro/weather. fill_velocity catches adverse fills
on quoted markets. This catches IDIOSYNCRATIC moves on quoted-or-not markets
(political news, single-stock surprises) BEFORE we get run over.

Run via cron every 2 min.
"""
from __future__ import annotations

# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "price_velocity_kill")
except Exception:
    pass

import sys, time, sqlite3
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/root/lip-maker")
from execution.kalshi_auth import KalshiClient

DB = "/root/lip-maker/data/lip_maker.db"
TOP_N = 50                      # poll top-50 reward markets
VELOCITY_WINDOW_SEC = 300       # 5 min
KILL_DELTA_CENTS = 8            # ≥8c move in window
BLACKLIST_TTL_MIN = 30


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            ticker TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            mid_cents REAL NOT NULL,
            best_bid INTEGER,
            best_ask INTEGER,
            PRIMARY KEY (ticker, captured_at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_ticker_time ON price_history(ticker, captured_at DESC)")


def fetch_top_markets(conn) -> list[str]:
    rows = conn.execute(f"""
        SELECT market_ticker FROM lip_programs
        WHERE enrolled = 1 AND paid_out = 0
          AND datetime(end_date) > datetime('now')
          AND reward_per_day_usd > 5
        ORDER BY reward_per_day_usd DESC
        LIMIT {TOP_N}
    """).fetchall()
    return [r[0] for r in rows]


def is_blacklisted(conn, ticker: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM market_blacklist WHERE ticker = ? AND (expires_at IS NULL OR expires_at > ?)",
        (ticker, datetime.now(timezone.utc).isoformat())
    ).fetchone()
    return row is not None


def main():
    client = KalshiClient()
    conn = sqlite3.connect(DB, timeout=10.0)
    ensure_table(conn)

    tickers = fetch_top_markets(conn)
    if not tickers:
        print("No enrolled markets to poll")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    now_dt = datetime.now(timezone.utc)
    cutoff_iso = (now_dt - timedelta(seconds=VELOCITY_WINDOW_SEC * 2)).isoformat()  # keep last 10min

    polled = 0
    killed = 0
    errors = 0

    for tk in tickers:
        try:
            r = client.get(f"/markets/{tk}/orderbook")
            ob = r.get("orderbook_fp") or r.get("orderbook") or {}
            # Kalshi returns yes_dollars/no_dollars as [["0.0100", "1022.98"], ...]
            yes_bids = ob.get("yes_dollars") or ob.get("yes") or []
            no_bids = ob.get("no_dollars") or ob.get("no") or []
            def _max_price_cents(book):
                if not book: return None
                try:
                    return max(int(round(float(b[0]) * 100)) for b in book)
                except (ValueError, TypeError, IndexError):
                    return None
            best_yes = _max_price_cents(yes_bids)
            best_no = _max_price_cents(no_bids)
            if best_yes is None or best_no is None:
                continue
            best_ask_yes = 100 - best_no  # implied
            mid = (best_yes + best_ask_yes) / 2.0
            conn.execute(
                "INSERT OR REPLACE INTO price_history (ticker, captured_at, mid_cents, best_bid, best_ask) VALUES (?, ?, ?, ?, ?)",
                (tk, now_iso, mid, best_yes, best_ask_yes)
            )
            polled += 1
        except Exception as e:
            errors += 1
            continue

        # Check velocity vs ~5min ago
        prev = conn.execute(
            "SELECT mid_cents, captured_at FROM price_history WHERE ticker = ? AND captured_at < ? ORDER BY captured_at DESC LIMIT 1",
            (tk, (now_dt - timedelta(seconds=VELOCITY_WINDOW_SEC - 60)).isoformat())
        ).fetchone()
        if prev:
            prev_mid, prev_ts = prev
            delta = abs(mid - prev_mid)
            if delta >= KILL_DELTA_CENTS and not is_blacklisted(conn, tk):
                expires = (now_dt + timedelta(minutes=BLACKLIST_TTL_MIN)).isoformat()
                reason = f"price_velocity:{delta:.0f}c_in_{VELOCITY_WINDOW_SEC//60}m"
                conn.execute(
                    "INSERT OR REPLACE INTO market_blacklist (ticker, reason, added_at, expires_at) VALUES (?, ?, ?, ?)",
                    (tk, reason, now_iso, expires)
                )
                killed += 1
                print(f"  KILL {tk}: mid {prev_mid:.0f}→{mid:.0f}c ({delta:+.0f}c)")

        time.sleep(0.05)  # be polite to Kalshi REST

    # Prune old price_history rows (>10 min)
    conn.execute("DELETE FROM price_history WHERE captured_at < ?", (cutoff_iso,))
    conn.commit()
    conn.close()

    print(f"Polled {polled}/{len(tickers)} markets, killed {killed}, errors {errors}")


if __name__ == "__main__":
    main()
