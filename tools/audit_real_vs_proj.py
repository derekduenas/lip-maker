"""BRUTAL AUDIT — real rebates vs projected MONEY_PRINT.

Shows truth: are we earning what the system projects?
Decomposes:
  1. Avg projected daily ($/d from MONEY_PRINT log)
  2. Actual daily realized rebates (from settlement_log)
  3. Ratio = actual / projected (should be ~1.0; if 0.3, we're getting 30%)
  4. Where the gap is (calibration vs fill rate)
"""
import sqlite3, re, subprocess
from collections import defaultdict
from datetime import datetime, timedelta

DB = "/root/lip-maker/data/lip_maker.db"
LOG = "/root/lip-maker/logs/lip-maker.log"

# 1. Pull MONEY_PRINT history from log (last 14 days)
print("=" * 80)
print("AUDIT: PROJECTED vs ACTUAL — LIP rebates")
print("=" * 80)

# Get MONEY_PRINT projections grouped by day
proj_by_day = defaultdict(list)
try:
    out = subprocess.run(
        ["grep", "-h", "MONEY_PRINT", LOG],
        capture_output=True, text=True
    ).stdout
    for line in out.split("\n"):
        # Format: "2026-05-08 12:54:14 ... proj_daily=$XX.XX"
        m_ts = re.match(r"^(\d{4}-\d{2}-\d{2})", line)
        m_proj = re.search(r"proj_daily=\$([\d.]+)", line)
        if m_ts and m_proj:
            day = m_ts.group(1)
            proj_by_day[day].append(float(m_proj.group(1)))
except Exception as e:
    print(f"log parse err: {e}")

# 2. Pull ACTUAL rebates from settlement_log (last 14 days)
conn = sqlite3.connect(DB)
actual_by_day = {}
rows = conn.execute("""
    SELECT date(close_time) AS day,
           ROUND(SUM(rebate_earned_usd), 2) AS rebates,
           ROUND(SUM(net_outcome_usd), 2) AS net,
           COUNT(*) AS settles
    FROM settlement_log
    WHERE datetime(close_time) > datetime('now','-14 days')
    GROUP BY day ORDER BY day DESC
""").fetchall()
for r in rows:
    actual_by_day[r[0]] = (r[1], r[2], r[3])

# 3. Compare
print(f"\n{'Day':12} {'Proj $/d (avg)':>16} {'Actual rebate':>14} {'Net':>10} {'Ratio':>7}")
print("-" * 70)
total_proj = 0
total_actual = 0
for day in sorted(set(list(proj_by_day.keys()) + list(actual_by_day.keys())), reverse=True)[:14]:
    avg_proj = sum(proj_by_day.get(day, [0])) / max(len(proj_by_day.get(day, [1])), 1) if proj_by_day.get(day) else 0
    actual_rebate, actual_net, n_settles = actual_by_day.get(day, (0, 0, 0))
    ratio = actual_rebate / avg_proj if avg_proj > 0 else 0
    total_proj += avg_proj
    total_actual += actual_rebate
    print(f"  {day} ${avg_proj:>14.2f} ${actual_rebate:>12.2f} ${actual_net:>+8.2f} {ratio*100:>5.0f}%")

print("-" * 70)
overall_ratio = total_actual / total_proj * 100 if total_proj > 0 else 0
print(f"  TOTALS    ${total_proj:>14.2f} ${total_actual:>12.2f}  ratio={overall_ratio:.0f}%")

# 4. Order fill rate (resting → filled %)
print(f"\n{'='*80}")
print("FILL RATE — orders placed → filled %")
print("=" * 80)
fr = conn.execute("""
    SELECT
        DATE(placed_at) AS day,
        COUNT(*) AS placed,
        SUM(CASE WHEN status IN ('filled','partially_filled') THEN 1 ELSE 0 END) AS filled,
        SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
        SUM(CASE WHEN status = 'resting' THEN 1 ELSE 0 END) AS resting
    FROM quotes
    WHERE paper=0 AND placed_at > datetime('now','-7 days')
    GROUP BY DATE(placed_at) ORDER BY day DESC
""").fetchall()
print(f"\n{'Day':12} {'Placed':>7} {'Filled':>7} {'Cancelled':>10} {'Resting':>9} {'Fill%':>6}")
print("-" * 60)
for r in fr:
    fill_pct = (r[2] / r[1] * 100) if r[1] else 0
    print(f"  {r[0]:10} {r[1]:>7} {r[2]:>7} {r[3]:>10} {r[4]:>9} {fill_pct:>5.1f}%")

# 5. Per-series rebate efficiency
print(f"\n{'='*80}")
print("PER-SERIES RENT — what's actually paying")
print("=" * 80)
ps = conn.execute("""
    SELECT series_prefix,
           COUNT(*) AS n_settles,
           ROUND(SUM(rebate_earned_usd), 2) AS rebate_total,
           ROUND(SUM(our_realized_usd), 2) AS realized,
           ROUND(SUM(net_outcome_usd), 2) AS net,
           ROUND(SUM(rebate_earned_usd)*1.0/COUNT(*), 3) AS rebate_per_settle
    FROM settlement_log
    WHERE datetime(close_time) > datetime('now','-7 days')
    GROUP BY series_prefix
    HAVING rebate_total > 0
    ORDER BY rebate_total DESC LIMIT 15
""").fetchall()
print(f"\n{'Series':25} {'n':>4} {'Rebate':>9} {'Realized':>10} {'Net':>8} {'$/settle':>9}")
print("-" * 75)
for r in ps:
    print(f"  {r[0]:23} {r[1]:>4} ${r[2]:>7.2f} ${r[3]:>+8.2f} ${r[4]:>+6.2f} ${r[5]:>7.3f}")

conn.close()

print("\n" + "=" * 80)
print("INTERPRETATION:")
print(f"  - If RATIO < 50%: projection is wildly inflated, real income is much less")
print(f"  - If RATIO > 70%: projection is roughly accurate")
print(f"  - If FILL% < 10%: orders aren't filling, rebates accruing only on resting time")
print(f"  - If FILL% > 50%: orders cross frequently (good for income, bad for adverse selection)")
