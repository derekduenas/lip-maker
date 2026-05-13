"""Resolver for shadow_trades — closes the persistence loop.

Without this, paper executions persist to shadow_trades but their
outcome stays 'OPEN' forever → no PnL, no Brier, no track record.

For each open shadow_trade:
  1. Ask Kalshi /markets/{ticker} for the settled result
  2. If yes/no → mark resolved, compute paper_pnl, compute Brier
  3. Backfill missing estimated_prob from `opportunities` (older rows
     before 2026-05-13 weren't persisting model_p)

PnL math (binary, paper):
    contracts × price = total_cost  (what we put up)
    WIN  → profit = contracts × (1 - price)   (we get $1, paid `price`)
    LOSS → profit = -contracts × price        (we lose what we paid)

Brier (per-trade): (model_p_yes - actual)^2 where actual = 1 if outcome='yes' else 0
  This is the model's calibration on the YES probability of the event.
  Domain-tagged via `domain` column for separate per-prong tracking.

USAGE:
  python -m loop.shadow_resolver               # one cycle
  python -m loop.shadow_resolver --json
  python -m loop.shadow_resolver --backfill-estimated-prob
                                                # fills missing model_p from opportunities

Cron: every 30 min via deploy/sovereign-shadow-resolver.{service,timer}.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

_log = logging.getLogger("shadow_resolver")


SCHEMA_MIGRATIONS = """
-- 2026-05-13: tag rows by domain so per-prong Brier doesn't pool
-- (trump_mention vs fed_mention vs earnings).
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    # Idempotent migrations on shadow_trades
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)")]
    for col, ddl in [
        ("domain",   "ALTER TABLE shadow_trades ADD COLUMN domain TEXT"),
        ("brier",    "ALTER TABLE shadow_trades ADD COLUMN brier REAL"),
    ]:
        if col not in cols:
            conn.execute(ddl)
    conn.commit()
    return conn


# ── Domain inference (from ticker) ──────────────────────────────────────────
def _infer_domain(ticker: str) -> str:
    """Map ticker prefix → domain bucket so per-domain Brier stays clean."""
    t = (ticker or "").upper()
    if "TRUMPMENTION" in t or "TRUMPSAY" in t or "TRUMPFIRE" in t:
        return "trump_mention"
    if "FEDMENTION" in t or "POWELL" in t:
        return "fed_mention"
    if "EARNINGSMENTION" in t or "MENTIONEARN" in t or "_earnings" in t.lower():
        return "earnings_mention"
    if "FOMC" in t:
        return "fomc"
    return "other"


# ── Kalshi market lookup ────────────────────────────────────────────────────
def _market_outcome(client, ticker: str) -> Optional[dict]:
    """Return {'status', 'result'} or None on error."""
    try:
        r = client._get(f"/markets/{ticker}")
    except Exception as e:
        _log.debug(f"market lookup {ticker} failed: {e}")
        return None
    m = r.get("market") or {}
    return {
        "status": m.get("status", ""),
        "result": (m.get("result") or "").lower(),
    }


# ── Paper PnL + Brier ───────────────────────────────────────────────────────
def _paper_pnl(side: str, price: float, contracts: int, outcome: str) -> float:
    """Binary paper P&L in dollars."""
    if price <= 0 or price >= 1 or contracts == 0:
        return 0.0
    side = (side or "").upper()
    won = (outcome == "yes" and side == "YES") or (outcome == "no" and side == "NO")
    if won:
        return round(contracts * (1.0 - price), 4)
    return round(-contracts * price, 4)


def _brier_for_yes_prob(model_p_yes: Optional[float], outcome: str) -> Optional[float]:
    """Standard Brier on the YES probability."""
    if model_p_yes is None:
        return None
    actual = 1.0 if outcome == "yes" else 0.0
    return round((float(model_p_yes) - actual) ** 2, 4)


# ── Backfill helper ─────────────────────────────────────────────────────────
def backfill_estimated_prob(conn: sqlite3.Connection) -> int:
    """For shadow_trades where estimated_prob IS NULL, look it up from
    opportunities by (kalshi_market_id, term)."""
    rows = conn.execute(
        """SELECT id, kalshi_market_id, term FROM shadow_trades
           WHERE estimated_prob IS NULL"""
    ).fetchall()
    n = 0
    for sid, ticker, term in rows:
        opp = conn.execute(
            """SELECT estimated_prob, edge FROM opportunities
               WHERE kalshi_market_id=? AND term=?
               ORDER BY detected_at DESC LIMIT 1""",
            (ticker, term),
        ).fetchone()
        if opp and opp[0] is not None:
            conn.execute(
                "UPDATE shadow_trades SET estimated_prob=?, edge=COALESCE(edge,?) WHERE id=?",
                (opp[0], opp[1], sid),
            )
            n += 1
    conn.commit()
    return n


