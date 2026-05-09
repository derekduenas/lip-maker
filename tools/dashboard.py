"""INNAIT Kalshi LIP Dashboard — single command, real numbers.

Pulls TRUTH from Kalshi API (not local DB sums for exposure) and combines
with local settlement_log + vip_accrual + daily_pnl for a complete picture.

USAGE:
  python tools/dashboard.py            # full view
  python tools/dashboard.py --pnl      # just PnL lines
  python tools/dashboard.py --orders   # just current orders by series
  python tools/dashboard.py --json     # machine-readable

#110 FIX: exposure metric now comes from /portfolio/positions
market_exposure_dollars (Kalshi-side truth), not local fill_ledger sums
that double-count cumulative cost.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def _kalshi_state() -> dict:
    """Pull current resting orders + positions + balance from Kalshi."""
    from execution.kalshi_auth import KalshiClient
    c = KalshiClient()

    # Resting orders
    orders = []
    cursor = None
    while True:
        params = {"status": "resting", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = c.get("/portfolio/orders", params=params)
        except Exception as e:
            return {"error": f"orders fetch failed: {e}"}
        for o in r.get("orders", []):
            if o.get("status") == "resting":
                orders.append(o)
        cursor = r.get("cursor")
        if not cursor:
            break

    # Open positions (with realized PnL on each)
    positions = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = c.get("/portfolio/positions", params=params)
        except Exception as e:
            return {"error": f"positions fetch failed: {e}"}
        positions.extend(r.get("market_positions", []))
        cursor = r.get("cursor")
        if not cursor:
            break

    # Balance
    try:
        balance = c.get_balance()
    except Exception as e:
        balance = None

    return {"orders": orders, "positions": positions, "balance": balance}


def _local_state(db_path: str = settings.DB_PATH) -> dict:
    """Pull last 7d aggregates from local DB."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # PnL trajectory last 7 days
        pnl_rows = conn.execute(
            """SELECT day, realized_pnl_usd, daily_realized_delta,
                      open_positions, balance_usd
               FROM daily_pnl_log
               WHERE day >= date('now', '-7 days')
               ORDER BY day"""
        ).fetchall()

        # Settlements last 7d by series
        settle_rows = conn.execute(
            """SELECT series_prefix, COUNT(*) AS n,
                      ROUND(SUM(our_realized_usd), 2) AS realized,
                      ROUND(SUM(rebate_earned_usd), 2) AS rebate,
                      ROUND(SUM(net_outcome_usd), 2) AS net
               FROM settlement_log
               WHERE close_time > ?
               GROUP BY series_prefix
               HAVING ABS(net) > 0.01
               ORDER BY net DESC""",
            (cutoff,),
        ).fetchall()

        # VIP last 7d
        try:
            vip_total = conn.execute(
                """SELECT COALESCE(SUM(accrued_vip_usd), 0)
                   FROM vip_accrual_log
                   WHERE day >= date('now', '-7 days')"""
            ).fetchone()[0]
        except sqlite3.OperationalError:
            vip_total = 0

        # Active blacklist
        bl_count = conn.execute(
            """SELECT COUNT(*) FROM market_blacklist
               WHERE datetime(expires_at) > datetime('now')"""
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "pnl_trajectory": pnl_rows,
        "settlements_7d": settle_rows,
        "vip_7d":         round(float(vip_total or 0), 2),
        "active_blacklist": bl_count,
    }


