"""Polymarket public data recorder — uses gamma-api (no auth required).

PURPOSE:
  Polls Polymarket gamma-api every N seconds to capture market structure
  + rewards data + best bid/ask. Builds a baseline of:
    - Which markets have active rewards programs (clobRewards.rewardsDailyRate)
    - Per-market spread + min_size + max_spread requirements
    - Best bid/ask drift (proxy for volatility)

  Once we have user's polymarket.us API keys, we'll layer the WS feed
  for real-time orderbook depth. For now this gives 80% of what we need
  with zero auth complexity.

USAGE:
  python tools/public_recorder.py                # poll every 60s, top markets
  python tools/public_recorder.py --discover     # one-shot: list rewards markets
  python tools/public_recorder.py --interval 30  # custom poll interval
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)


def http_get(url: str, timeout: int = 10) -> dict | list:
    import ssl
    # macOS Python sometimes lacks system certs; use certifi if available,
    # else fall back to unverified for the public gamma-api (read-only,
    # no credentials transmitted).
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        try:
            ctx.load_default_certs()
        except Exception:
            ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "polymarket-maker/0.1 (+https://github.com/innait)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


def fetch_active_markets(limit: int = 200) -> list[dict]:
    """Pull active markets from gamma-api. Filters out closed/archived."""
    url = f"{settings.PM_GAMMA_BASE}/markets?limit={limit}"
    try:
        data = http_get(url)
        if isinstance(data, list):
            return data
        return data.get("markets", []) or data.get("data", [])
    except Exception as e:
        _log.error(f"fetch_active_markets failed: {e}")
        return []


def fetch_rewards_markets(limit: int = 500) -> list[dict]:
    """Filter markets that have active clobRewards (rewardsDailyRate > 0).
    These are the only markets worth quoting for rebate harvest."""
    all_markets = fetch_active_markets(limit=limit)
    out = []
    for m in all_markets:
        rewards = m.get("clobRewards") or []
        for r in rewards:
            if float(r.get("rewardsDailyRate") or 0) > 0:
                m["_active_reward"] = r
                out.append(m)
                break
    return out


def persist_markets(conn: sqlite3.Connection, markets: list[dict]) -> int:
    """UPSERT current market state into pm_markets."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue
        reward = m.get("_active_reward") or {}
        daily_rate = float(reward.get("rewardsDailyRate") or 0)
        # Reward "period" for Polymarket is daily. Use as period_reward_usd.
        period_reward = daily_rate
        try:
            conn.execute(
                """INSERT INTO pm_markets
                   (market_slug, series_ticker, question, end_date,
                    epoch_start, epoch_end, period_reward_usd,
                    reward_per_day_usd, max_spread, min_size,
                    b_size_weight, enrolled, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(market_slug, epoch_start) DO UPDATE SET
                     period_reward_usd  = excluded.period_reward_usd,
                     reward_per_day_usd = excluded.reward_per_day_usd,
                     max_spread         = excluded.max_spread,
                     min_size           = excluded.min_size,
                     last_seen          = excluded.last_seen""",
                (slug,
                 _series_from_slug(slug),
                 m.get("question", "")[:200],
                 m.get("endDate") or m.get("endDateIso"),
                 reward.get("startDate"),
                 reward.get("endDate"),
                 period_reward,
                 daily_rate,
                 float(m.get("rewardsMaxSpread") or 0),
                 float(m.get("rewardsMinSize") or 0),
                 1.0,  # b weight default
                 1 if daily_rate > 0 else 0,
                 now_iso),
            )
            n += 1
        except Exception as e:
            _log.debug(f"persist {slug} failed: {e}")
    conn.commit()
    return n


def persist_book_snapshots(conn: sqlite3.Connection, markets: list[dict]) -> int:
    """Capture best bid/ask + spread + midpoint per market."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue
        best_bid = m.get("bestBid")
        best_ask = m.get("bestAsk")
        if best_bid is None or best_ask is None:
            continue
        try:
            best_bid_f = float(best_bid)
            best_ask_f = float(best_ask)
        except (TypeError, ValueError):
            continue
        midpoint = (best_bid_f + best_ask_f) / 2
        try:
            conn.execute(
                """INSERT INTO pm_book_log
                   (market_slug, captured_at, yes_bids_json, yes_asks_json,
                    best_yes_bid, best_yes_ask, midpoint)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (slug, now_iso,
                 json.dumps([[best_bid_f, 0]]),
                 json.dumps([[best_ask_f, 0]]),
                 best_bid_f, best_ask_f, midpoint),
            )
            n += 1
        except Exception:
            pass
    conn.commit()
    return n


def _series_from_slug(slug: str) -> str:
    """Extract event-level series from market slug.

    Polymarket slugs are human-readable. Many markets share an event prefix
    via the events.ticker field (we'd need to query for that). For now,
    use first 3 dash-separated tokens as a rough series proxy.
    """
    parts = (slug or "").split("-")
    return "-".join(parts[:3])


def loop(interval: int = 60) -> None:
    """Poll forever. Persists markets + book snapshots every interval."""
    from init_db import init_db
    init_db()
    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    # 2026-04-28: ensure pragmas applied per-connection (audit-flagged
    # iowait spikes during commit). NORMAL sync + WAL + bigger cache
    # smooths the burst.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA cache_size=-20000")  # 20MB cache
    _log.info(f"polling every {interval}s — gamma-api={settings.PM_GAMMA_BASE}")
    try:
        while True:
            try:
                markets = fetch_rewards_markets(limit=500)
                n_markets = persist_markets(conn, markets)
                n_books   = persist_book_snapshots(conn, markets)
                total_pool = sum(
                    float((m.get("_active_reward") or {}).get("rewardsDailyRate") or 0)
                    for m in markets
                )
                _log.info(f"recorded {n_markets} markets / {n_books} books / "
                          f"${total_pool:.0f}/day total reward pool")
            except Exception as e:
                _log.error(f"poll cycle error: {e}")
            time.sleep(interval)
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--interval", type=int, default=60,
                   help="poll interval seconds (default 60)")
    p.add_argument("--discover", action="store_true",
                   help="one-shot list of rewards-eligible markets")
    p.add_argument("--limit", type=int, default=500)
    a = p.parse_args()

    if a.discover:
        markets = fetch_rewards_markets(limit=a.limit)
        print(f"\n══ POLYMARKET REWARDS-ELIGIBLE MARKETS ({len(markets)}) ══")
        # Sort by daily reward rate desc
        markets.sort(key=lambda m: -float(
            (m.get("_active_reward") or {}).get("rewardsDailyRate") or 0
        ))
        total_pool = 0
        print(f"  {'slug':<55s}  {'$/day':>8s}  {'spread':>7s}  {'minSize':>8s}")
        for m in markets[:30]:
            r = m.get("_active_reward") or {}
            daily = float(r.get("rewardsDailyRate") or 0)
            total_pool += daily
            print(f"  {(m.get('slug') or '')[:55]:<55s}  "
                  f"${daily:>7.0f}  "
                  f"{m.get('spread', 0):>7.3f}  "
                  f"{m.get('rewardsMinSize', 0):>8.0f}")
        print(f"\n  TOTAL daily reward pool (top 30): ${total_pool:.0f}")
        print(f"  Across all rewards markets: ${sum(float((m.get('_active_reward') or {}).get('rewardsDailyRate') or 0) for m in markets):.0f}/day")
        return 0

    loop(interval=a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