# ── Main resolution loop ────────────────────────────────────────────────────
def reconcile() -> dict:
    conn = _get_conn()
    try:
        # Tag domain on any row missing it
        rows_to_tag = conn.execute(
            "SELECT id, kalshi_market_id FROM shadow_trades WHERE domain IS NULL"
        ).fetchall()
        for sid, tk in rows_to_tag:
            conn.execute("UPDATE shadow_trades SET domain=? WHERE id=?",
                         (_infer_domain(tk), sid))
        conn.commit()

        rows = conn.execute(
            """SELECT id, kalshi_market_id, term, side, price, contracts,
                      estimated_prob, domain
               FROM shadow_trades WHERE outcome='OPEN'"""
        ).fetchall()
        if not rows:
            return {"checked": 0, "resolved": 0, "still_open": 0,
                    "errors": 0, "tagged_domain": len(rows_to_tag)}

        from engine.scanner import KalshiClient
        client = KalshiClient()
        if not client.authenticated:
            return {"error": "kalshi_unauthenticated"}

        n_resolved = n_open = n_err = 0
        pnl_total = 0.0
        per_domain: dict[str, dict] = {}
        for sid, tk, term, side, price, contracts, est_p, domain in rows:
            info = _market_outcome(client, tk)
            if info is None:
                n_err += 1
                continue
            result = info["result"]
            if result not in ("yes", "no"):
                n_open += 1
                continue
            pnl = _paper_pnl(side, float(price), int(contracts), result)
            brier = _brier_for_yes_prob(est_p, result)
            outcome_label = "WIN" if pnl > 0 else "LOSS"
            conn.execute(
                """UPDATE shadow_trades
                   SET outcome=?, pnl=?, brier=?,
                       resolved_at=?
                   WHERE id=?""",
                (outcome_label, pnl, brier,
                 datetime.now(timezone.utc).isoformat(), sid),
            )
            n_resolved += 1
            pnl_total += pnl
            d = per_domain.setdefault(domain or "other",
                                      {"n": 0, "wins": 0, "pnl": 0.0,
                                       "briers": []})
            d["n"] += 1
            if outcome_label == "WIN":
                d["wins"] += 1
            d["pnl"] += pnl
            if brier is not None:
                d["briers"].append(brier)
        conn.commit()
        for d in per_domain.values():
            d["pnl"] = round(d["pnl"], 2)
            if d["briers"]:
                d["mean_brier"] = round(sum(d["briers"]) / len(d["briers"]), 4)
                d["n_brier"] = len(d["briers"])
            d.pop("briers", None)
        return {
            "checked":          len(rows),
            "resolved":         n_resolved,
            "still_open":       n_open,
            "errors":           n_err,
            "tagged_domain":    len(rows_to_tag),
            "pnl_resolved_usd": round(pnl_total, 2),
            "per_domain":       per_domain,
        }
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backfill-estimated-prob", action="store_true",
                    help="Backfill model_p from opportunities, then exit")
    a = ap.parse_args()

    if a.backfill_estimated_prob:
        conn = _get_conn()
        try:
            n = backfill_estimated_prob(conn)
            print(f"backfilled estimated_prob on {n} shadow_trade rows")
        finally:
            conn.close()
        return 0

    # Auto-backfill missing model_p before resolving
    conn = _get_conn()
    try:
        n_back = backfill_estimated_prob(conn)
        if n_back:
            _log.info(f"backfilled estimated_prob on {n_back} rows pre-reconcile")
    finally:
        conn.close()

    res = reconcile()
    if a.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    if res.get("error"):
        print(f"\n  ERROR: {res['error']}")
        return 2
    print(f"\n━━━ SHADOW RESOLVER — {datetime.now(timezone.utc).isoformat()} ━━━")
    print(f"  checked:          {res['checked']}")
    print(f"  resolved:         {res['resolved']}  "
          f"still_open: {res['still_open']}  errors: {res['errors']}")
    print(f"  pnl_resolved:     ${res.get('pnl_resolved_usd',0):+.2f}")
    pd = res.get("per_domain", {})
    if pd:
        print(f"\n  Per-domain breakdown:")
        for dom, d in sorted(pd.items()):
            wr = (d['wins'] / d['n'] * 100.0) if d['n'] else 0.0
            mb = d.get('mean_brier')
            mb_str = f"  mean_brier={mb}" if mb is not None else "  (no model_p)"
            print(f"    {dom:<18}  n={d['n']:>3}  WR={wr:>5.1f}%  "
                  f"pnl=${d['pnl']:+.2f}{mb_str}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