def render(k: dict, l: dict) -> None:
    """Pretty-print combined dashboard."""
    if k.get("error"):
        print(f"\n🔴 KALSHI API ERROR: {k['error']}")
        return

    orders = k["orders"]
    positions = k["positions"]
    balance = k["balance"]

    # ── Header ──
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  INNAIT KALSHI LIP DASHBOARD — {now}")
    print(f"══════════════════════════════════════════════════════════════")

    # ── Account ──
    print(f"\n📊 ACCOUNT")
    print(f"  Balance:      ${balance:.2f}" if balance is not None else "  Balance: (fetch failed)")
    open_realized = sum(float(p.get("realized_pnl_dollars", 0) or 0) for p in positions)
    exposure = sum(float(p.get("market_exposure_dollars", 0) or 0) for p in positions)
    print(f"  Open exposure (Kalshi truth): ${exposure:.2f}")
    print(f"  Open positions: {sum(1 for p in positions if float(p.get('position_fp',0) or 0) != 0)}")
    print(f"  Open realized P&L: ${open_realized:+.2f}")

    # ── Resting orders ──
    print(f"\n📋 RESTING ORDERS ({len(orders)} total)")
    by_series = defaultdict(lambda: {"orders": 0, "tickers": set()})
    for o in orders:
        t = o.get("ticker", "")
        s = t.split("-", 1)[0] if "-" in t else t
        by_series[s]["orders"] += 1
        by_series[s]["tickers"].add(t)
    for s, d in sorted(by_series.items(), key=lambda kv: -kv[1]["orders"]):
        print(f"  {s:<28s}  {d['orders']:>3} orders / {len(d['tickers']):>2} markets")

    # ── 7-day PnL ──
    print(f"\n💰 PnL LAST 7 DAYS")
    pnl = l["pnl_trajectory"]
    if pnl:
        print(f"  {'day':<10s}  {'cum_realized':>12s}  {'daily_Δ':>10s}  {'positions':>9s}  {'balance':>8s}")
        for day, realized, delta, n_pos, bal in pnl:
            # AUDIT FIX: realized may be None if daily_pnl_log row was
            # incomplete; default to 0 for display.
            print(f"  {day:<10s}  ${realized or 0:>+11.2f}  ${delta or 0:>+9.2f}  {n_pos or 0:>9d}  ${bal or 0:>7.2f}")
        weekly_delta = sum(d[2] or 0 for d in pnl[-7:])
        print(f"  {'─'*60}")
        print(f"  Weekly Δ realized: ${weekly_delta:+.2f}")
    else:
        print("  (no daily_pnl_log entries yet)")

    # ── Per-series settlement (last 7d) ──
    print(f"\n📈 SETTLEMENT NET (last 7 days, top 10)")
    settles = l["settlements_7d"][:10]
    if settles:
        print(f"  {'series':<25s}  {'n':>3s}  {'realized':>10s}  {'rebate':>8s}  {'NET':>8s}")
        weekly_settle = 0
        weekly_rebate = 0
        for s, n, r, reb, net in settles:
            weekly_settle += r or 0
            weekly_rebate += reb or 0
            print(f"  {s:<25s}  {n:>3d}  ${r or 0:>+9.2f}  ${reb or 0:>+7.2f}  ${net or 0:>+7.2f}")
        # Include all (not just top 10) in totals
        all_settles = l["settlements_7d"]
        total_net = sum(d[4] or 0 for d in all_settles)
        total_rebate = sum(d[3] or 0 for d in all_settles)
        total_realized = sum(d[2] or 0 for d in all_settles)
        print(f"  {'─'*60}")
        print(f"  Total 7d:  realized=${total_realized:+.2f}  rebate=${total_rebate:+.2f}  NET=${total_net:+.2f}")
    else:
        print("  (no settlements in last 7 days)")

    # ── VIP ──
    print(f"\n🎟️ VIP REBATE (Volume Incentive, last 7d)")
    print(f"  ${l['vip_7d']:.2f}")

    # ── Blacklist ──
    print(f"\n🛑 ACTIVE BLACKLIST: {l['active_blacklist']} markets")

    _aegis_brain_recommendations("/root/lip-maker/data/lip_maker.db")

    print()



def _aegis_brain_recommendations(db_path: str) -> None:
    """AEGIS latest brain decision + recommendations + needs-approval items."""
    import sqlite3, json as _json
    print("\n🛡️  AEGIS BRAIN — latest cycle:")
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT ts, trigger_type, reasoning, actions_proposed, actions_executed, cost_usd "
            "FROM decision_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception as e:
        print(f"  (brain log unavailable: {e})")
        return
    if not row:
        print("  (no brain cycles yet)")
        return
    ts, trigger, reasoning, actions_p, actions_e, cost = row
    print(f"  ts: {ts[:19]}  trigger: {trigger}  cost: ${cost:.4f}")
    # Try parse reasoning JSON for diagnosis
    try:
        r = _json.loads(reasoning) if reasoning else {}
        diag = r.get("diagnosis", "")[:280]
        if diag:
            print(f"  diagnosis: {diag}")
        confidence = r.get("confidence", "")
        if confidence:
            print(f"  confidence: {confidence}")
    except Exception:
        if reasoning:
            print(f"  reasoning: {reasoning[:280]}")
    try:
        executed = _json.loads(actions_e or "[]")
        proposed = _json.loads(actions_p or "[]")
        if executed:
            names = [a.get("action") or a.get("step") for a in executed if isinstance(a, dict)]
            print(f"  executed: {names}")
        unexec = [a for a in proposed if isinstance(a, dict) and (
            a.get("safety") == "HUMAN-APPROVE" or
            a.get("action") not in [e.get("action") for e in executed if isinstance(e, dict)]
        )]
        if unexec:
            print("  ⚡ AEGIS RECOMMENDATIONS (needs your approval):")
            for a in unexec[:3]:
                desc = a.get("expected_value") or a.get("details") or a.get("expected_impact") or ""
                act = a.get("action", "?")
                print(f"    - {act}: {str(desc)[:160]}")
    except Exception:
        pass


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args()

    # AUDIT FIX: chdir at main() start (was module-level — would mutate cwd
    # of any future importer). KalshiClient .env loader needs cwd here.
    os.chdir(str(Path(__file__).resolve().parent.parent))

    k = _kalshi_state()
    l = _local_state()

    if a.json:
        # Make Kalshi state JSON-serializable (positions/orders are dicts, but
        # only emit summary fields)
        out = {
            "kalshi": {
                "balance": k.get("balance"),
                "n_resting": len(k.get("orders", [])),
                "n_positions": sum(
                    1 for p in k.get("positions", [])
                    if float(p.get("position_fp", 0) or 0) != 0
                ),
                "exposure_usd": sum(
                    float(p.get("market_exposure_dollars", 0) or 0)
                    for p in k.get("positions", [])
                ),
                "open_realized_pnl_usd": sum(
                    float(p.get("realized_pnl_dollars", 0) or 0)
                    for p in k.get("positions", [])
                ),
            },
            "local": {
                "pnl_trajectory": [
                    {"day": r[0], "realized": r[1], "delta": r[2],
                     "positions": r[3], "balance": r[4]} for r in l["pnl_trajectory"]
                ],
                "vip_7d_usd": l["vip_7d"],
                "active_blacklist_count": l["active_blacklist"],
            },
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        render(k, l)

    return 0


if __name__ == "__main__":
    sys.exit(main())
