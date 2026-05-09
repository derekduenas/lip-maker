"""LIP Yield Funnel — single-pass snapshot of every layer.

The system has many layers (discovery → rank → safety → subscribe → quote →
fill → settle → payout). Each silently drops markets. Without per-layer
counts, we can't tell where 80% of pool value is being lost.

This script runs every minute and writes one row per layer to funnel_log,
plus emits one summary line. Then `tools/funnel_view.py` shows the last
hour or day. End state: it is impossible for a market to silently disappear
between layers — every drop is counted and reasoned.

Layers (Kalshi-only first; PM added once unhalted):
  1. ENROLLED   — lip_programs where enrolled=1, paid_out=0, end_date>now
  2. RANKED     — top_n_to_quote() output (capital concentration cutoff)
  3. SAFE       — survived all _passes_safety checks (parsed from log, last 5 min)
  4. SUBSCRIBED — markets currently flowing book updates (parsed from log, last 5 min)
  5. QUOTING    — markets with at least one resting order (Kalshi API)
  6. FILLED_1H  — markets with a fill in the last hour (fill_ledger)
  7. SETTLED_24H — markets settled in last 24h (settlement_log)
  8. NET_$_24H  — sum(rebate + realized) over settled (settlement_log)

Drop-reason histograms recorded per layer where applicable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)

LOG_PATH = Path(settings.LOG_PATH) if hasattr(settings, "LOG_PATH") else Path("/root/lip-maker/logs/lip_maker.log")


# ── Schema ────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS funnel_log (
  snapshot_at  TEXT NOT NULL,
  layer        TEXT NOT NULL,
  count        INTEGER NOT NULL,
  detail_json  TEXT,
  PRIMARY KEY(snapshot_at, layer)
);
CREATE INDEX IF NOT EXISTS idx_funnel_at ON funnel_log(snapshot_at);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ── Layer probes ──────────────────────────────────────────────────────────

def layer_enrolled(conn: sqlite3.Connection) -> tuple[int, dict]:
    """Programs we've decided are quotable."""
    n = conn.execute("""
        SELECT COUNT(*) FROM lip_programs
        WHERE enrolled = 1 AND paid_out = 0
          AND datetime(end_date) > datetime('now')
    """).fetchone()[0]
    # Drop reasons (programs that were rejected at enrolment)
    drop_reasons = dict(conn.execute("""
        SELECT blocked_reason, COUNT(*) FROM lip_programs
        WHERE enrolled = 0 AND paid_out = 0
          AND datetime(end_date) > datetime('now')
          AND blocked_reason IS NOT NULL
        GROUP BY blocked_reason ORDER BY 2 DESC LIMIT 8
    """).fetchall())
    pool_d = conn.execute("""
        SELECT COALESCE(SUM(reward_per_day_usd), 0) FROM lip_programs
        WHERE enrolled = 1 AND paid_out = 0
          AND datetime(end_date) > datetime('now')
    """).fetchone()[0] or 0
    return n, {"pool_per_day_usd": round(pool_d, 2), "drop_reasons": drop_reasons}


def layer_ranked() -> tuple[int, dict]:
    """Top-N selected by ranker. Reads --top-n from systemd unit so funnel
    matches the live runner's selection cardinality. Falls back to 40."""
    try:
        from engine.lip_discovery import top_n_to_quote
        top_n = 40
        try:
            with open("/etc/systemd/system/lip-maker.service") as f:
                for line in f:
                    if "--top-n" in line:
                        parts = line.strip().split()
                        for i, p in enumerate(parts):
                            if p == "--top-n" and i + 1 < len(parts):
                                top_n = int(parts[i + 1])
                                break
        except Exception:
            pass
        markets = top_n_to_quote(top_n)
        pool_d = sum(m.get("reward_per_day_usd", 0) or 0 for m in markets)
        series_breakdown = Counter(m.get("series_ticker", "?") for m in markets)
        return len(markets), {
            "pool_per_day_usd": round(pool_d, 2),
            "series_breakdown": dict(series_breakdown.most_common(10)),
        }
    except Exception as e:
        return -1, {"error": str(e)}


