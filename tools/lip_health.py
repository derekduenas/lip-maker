"""LIP health scorecard — `python tools/lip_health.py` shows everything
that matters in a single screen. Pure measurement, no behavior changes.

Designed for SSH-then-glance ops. Run after a config tweak to see what
moved. Each section flags concrete TODO items if the metric is off.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "/root/lip-maker/data/lip_maker.db"
LOG_PATH = "/root/lip-maker/logs/lip_maker.log"


# ── helpers ──────────────────────────────────────────────────────────────────

def _q(sql: str, params: tuple = ()) -> list:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0) as c:
        return c.execute(sql, params).fetchall()


def _q1(sql: str, params: tuple = ()) -> tuple | None:
    rows = _q(sql, params)
    return rows[0] if rows else None


def _systemctl_active(svc: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", svc],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _systemctl_uptime(svc: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", svc],
            capture_output=True, text=True, timeout=3,
        )
        ts = r.stdout.strip()
        if not ts or ts == "n/a":
            return "?"
        # parse e.g. "Fri 2026-05-16 14:09:05 UTC"
        from datetime import datetime as dt
        for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S UTC"):
            try:
                t = dt.strptime(ts, fmt)
                t = t.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - t
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                return f"{h}h{m:02d}m"
            except ValueError:
                continue
        return ts
    except Exception:
        return "?"


def _tail_log_money_prints(n: int = 30) -> list[float]:
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 500_000))
            tail = f.read().decode(errors="replace")
        vals = re.findall(r"proj_daily=\$([\d.]+)", tail)
        return [float(v) for v in vals[-n:]]
    except FileNotFoundError:
        return []


def _bankroll_and_cap() -> dict:
    # Read settings (importing from main script's working dir)
    import os
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import settings
    return {
        "bankroll": getattr(settings, "BANKROLL_USD", 80),
        "max_total_gross": getattr(settings, "MAX_TOTAL_GROSS_USD", 0),
        "max_daily_loss": getattr(settings, "MAX_DAILY_LOSS_USD", 0),
    }


def _quote_manager_state() -> dict:
    """Parse the most recent 'Quote manager:' log line."""
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read().decode(errors="replace")
        # Match the dict in the log line
        m = re.findall(
            r"Quote manager: \{([^}]+)\}",
            tail,
        )
        if not m:
            return {}
        # Parse the python dict-ish line
        out = {}
        for kv in m[-1].split(","):
            kv = kv.strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                k = k.strip().strip("'\"")
                v = v.strip().strip("'\"")
                try:
                    out[k] = float(v) if "." in v else int(v)
                except ValueError:
                    out[k] = v
        return out
    except Exception:
        return {}


# ── audit sections ───────────────────────────────────────────────────────────

def audit_service() -> dict:
    return {
        "lip_maker_active": _systemctl_active("lip-maker.service"),
        "uptime": _systemctl_uptime("lip-maker.service"),
        "dashboard_active": _systemctl_active("lip-dashboard.service"),
        "fills_sync_active": _systemctl_active("fills-sync.timer"),
        "crypto_spot_active": _systemctl_active("crypto-spot-recorder.timer"),
        "graduated_scanners_active": all([
            _systemctl_active("vpin-gate.timer"),
            _systemctl_active("toxicity-filter.timer"),
            _systemctl_active("order-flow-tracker.timer"),
        ]),
    }


def audit_deployment() -> dict:
    cfg = _bankroll_and_cap()
    qm = _quote_manager_state()
    deployed = float(qm.get("total_gross_usd", 0))
    cap = float(cfg["max_total_gross"]) or 2000.0
    return {
        "bankroll_usd": cfg["bankroll"],
        "max_total_gross_usd": cap,
        "max_daily_loss_usd": cfg["max_daily_loss"],
        "currently_deployed_usd": deployed,
        "utilization_pct": round(deployed / cap * 100, 1) if cap else 0,
        "n_markets_with_orders": int(qm.get("n_markets_with_orders", 0)),
        "n_total_orders": int(qm.get("n_total_orders", 0)),
        "paper_mode": qm.get("paper", "?"),
    }


def audit_income() -> dict:
    mps = _tail_log_money_prints(30)
    if not mps:
        return {"median_proj_daily": None, "p10": None, "p90": None,
                "n_samples": 0, "monthly_proj": None}
    return {
        "median_proj_daily": round(statistics.median(mps), 2),
        "p10": round(sorted(mps)[len(mps) // 10] if len(mps) >= 10 else mps[0], 2),
        "p90": round(sorted(mps)[len(mps) * 9 // 10] if len(mps) >= 10 else mps[-1], 2),
        "n_samples": len(mps),
        "monthly_proj": round(statistics.median(mps) * 30, 0),
    }


def audit_selection() -> dict:
    # What is the engine actually quoting in the last 5 minutes?
    actual = _q(
        """SELECT market_ticker, COUNT(*) AS snaps
           FROM lip_snapshots
           WHERE captured_at > datetime('now', '-5 minutes')
             AND snapshot_valid = 1
           GROUP BY market_ticker
           ORDER BY snaps DESC
           LIMIT 20"""
    )
    actually_quoted = {t for t, _ in actual}

    # Top by net_capture * pool_per_day (the "should-quote" universe)
    should = _q(
        """SELECT p.market_ticker,
                  ROUND(p.reward_per_day_usd, 2) AS pool,
                  ROUND(c.calibration, 4) AS net_calib,
                  ROUND(p.reward_per_day_usd * COALESCE(c.calibration, 0.25), 2) AS exp_net
           FROM lip_programs p
           LEFT JOIN market_calibration c
             ON c.key = substr(p.market_ticker, 1, instr(p.market_ticker, '-')-1)
           WHERE p.end_date > datetime('now')
             AND p.reward_per_day_usd >= 5
           ORDER BY exp_net DESC
           LIMIT 25"""
    )
    should_quote = [(t, pool, nc, en) for t, pool, nc, en in should]

    # Mismatch: high-EV markets the engine ISN'T currently quoting
    missing_high_ev = [
        (t, pool, nc, en) for t, pool, nc, en in should_quote[:15]
        if t not in actually_quoted
    ]

    return {
        "currently_quoted_count": len(actually_quoted),
        "should_quote_top15": [
            {"ticker": t, "pool_$": pool, "net_calib": nc, "exp_$/day": en}
            for t, pool, nc, en in should_quote[:10]
        ],
        "high_ev_missing_from_quotes": [
            {"ticker": t, "pool_$": pool, "net_calib": nc, "exp_$/day": en}
            for t, pool, nc, en in missing_high_ev[:10]
        ],
    }


def audit_defense() -> dict:
    active_throttle = _q1(
        "SELECT COUNT(*) FROM market_throttle "
        "WHERE datetime(expires_at) > datetime('now')"
    )[0]
    legacy_blacklist = _q1(
        "SELECT COUNT(*) FROM market_blacklist "
        "WHERE expires_at > datetime('now')"
    )[0]
    # AS_skew log lines
    try:
        r = subprocess.run(
            ["grep", "-c", "AS_skew", LOG_PATH],
            capture_output=True, text=True, timeout=10,
        )
        as_skew_fires = int(r.stdout.strip() or 0)
    except Exception:
        as_skew_fires = 0
    return {
        "active_throttle_rows": active_throttle,
        "legacy_blacklist_active": legacy_blacklist,
        "as_skew_total_fires": as_skew_fires,
    }


def audit_calibration() -> dict:
    total = _q1("SELECT COUNT(*) FROM market_calibration")[0]
    qualified = _q1(
        "SELECT COUNT(*) FROM market_calibration WHERE n_samples >= 5"
    )[0]
    # Series the engine is quoting that DON'T have qualified calibration
    quoting_series = _q(
        """SELECT DISTINCT substr(market_ticker, 1, instr(market_ticker, '-')-1) AS series
           FROM lip_snapshots
           WHERE captured_at > datetime('now', '-5 minutes')"""
    )
    falling_back = []
    for (s,) in quoting_series:
        r = _q1(
            "SELECT n_samples FROM market_calibration WHERE key=?",
            (s,),
        )
        if r is None or int(r[0]) < 5:
            n = int(r[0]) if r else 0
            falling_back.append({"series": s, "n_samples": n})
    return {
        "total_series_with_data": total,
        "qualified_series_n_ge_5": qualified,
        "currently_quoting_falling_back_to_0.25": falling_back[:10],
    }


def audit_hedge() -> dict:
    try:
        from cross_venue.market_match import HEDGE_MAP, hedge_for_ticker
    except Exception:
        return {"error": "could not import HEDGE_MAP"}
    quoted = _q(
        """SELECT DISTINCT market_ticker
           FROM lip_snapshots
           WHERE captured_at > datetime('now', '-5 minutes')"""
    )
    hedge_eligible = [t for (t,) in quoted if hedge_for_ticker(t)]
    return {
        "hedge_map_size": len(HEDGE_MAP),
        "currently_quoted_total": len(quoted),
        "currently_quoted_hedgeable": len(hedge_eligible),
        "hedge_log_open": _q1(
            "SELECT COUNT(*) FROM hedge_log WHERE status='placed' "
            "AND (unwound_at IS NULL OR unwound_at = '')"
        )[0],
        "hedge_log_closed": _q1(
            "SELECT COUNT(*) FROM hedge_log WHERE unwound_at IS NOT NULL"
        )[0],
    }


# ── rendering ────────────────────────────────────────────────────────────────

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _color(condition: bool | str) -> str:
    if condition is True or condition == "green":
        return GREEN + "🟢" + RESET
    if condition == "yellow":
        return YELLOW + "🟡" + RESET
    return RED + "🔴" + RESET


def render(audit: dict, plain: bool = False) -> None:
    if plain:
        print(json.dumps(audit, indent=2, default=str))
        return

    def line(s: str = ""):
        print(s)

    line(f"\n{BOLD}=== LIP HEALTH — {datetime.now(timezone.utc).isoformat(timespec='seconds')} ==={RESET}")

    # Service
    s = audit["service"]
    health = _color(s["lip_maker_active"])
    line(f"\n{BOLD}Service{RESET}  {health}  lip-maker active={s['lip_maker_active']}  uptime={s['uptime']}")
    for k in ("dashboard_active", "fills_sync_active", "crypto_spot_active", "graduated_scanners_active"):
        line(f"  {_color(s[k])} {k}: {s[k]}")

    # Deployment
    d = audit["deployment"]
    util_color = "green" if d["utilization_pct"] >= 70 else ("yellow" if d["utilization_pct"] >= 40 else "red")
    line(f"\n{BOLD}Deployment{RESET}")
    line(f"  bankroll=${d['bankroll_usd']:,.0f}  max_gross=${d['max_total_gross_usd']:,.0f}  daily_loss_cap=${d['max_daily_loss_usd']:,.0f}")
    line(f"  deployed=${d['currently_deployed_usd']:,.2f}  utilization={_color(util_color)}{d['utilization_pct']}%")
    line(f"  markets_with_orders={d['n_markets_with_orders']}  orders={d['n_total_orders']}  paper={d['paper_mode']}")
    if d["utilization_pct"] < 70:
        line(f"  {YELLOW}↳ TODO: investigate why {100-d['utilization_pct']:.0f}% of cap is idle (size floor? top-N? filter chain?){RESET}")

    # Income
    i = audit["income"]
    line(f"\n{BOLD}Income projection{RESET} (last {i['n_samples']} MONEY_PRINTs)")
    if i["median_proj_daily"] is not None:
        line(f"  median=${i['median_proj_daily']:.2f}/day  p10=${i['p10']:.2f}  p90=${i['p90']:.2f}")
        line(f"  monthly_proj=${i['monthly_proj']:,.0f}")

    # Selection
    sel = audit["selection"]
    line(f"\n{BOLD}Selection{RESET}  currently_quoting={sel['currently_quoted_count']} markets")
    line(f"  top-10 markets by expected $/day (pool × net_calib):")
    for r in sel["should_quote_top15"][:10]:
        nc_str = f"{r['net_calib']:.4f}" if r['net_calib'] is not None else "  n/a "
        line(f"    ${r['exp_$/day']:>6.2f}/d  net_calib={nc_str}  {r['ticker']}")
    if sel["high_ev_missing_from_quotes"]:
        line(f"  {RED}↳ HIGH-EV markets engine is NOT quoting:{RESET}")
        for r in sel["high_ev_missing_from_quotes"]:
            nc_str = f"{r['net_calib']:.4f}" if r['net_calib'] is not None else "  n/a "
            line(f"    ${r['exp_$/day']:>6.2f}/d  net_calib={nc_str}  {r['ticker']}")
        line(f"  {YELLOW}↳ TODO: investigate why ranker skips these{RESET}")

    # Defense
    df = audit["defense"]
    line(f"\n{BOLD}Defense{RESET}")
    line(f"  active throttle rows: {df['active_throttle_rows']}  (populated by toxicity events; paper has no fills → 0 expected)")
    line(f"  legacy blacklist active: {df['legacy_blacklist_active']}  (decays as TTL expires)")
    line(f"  AS_skew total fires: {df['as_skew_total_fires']}  {'(0 = needs heavier inventory to trip 1c threshold)' if df['as_skew_total_fires'] == 0 else ''}")

    # Calibration
    cal = audit["calibration"]
    line(f"\n{BOLD}Calibration{RESET}")
    line(f"  total series with data: {cal['total_series_with_data']}")
    line(f"  qualified (n_samples ≥ 5, ranker uses learned): {cal['qualified_series_n_ge_5']}")
    if cal["currently_quoting_falling_back_to_0.25"]:
        line(f"  {YELLOW}↳ currently quoting series still on 0.25 prior (need more settles):{RESET}")
        for x in cal["currently_quoting_falling_back_to_0.25"][:5]:
            line(f"    {x['series']:30s} n={x['n_samples']}")

    # Hedge
    h = audit["hedge"]
    if "error" in h:
        line(f"\n{BOLD}Hedge{RESET}  {RED}{h['error']}{RESET}")
    else:
        hedge_pct = h["currently_quoted_hedgeable"] / max(h["currently_quoted_total"], 1) * 100
        line(f"\n{BOLD}Hedge{RESET}")
        line(f"  HEDGE_MAP size: {h['hedge_map_size']} series")
        line(f"  currently quoted: {h['currently_quoted_total']}  of which hedgeable: {h['currently_quoted_hedgeable']} ({hedge_pct:.1f}%)")
        line(f"  hedge_log open positions: {h['hedge_log_open']}  closed (unwound): {h['hedge_log_closed']}")
        if hedge_pct < 20:
            line(f"  {YELLOW}↳ TODO: < 20% of quoted markets are hedgeable. Consider shifting selection.{RESET}")

    line(f"\n{GRAY}run with --json for machine output{RESET}\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true",
                   help="machine-readable output")
    a = p.parse_args()

    audit = {
        "service": audit_service(),
        "deployment": audit_deployment(),
        "income": audit_income(),
        "selection": audit_selection(),
        "defense": audit_defense(),
        "calibration": audit_calibration(),
        "hedge": audit_hedge(),
    }
    render(audit, plain=a.json)


if __name__ == "__main__":
    main()
