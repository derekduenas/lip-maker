"""LIP program discovery.

Polls Kalshi's /incentive_programs endpoint, parses active liquidity
programs, filters by our settings (reward threshold, target-size cap,
discount-factor floor, series blocklist), and persists to SQLite.

The /incentive_programs endpoint is UNAUTHENTICATED per Kalshi docs.

API response fields (from Kalshi docs):
  period_reward:       int, CENTI-CENTS (divide by 10,000 for USD)
  discount_factor_bps: int, basis points (divide by 10,000 for multiplier)
  target_size_fp:      fixed-point string, up to 2 decimals
  incentive_type:      "liquidity" | "volume"
  paid_out:            bool
  start_date/end_date: ISO 8601
  market_ticker:       Kalshi market identifier
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)


def _series_from_market_ticker(ticker: str) -> str:
    """Extract series prefix. E.g., 'KXHIGHCHI-26APR19-B50.5' → 'KXHIGHCHI'."""
    if not ticker:
        return ""
    # Series is everything before the first hyphen
    return ticker.split("-", 1)[0]


def _parse_program(raw: dict) -> dict:
    """Normalize raw incentive program fields."""
    period_reward_usd = float(raw.get("period_reward", 0)) / 10_000.0  # centi-cents → USD
    discount_factor   = float(raw.get("discount_factor_bps", 10_000)) / 10_000.0  # bps → multiplier
    target_size       = float(raw.get("target_size_fp", 0))
    start_date        = raw.get("start_date", "")
    end_date          = raw.get("end_date", "")

    days_in_period = 1
    try:
        s = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        days_in_period = max(1, (e - s).days or 1)
    except Exception:
        pass

    reward_per_day = period_reward_usd / days_in_period

    return {
        "id":                 raw.get("id"),
        "market_ticker":      raw.get("market_ticker", ""),
        "series_ticker":      _series_from_market_ticker(raw.get("market_ticker", "")),
        "start_date":         start_date,
        "end_date":           end_date,
        "period_reward_usd":  period_reward_usd,
        "discount_factor":    discount_factor,
        "target_size":        target_size,
        "paid_out":           int(bool(raw.get("paid_out", False))),
        "reward_per_day_usd": reward_per_day,
    }


def _decide_enrol(p: dict) -> tuple[int, str]:
    """Our quoting decision for a program. Returns (enrol 0/1, reason)."""
    series = p["series_ticker"] or ""
    # 2026-04-22: prefix-match (startswith) instead of exact. Kalshi spawns
    # subseries like KXNBAMENTION/KXNBARETURN under the KXNBA family — exact
    # match missed 125+ NBA prop markets ($2.5k/day reward exposure) that
    # SIG dominates. KXFEDDECISION still matches only itself (no KXFEDDECISION*
    # subseries exist; doesn't accidentally match KXFEDERALCHARGE).
    if any(series.startswith(b) for b in settings.SERIES_BLOCKLIST):
        return 0, f"blocklist:series({series})"
    if p["reward_per_day_usd"] < settings.MIN_REWARD_PER_DAY_USD:
        return 0, f"reward_too_small:{p['reward_per_day_usd']:.2f}"
    if p["target_size"] > settings.MAX_TARGET_SIZE_CONTRACTS:
        return 0, f"target_too_large:{p['target_size']:.0f}"
    if p["discount_factor"] < settings.MIN_DISCOUNT_FACTOR:
        return 0, f"discount_too_low:{p['discount_factor']:.2f}"
    if p["paid_out"]:
        return 0, "already_paid_out"
    return 1, "ok"


def discover(*, status: str = "active", save: bool = True) -> list[dict]:
    """Fetch + persist LIP programs. Returns parsed list."""
    c = KalshiClient()
    programs: list[dict] = []
    cursor = None

    while True:
        params = {"status": status, "type": "liquidity", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = c.get_unauth("/incentive_programs", params=params)
        except Exception as e:
            _log.warning(f"/incentive_programs call failed: {e}")
            break
        batch = resp.get("incentive_programs", [])
        for raw in batch:
            if raw.get("incentive_type") != "liquidity":
                continue
            programs.append(_parse_program(raw))
        cursor = resp.get("next_cursor")
        if not cursor:
            break

    # Decide enrolment and persist
    now_iso = datetime.now(timezone.utc).isoformat()
    if save and programs:
        conn = sqlite3.connect(settings.DB_PATH)
        try:
            for p in programs:
                enrol, reason = _decide_enrol(p)
                conn.execute(
                    """INSERT OR REPLACE INTO lip_programs
                       (id, market_ticker, series_ticker, start_date, end_date,
                        period_reward_usd, discount_factor, target_size, paid_out,
                        enrolled, blocked_reason, reward_per_day_usd, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p["id"], p["market_ticker"], p["series_ticker"],
                        p["start_date"], p["end_date"], p["period_reward_usd"],
                        p["discount_factor"], p["target_size"], p["paid_out"],
                        enrol, reason if not enrol else None,
                        p["reward_per_day_usd"], now_iso,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    return programs


def top_n_to_quote(n: int = 100, max_target_size: int = 2500,
                   db_path: str = settings.DB_PATH) -> list[dict]:
    """Return top-N enrolled REACHABLE markets ranked by PRIORITY-WEIGHTED reward.

    2026-04-28 (#100 EV-PRIORITY V2):
      First attempt used observed share (our_score/total_score) as an EV
      multiplier, but the snapshot share metric understates true rebate
      capture by 50-100x — it's per-second snapshot fraction, not
      time-integrated $-flow. With share=6e-05, even MAMDANIEO ranked
      below random political markets at the 0.20 prior. Net effect: would
      have crowded out proven winners (BRENTD/CORNW/COPPERD per Apr 27
      audit) for unproven Trump-time/endorsement markets.

      V2 ranks by reward × series_priority. Series priority comes from the
      same SIZE_MULTIPLIER_BY_SERIES table that tier-2x's our quote sizes
      (#119) — known winners get ranking lift, defaults to 1.0. Filters
      (end_date past, blacklist, target_size cap) still apply.

    Filters:
      1. enrolled=1 AND paid_out=0
      2. target_size <= max_target_size
      3. NOT in active market_blacklist
      4. end_date > today (don't quote settled markets)
    """
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        try:
            rows = conn.execute(
                """SELECT p.market_ticker, p.series_ticker, p.reward_per_day_usd,
                          p.target_size, p.discount_factor, p.start_date, p.end_date
                   FROM lip_programs p
                   LEFT JOIN market_blacklist b
                     ON p.market_ticker = b.ticker
                     AND datetime(b.expires_at) > datetime('now')
                   WHERE p.enrolled = 1 AND p.paid_out = 0
                     AND p.target_size <= ?
                     AND date(p.end_date) > date('now')
                     AND b.ticker IS NULL
                   ORDER BY p.reward_per_day_usd DESC
                   LIMIT ?""",
                (max_target_size, n * 3),  # over-fetch so priority can re-sort top-n
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """SELECT market_ticker, series_ticker, reward_per_day_usd,
                          target_size, discount_factor, start_date, end_date
                   FROM lip_programs
                   WHERE enrolled = 1 AND paid_out = 0
                     AND target_size <= ?
                   ORDER BY reward_per_day_usd DESC
                   LIMIT ?""",
                (max_target_size, n * 3),
            ).fetchall()
    finally:
        conn.close()

    # Re-rank by reward × series priority. Defaults to 1.0 when not in table.
    multipliers = settings.SIZE_MULTIPLIER_BY_SERIES
    default = settings.DEFAULT_SIZE_MULTIPLIER

    enriched = []
    for r in rows:
        series = r[1] or ""
        priority = multipliers.get(series, default)
        weighted = (r[2] or 0.0) * priority
        enriched.append({
            "market_ticker":         r[0],
            "series_ticker":         r[1],
            "reward_per_day_usd":    r[2],
            "target_size":           r[3],
            "discount_factor":       r[4],
            "start_date":            r[5],
            "end_date":              r[6],
            "series_priority":       priority,
            "priority_weighted_reward": round(weighted, 4),
        })
    enriched.sort(key=lambda d: -d["priority_weighted_reward"])
    return enriched[:n]


def report_enrolled(db_path: str = settings.DB_PATH) -> None:
    """Print what we'd quote."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT market_ticker, series_ticker, reward_per_day_usd,
                      target_size, discount_factor
               FROM lip_programs
               WHERE enrolled = 1 AND paid_out = 0
               ORDER BY reward_per_day_usd DESC"""
        ).fetchall()
        total_reward = conn.execute(
            "SELECT COALESCE(SUM(reward_per_day_usd), 0) FROM lip_programs WHERE enrolled = 1 AND paid_out = 0"
        ).fetchone()[0]
        # Top-30 view — where we'd actually quote
        top30_reward = sum(r[2] for r in rows[:30])
        blocked_rows = conn.execute(
            """SELECT blocked_reason, COUNT(*) FROM lip_programs
               WHERE enrolled = 0 GROUP BY blocked_reason ORDER BY 2 DESC"""
        ).fetchall()
        # Series breakdown of enrolled
        series_rows = conn.execute(
            """SELECT series_ticker, COUNT(*) as n, SUM(reward_per_day_usd) as rwd
               FROM lip_programs
               WHERE enrolled = 1 AND paid_out = 0
               GROUP BY series_ticker
               ORDER BY rwd DESC
               LIMIT 15"""
        ).fetchall()
    finally:
        conn.close()

    print(f"Enrolled markets: {len(rows)}  Total daily reward pool: ${total_reward:.2f}")
    print(f"Top-30 by reward-per-day: ${top30_reward:.2f}/day total")
    print(f"  at 10% realized share = ~${top30_reward * 0.10:.2f}/day = ~${top30_reward * 0.10 * 30:.0f}/month")
    print()
    print("Top 20 markets:")
    for tkr, series, rwd, ts, df in rows[:20]:
        print(f"  {tkr:40s} series={series:20s} ${rwd:>6.2f}/day target={ts:>6.0f} df={df:.2f}")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")
    print()
    print("Enrolled by series (top 15):")
    for series, n, rwd in series_rows:
        print(f"  {series:20s}: {n:3d} markets  ${rwd:>7.2f}/day total")
    print()
    print("Blocked (top reasons):")
    for reason, n in blocked_rows[:10]:
        print(f"  {reason}: {n}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    programs = discover()
    print(f"fetched {len(programs)} active liquidity programs")
    print()
    report_enrolled()
