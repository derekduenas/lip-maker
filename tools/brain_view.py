"""Brain view — see what the Edge Hunter has been reasoning about + outcomes.

Renders:
  - Pending triggers (anomaly detector queue)
  - Recent decisions (Brain reasoning + actions proposed/executed)
  - Cost spent (last 24h, last 7d)
  - Top patterns Brain has flagged

Usage:
  python -m tools.brain_view              # last 24h
  python -m tools.brain_view --hours 6
  python -m tools.brain_view --decision N # full reasoning for one decision
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def render(hours: int = 24, db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        # ── Pending triggers ────────────────────────────────────────
        pending = conn.execute(
            """SELECT id, severity, rule, datetime(ts) ts
               FROM brain_triggers WHERE status = 'pending'
               ORDER BY CASE severity WHEN 'EMERGENCY' THEN 1 WHEN 'ALERT' THEN 2
                                      WHEN 'WARN' THEN 3 ELSE 4 END, ts ASC"""
        ).fetchall()

        # ── Decisions in window ─────────────────────────────────────
        decisions = conn.execute(
            f"""SELECT id, datetime(ts) ts, trigger_type, brain_model,
                       cost_usd, reasoning, actions_proposed, actions_executed
               FROM decision_log
               WHERE datetime(ts) > datetime('now','-{hours} hours')
               ORDER BY ts DESC"""
        ).fetchall()

        # ── Cost summary ────────────────────────────────────────────
        cost_24h = conn.execute(
            """SELECT ROUND(SUM(cost_usd), 4), COUNT(*) FROM decision_log
               WHERE datetime(ts) > datetime('now','-24 hours')"""
        ).fetchone()
        cost_7d = conn.execute(
            """SELECT ROUND(SUM(cost_usd), 4), COUNT(*) FROM decision_log
               WHERE datetime(ts) > datetime('now','-7 days')"""
        ).fetchone()

        # ── Top patterns by trigger type ───────────────────────────
        patterns = conn.execute(
            f"""SELECT trigger_type, COUNT(*) n, ROUND(SUM(cost_usd),4) cost
                FROM decision_log
                WHERE datetime(ts) > datetime('now','-{hours} hours')
                GROUP BY trigger_type ORDER BY n DESC"""
        ).fetchall()

        # ── Render ─────────────────────────────────────────────────
        print(f"\n🧠 EDGE HUNTER BRAIN — last {hours}h\n")
        print(f"  Cost: ${cost_24h[0] or 0:.4f} (24h, {cost_24h[1] or 0} decisions) | "
              f"${cost_7d[0] or 0:.4f} (7d, {cost_7d[1] or 0} decisions)")
        print()

        if pending:
            print(f"⏳ PENDING TRIGGERS ({len(pending)})")
            for tid, sev, rule, ts in pending[:8]:
                print(f"  [{sev}] {rule:<30} ts={ts}")
            print()

        if patterns:
            print(f"📊 BRAIN FOCUS — last {hours}h")
            for trigger_type, n, cost in patterns:
                print(f"  {trigger_type:<32} fired={n:>3}× cost=${cost:.4f}")
            print()

        if decisions:
            print(f"🗒  RECENT DECISIONS ({len(decisions)})\n")
            for d in decisions[:10]:
                did, ts, trigger_type, model, cost, reasoning_json, prop_json, exec_json = d
                try:
                    reasoning = json.loads(reasoning_json or "{}")
                    diagnosis = reasoning.get("diagnosis", "")[:200]
                    confidence = reasoning.get("confidence", "?")
                except Exception:
                    diagnosis = (reasoning_json or "")[:200]
                    confidence = "?"
                try:
                    proposed = json.loads(prop_json or "[]")
                    executed = json.loads(exec_json or "[]")
                    auto_count = len(executed)
                    human_count = sum(1 for a in proposed if a.get("safety") == "HUMAN-APPROVE")
                except Exception:
                    auto_count, human_count = 0, 0
                model_short = model.split("-")[1] if "-" in model else model[:8]
                print(f"  #{did} [{ts}] {trigger_type[:28]:<28} {model_short} "
                      f"${cost:.4f} conf={confidence}")
                print(f"     auto-executed: {auto_count}  needs-approval: {human_count}")
                print(f"     DX: {diagnosis}")
                print()

        # ── Action queue (HUMAN-APPROVE proposals waiting) ──
        if decisions:
            human_pending = []
            for d in decisions:
                try:
                    proposed = json.loads(d[6] or "[]")
                    for action in proposed:
                        if action.get("safety") == "HUMAN-APPROVE":
                            human_pending.append({
                                "decision_id": d[0],
                                "trigger": d[2],
                                "action": action.get("action", "")[:60],
                                "details": action.get("details", "")[:120],
                            })
                except Exception:
                    pass
            if human_pending:
                print(f"\n⚠️  HUMAN-APPROVAL QUEUE ({len(human_pending)} items waiting)")
                for hp in human_pending[:8]:
                    print(f"  [#{hp['decision_id']}] {hp['trigger'][:25]:<25} → {hp['action']}")
                    print(f"     {hp['details']}")
                print()
    finally:
        conn.close()


def render_one(decision_id: int, db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        r = conn.execute(
            """SELECT id, ts, trigger_type, brain_model, cost_usd, context_summary,
                      reasoning, actions_proposed, actions_executed
               FROM decision_log WHERE id = ?""",
            (decision_id,),
        ).fetchone()
        if not r:
            print(f"No decision #{decision_id}")
            return
        print(f"\n=== Decision #{r[0]} — {r[1]} ===")
        print(f"Trigger: {r[2]} | Model: {r[3]} | Cost: ${r[4]:.4f}")
        print(f"\nContext:\n{r[5]}")
        print(f"\nReasoning:\n{json.dumps(json.loads(r[6] or '{}'), indent=2)}")
        print(f"\nActions proposed:\n{json.dumps(json.loads(r[7] or '[]'), indent=2)}")
        print(f"\nActions executed:\n{json.dumps(json.loads(r[8] or '[]'), indent=2)}")
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--decision", type=int, default=None,
                   help="Show full reasoning for a specific decision_id")
    a = p.parse_args()
    if a.decision is not None:
        render_one(a.decision)
    else:
        render(hours=a.hours)