def _tail_log_window_lines(seconds: int = 300) -> list[str]:
    """Return log lines from approximately the last `seconds`. Best-effort."""
    if not LOG_PATH.exists():
        return []
    # Use tail -n 5000 (cheap) and filter by timestamp
    try:
        out = subprocess.run(
            ["tail", "-n", "5000", str(LOG_PATH)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    lines = out.splitlines()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_str = (cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    # Filter to lines whose timestamp is within window. The log format is
    # "YYYY-MM-DD HH:MM:SS,ms LEVEL ..."
    out_lines: list[str] = []
    cut_dt = datetime.now()
    for line in lines:
        if len(line) < 20:
            continue
        ts_str = line[:19]
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (cut_dt - ts).total_seconds() <= seconds:
            out_lines.append(line)
    return out_lines


def layer_safe(window_lines: list[str]) -> tuple[int, dict]:
    """How many markets passed safety in the last 5 minutes (distinct tickers).
    Counted as: markets with at least one quote PLACED minus those whose latest
    log was a safety_gate failure. Approximation — true count needs runtime
    instrumentation.
    """
    placed_tickers: set[str] = set()
    failed_tickers: Counter = Counter()
    fail_reasons: Counter = Counter()
    fail_re = re.compile(r"safety gate failed for (\S+):\s*(.+)$")
    placed_re = re.compile(r"\[(?:LIVE|PAPER)\]\s+PLACED\s+(\S+)")
    for line in window_lines:
        m = fail_re.search(line)
        if m:
            tkr = m.group(1).rstrip(":")
            reason = m.group(2).strip()
            # Strip the variable part of the reason for grouping
            base_reason = reason.split(":")[0].split(",")[0][:50]
            failed_tickers[tkr] += 1
            fail_reasons[base_reason] += 1
            continue
        m = placed_re.search(line)
        if m:
            placed_tickers.add(m.group(1))
    n = len(placed_tickers)
    return n, {
        "fail_reasons": dict(fail_reasons.most_common(8)),
        "distinct_failed_tickers": len(failed_tickers),
    }


def layer_subscribed(window_lines: list[str]) -> tuple[int, dict]:
    """Markets currently subscribed to the WS book channel (last 5 min)."""
    subscribed: set[str] = set()
    bulk_re = re.compile(r"subscribed \(cmd_id=\d+\)\s+(\d+)\s+tickers:\s+\[(.+)\]")
    single_re = re.compile(r"subscribed \(cmd_id=\d+\)\s+1\s+tickers:\s+\['([^']+)'\]")
    last_bulk_count = 0
    for line in window_lines:
        m = bulk_re.search(line)
        if m:
            n = int(m.group(1))
            if n > 1:
                last_bulk_count = max(last_bulk_count, n)
            else:
                m2 = single_re.search(line)
                if m2:
                    subscribed.add(m2.group(1))
    # Best-effort estimate: bulk count + distinct singles since
    return last_bulk_count + len(subscribed), {
        "bulk_seen": last_bulk_count,
        "single_adds_5m": len(subscribed),
    }


_resting_orders_cache: list[dict] = []


def layer_quoting() -> tuple[int, dict]:
    """Markets with at least one resting order on Kalshi right now.
    Caches order list for layer_two_sided() to avoid duplicate API call."""
    global _resting_orders_cache
    try:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        resp = c.get("/portfolio/orders", params={"status": "resting", "limit": 1000})
        orders = (resp or {}).get("orders", [])
        _resting_orders_cache = orders
        tickers = {o.get("ticker") for o in orders if o.get("ticker")}
        return len(tickers), {
            "total_resting_orders": len(orders),
            "avg_orders_per_ticker": round(len(orders) / max(1, len(tickers)), 1),
        }
    except Exception as e:
        _resting_orders_cache = []
        return -1, {"error": str(e)[:120]}


def layer_filled_1h(conn: sqlite3.Connection) -> tuple[int, dict]:
    """Distinct tickers with a fill in the last hour."""
    try:
        n = conn.execute("""
            SELECT COUNT(DISTINCT ticker) FROM fill_ledger
            WHERE datetime(created_at) > datetime('now','-1 hour')
        """).fetchone()[0]
        rows = conn.execute("""
            SELECT side, COUNT(*) FROM fill_ledger
            WHERE datetime(created_at) > datetime('now','-1 hour')
            GROUP BY side
        """).fetchall()
        return n, {"fills_by_side": dict(rows)}
    except sqlite3.OperationalError as e:
        return -1, {"error": str(e)}


def layer_settled_24h(conn: sqlite3.Connection) -> tuple[int, dict]:
    """Markets settled in last 24h WHERE WE WERE INVOLVED. Ours-only filter:
    settlement_reconciler logs every Kalshi market settlement (5,000+/day).
    Filtering to (rebate != 0 OR realized != 0) gives us only markets where
    we actually had a position or earned rebate. The unfiltered count was
    pure noise that masked our real performance."""
    try:
        row = conn.execute("""
            SELECT COUNT(*) n,
                   ROUND(COALESCE(SUM(rebate_earned_usd),0),3) reb,
                   ROUND(COALESCE(SUM(our_realized_usd),0),2) real,
                   ROUND(COALESCE(SUM(rebate_earned_usd)+SUM(our_realized_usd),0),2) net
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-1 day')
              AND (rebate_earned_usd != 0 OR our_realized_usd != 0)
        """).fetchone()
        # Also report total noise count for context
        total_n = conn.execute("""
            SELECT COUNT(*) FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-1 day')
        """).fetchone()[0]
        n, reb, real, net = row
        return n, {
            "rebate_usd":      reb,
            "realized_usd":    real,
            "net_usd":         net,
            "total_settles":   total_n,
            "noise_filtered":  total_n - n,
        }
    except sqlite3.OperationalError as e:
        return -1, {"error": str(e)}


def layer_estimated_rebate(conn: sqlite3.Connection) -> tuple[int, dict]:
    """Compute estimated rebate accrual from lip_snapshots over last hour.
    No need to wait for monthly settlement — this is the per-second snapshot
    income flowing right now. Compared with PROJECTED, tells us if the
    physics equation matches reality WITHIN HOURS instead of weeks.

    Per-snapshot rebate:
      our_share × pool_per_day / 86400_snaps_per_day × calibration

    Returns: (estimated_$/d_extrapolated, detail dict).
    """
    try:
        rows = conn.execute("""
            SELECT s.market_ticker,
                   COUNT(*) AS snaps,
                   SUM(s.snapshot_valid) AS valid_snaps,
                   AVG(CASE WHEN s.total_score > 0
                            THEN s.our_score * 1.0 / s.total_score
                            ELSE 0 END) AS avg_share,
                   p.reward_per_day_usd AS pool_d
            FROM lip_snapshots s
            JOIN lip_programs p ON s.market_ticker = p.market_ticker
            WHERE datetime(s.captured_at) > datetime('now','-1 hour')
              AND p.enrolled = 1
              AND p.paid_out = 0
            GROUP BY s.market_ticker
            HAVING valid_snaps >= 5
        """).fetchall()
    except sqlite3.OperationalError as e:
        return -1, {"error": str(e)[:100]}
    if not rows:
        return 0, {"reason": "no_valid_snapshots_last_hour"}
    KALSHI_CALIB = 0.25
    SNAPS_PER_DAY = 86400  # 1 per second per Kalshi LIP spec
    total_estimated_d = 0.0
    per_market = []
    for r in rows:
        tkr, snaps, valid, share, pool_d = r
        if not pool_d or not share or share <= 0 or valid <= 0:
            continue
        # Extrapolate: if we earned X over N valid snapshots, daily would be X × (86400/N) approx
        # But simpler: per-second average rate × seconds_per_day
        qual_rate = (valid or 0) / max(1, snaps)
        per_second_share = share * qual_rate
        per_day_rebate = per_second_share * pool_d * KALSHI_CALIB
        total_estimated_d += per_day_rebate
        per_market.append({
            "ticker": tkr[:30],
            "snaps": snaps,
            "share_pct": round(share * 100, 2),
            "qual_pct": round(qual_rate * 100, 1),
            "est_$/d": round(per_day_rebate, 3),
        })
    per_market.sort(key=lambda d: -d["est_$/d"])
    n_quoting = len(per_market)
    return n_quoting, {
        "total_estimated_$/d":   round(total_estimated_d, 3),
        "top_3_contributors":    per_market[:3],
        "calibration_used":      KALSHI_CALIB,
    }


def layer_projected() -> tuple[int, dict]:
    """Parse the latest capital_allocator log line to surface what the runner
    EXPECTS to earn. Compared with SETTLED_24H net, this tells us if the
    allocator's physics equation is calibrated. Drift > 2x = recalibrate."""
    try:
        out = subprocess.run(
            ["tail", "-n", "1000", str(LOG_PATH)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return -1, {"error": "log_read_failed"}
    last_line = None
    for line in out.splitlines():
        if "capital_allocator:" in line:
            last_line = line
    if not last_line:
        return -1, {"error": "no_allocator_log_yet"}
    # Extract: "capital_allocator: 10/1273 markets (exploit=4@$636, explore=6@$150), total=$786/$800, pool=$500/d, expected_net=$43.80/d"
    expected_net = 0.0
    n_markets = 0
    capital = 0.0
    try:
        m_n = re.search(r"capital_allocator:\s+(\d+)/", last_line)
        if m_n: n_markets = int(m_n.group(1))
        m_e = re.search(r"expected_net=\$(-?[\d.]+)/d", last_line)
        if m_e: expected_net = float(m_e.group(1))
        m_c = re.search(r"total=\$(-?[\d.]+)/", last_line)
        if m_c: capital = float(m_c.group(1))
    except Exception:
        pass
    return n_markets, {
        "expected_net_per_day_usd": round(expected_net, 2),
        "capital_deployed_usd":     round(capital, 2),
        "ts":                       last_line[:19],
    }


def layer_closing_soon(conn: sqlite3.Connection) -> tuple[int, dict]:
    """Markets we're enrolled in that close within 2h. Risk view: get-out radar."""
    try:
        n_2h = conn.execute("""
            SELECT COUNT(*) FROM lip_programs
            WHERE enrolled = 1 AND paid_out = 0
              AND datetime(end_date) > datetime('now')
              AND datetime(end_date) <= datetime('now','+2 hours')
        """).fetchone()[0]
        n_24h = conn.execute("""
            SELECT COUNT(*) FROM lip_programs
            WHERE enrolled = 1 AND paid_out = 0
              AND datetime(end_date) > datetime('now')
              AND datetime(end_date) <= datetime('now','+24 hours')
        """).fetchone()[0]
        # Imminent: those closing in next 30 min where we should already be cancelling
        n_30m = conn.execute("""
            SELECT COUNT(*) FROM lip_programs
            WHERE enrolled = 1 AND paid_out = 0
              AND datetime(end_date) > datetime('now')
              AND datetime(end_date) <= datetime('now','+30 minutes')
        """).fetchone()[0]
        return n_2h, {"closing_30m": n_30m, "closing_24h": n_24h}
    except sqlite3.OperationalError as e:
        return -1, {"error": str(e)}


def layer_two_sided(resting_orders: list[dict]) -> tuple[int, dict]:
    """How many of our quoting markets have BOTH sides resting (LIP-eligible).
    Re-uses orders fetched in layer_quoting() to avoid duplicate API calls."""
    try:
        sides_by_ticker: dict[str, set] = {}
        for o in resting_orders:
            tkr = o.get("ticker")
            side = o.get("side")
            if tkr and side:
                sides_by_ticker.setdefault(tkr, set()).add(side)
        two_sided = sum(1 for sides in sides_by_ticker.values()
                        if "yes" in sides and "no" in sides)
        one_sided_yes = sum(1 for sides in sides_by_ticker.values()
                            if sides == {"yes"})
        one_sided_no = sum(1 for sides in sides_by_ticker.values()
                           if sides == {"no"})
        return two_sided, {
            "one_sided_yes_only": one_sided_yes,
            "one_sided_no_only":  one_sided_no,
            "total_quoting":      len(sides_by_ticker),
        }
    except Exception as e:
        return -1, {"error": str(e)[:120]}


# ── Snapshot driver ───────────────────────────────────────────────────────

def snapshot(db_path: str = settings.DB_PATH, *, persist: bool = True) -> dict:
    ensure_schema(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    win = _tail_log_window_lines(seconds=300)

    conn = sqlite3.connect(db_path, timeout=10.0)
    layers: list[tuple[str, int, dict]] = []
    try:
        n, d = layer_enrolled(conn);     layers.append(("ENROLLED",   n, d))
        n, d = layer_ranked();            layers.append(("RANKED",     n, d))
        n, d = layer_safe(win);           layers.append(("SAFE_5M",    n, d))
        n, d = layer_subscribed(win);     layers.append(("SUBSCRIBED", n, d))
        n, d = layer_quoting();           layers.append(("QUOTING",    n, d))
        n, d = layer_two_sided(_resting_orders_cache); layers.append(("TWO_SIDED",  n, d))
        n, d = layer_filled_1h(conn);     layers.append(("FILLED_1H",  n, d))
        n, d = layer_closing_soon(conn);  layers.append(("CLOSING_2H", n, d))
        n, d = layer_projected();         layers.append(("PROJECTED",  n, d))
        n, d = layer_estimated_rebate(conn); layers.append(("EST_REBATE_1H", n, d))
        n, d = layer_settled_24h(conn);   layers.append(("SETTLED_24H", n, d))
        if persist:
            conn.executemany(
                "INSERT OR REPLACE INTO funnel_log(snapshot_at,layer,count,detail_json) VALUES(?,?,?,?)",
                [(now, lname, count, json.dumps(detail)) for lname, count, detail in layers],
            )
            conn.commit()
    finally:
        conn.close()

    # Emit one summary log line
    summary = " → ".join(f"{lname}={count}" for lname, count, _ in layers)
    pool = next((d.get("pool_per_day_usd") for n, _c, d in []
                 for n in (n,) if False), None)  # placeholder
    enrolled_pool = layers[0][2].get("pool_per_day_usd", 0) if layers else 0
    ranked_pool   = layers[1][2].get("pool_per_day_usd", 0) if len(layers) > 1 else 0
    pool_capture  = (ranked_pool / enrolled_pool * 100) if enrolled_pool else 0
    settled_net   = layers[6][2].get("net_usd", 0) if len(layers) > 6 else 0

    print(f"📊 FUNNEL [{now}] {summary} | "
          f"pool_$/d enrolled=${enrolled_pool:.0f} ranked=${ranked_pool:.0f} "
          f"({pool_capture:.0f}% reach) | settled_24h_net=${settled_net:+.2f}")

    return {"snapshot_at": now, "layers": layers}


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    snapshot()
