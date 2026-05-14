"""Pool Depletion Estimator (Sonnet's #3 ROI fix)

Kalshi doesn't expose pool_remaining. We estimate:
  pool_paid_so_far = elapsed_days × reward_per_day
  pool_remaining_pct = (total - paid) / total

Strategy boosts:
- pool_remaining_pct < 30%: BOOST 1.5x size (late-period, less competition)
- pool_remaining_pct > 80%: REDUCE 0.5x size (fresh, lots of competition)
- 30-80%: normal size

Output: write to runtime_overrides table that quote_manager respects.

Run via cron every 30 min.
"""
from __future__ import annotations

# === heartbeat (auto-injected, atexit) ===
import atexit as _atexit, sys as _sys
_sys.path.insert(0, "/root/lip-maker")
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "pool_depletion")
except Exception:
    pass

import sqlite3, json
from datetime import datetime, timezone, timedelta

DB = "/root/lip-maker/data/lip_maker.db"

def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT market_ticker, series_ticker, reward_per_day_usd,
               period_reward_usd, start_date, end_date
        FROM lip_programs
        WHERE enrolled = 1 AND paid_out = 0
          AND datetime(end_date) > datetime('now')
          AND period_reward_usd > 0
    """).fetchall()

    # 2026-05-09 SPRINT 3A: Correlation cap.
    # Sonnet flagged 2,802 simultaneous boosts = systemic over-leverage.
    # Sort late-period candidates by reward_per_day, only boost top N.
    MAX_BOOSTS = 50  # cap to top 50 highest-reward late-period markets
    MAX_REDUCES = 100
    candidates = []  # (boost_or_reduce, mult, label, tk, rwd)
    for tk, series, rwd_d, total_rwd, start, end in rows:
        try:
            ts = datetime.fromisoformat(start.replace("Z", "+00:00"))
            te = datetime.fromisoformat(end.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            total_period_days = max(0.5, (te - ts).total_seconds() / 86400)
            elapsed_days = max(0, (now - ts).total_seconds() / 86400)
            pct_elapsed = min(1.0, elapsed_days / total_period_days)
            pct_remaining = 1.0 - pct_elapsed
        except Exception:
            continue
        if pct_remaining < 0.30:
            candidates.append(("boost", 1.5, f"depletion_boost:{int(pct_remaining*100)}%left", tk, rwd_d or 0))
        elif pct_remaining > 0.80:
            candidates.append(("reduce", 0.5, f"depletion_reduce:{int(pct_remaining*100)}%left_fresh", tk, rwd_d or 0))

    # Sort boosts by reward DESC, take top N. Same for reduces.
    boosts_only = sorted([c for c in candidates if c[0] == "boost"], key=lambda x: -x[4])[:MAX_BOOSTS]
    reduces_only = sorted([c for c in candidates if c[0] == "reduce"], key=lambda x: -x[4])[:MAX_REDUCES]
    final_overrides = boosts_only + reduces_only

    boosted = 0
    reduced = 0
    for action, mult, label, tk, _rwd in final_overrides:
        if action == "boost":
            boosted += 1
        else:
            reduced += 1

        conn.execute("""
            INSERT OR REPLACE INTO runtime_overrides
              (series_ticker, override_type, value_json, expires_at, created_by, created_at)
            VALUES (?, 'size_mult', ?, ?, 'pool_depletion', ?)
        """, (
            tk, json.dumps({"multiplier": mult, "reason": label}),
            (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),  # 30 min
            datetime.now(timezone.utc).isoformat()
        ))

    conn.commit()
    conn.close()
    print(f"Pool depletion: boosted {boosted} (late-period), reduced {reduced} (fresh)")


if __name__ == "__main__":
    main()
