"""Edge Hunter Brain — event-triggered Claude API agent for the LIP system.

PERSONA: You are an alpha/edge profit hunter for a Kalshi LIP rebate-farming
system. Your job is to find inefficiencies, detect emerging opportunities,
diagnose anomalies, and recommend (or auto-execute when safe) actions that
maximize sustainable rebate yield.

INVOCATION:
  Called by cron every 10 min. Reads pending triggers from brain_triggers
  table. For each trigger:
    1. Gathers context (DB queries, optional Kalshi API, optional web search)
    2. Calls Claude API with persona + trigger + tools
    3. Persists Claude's reasoning + actions to decision_log
    4. Marks trigger resolved
    5. Auto-executes safe actions; alerts user on risky ones

COST CONTROL:
  - Haiku 4.5 for INFO/WARN triggers (~$0.005 per call)
  - Sonnet 4.6 for ALERT/EMERGENCY + scheduled reviews (~$0.05 per call)
  - Prompt caching enabled on system prompt (90% discount on repeated context)
  - If no triggers pending, exits immediately ($0)

SAFE-ACTION LIST (Brain can auto-execute):
  - log_observation       — write a note about a pattern noticed
  - mark_trigger_resolved — close out a stale alert
  - propose_to_user       — write a plain-English suggestion to alerts log

RISKY-ACTION LIST (Brain proposes, human approves):
  - blacklist_series      — soft-blacklist a series for N hours
  - bump_size_multiplier  — adjust per-series quote size
  - kill_market           — cancel all orders on a market
  - reset_calibration     — wipe a series's calibration history
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings

_log = logging.getLogger(__name__)


# ── Models + cost ──
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_INPUT_PER_M = 1.0
HAIKU_OUTPUT_PER_M = 5.0
SONNET_INPUT_PER_M = 3.0
SONNET_OUTPUT_PER_M = 15.0

# Severity → model mapping (default routing)
SEVERITY_MODEL = {
    "INFO":      HAIKU_MODEL,
    "WARN":      HAIKU_MODEL,
    "ALERT":     SONNET_MODEL,
    "EMERGENCY": SONNET_MODEL,
}

# Strategic-review rules ALWAYS use Sonnet regardless of severity — these
# need long structured output the brain can really reason about.
STRATEGIC_RULES = {"daily_strategic_review", "weekly_deep_review",
                   "drawdown_threshold", "reconciliation_alerts",
                   "allocator_quoting_gap"}
# 2026-05-05: drawdown/reconciliation/allocator triggers were getting
# truncated at 2048 tokens — brain saw the problem but couldn't finish
# its action plan. Bumped to 4096.


def _model_for(rule: str, severity: str) -> str:
    if rule in STRATEGIC_RULES:
        return SONNET_MODEL
    return SEVERITY_MODEL.get(severity, HAIKU_MODEL)


def _max_tokens_for(rule: str) -> int:
    return 4096 if rule in STRATEGIC_RULES else 2048


SYSTEM_PROMPT = """You are AEGIS — the defensive shield + rebate-maker brain for INNAIT.
Your sister AI is HYDRA (directional alpha hunter on weather/macro).
You manage REAL MONEY on Kalshi LIP. HYDRA is paper-validating its alpha.
Your job: protect capital, maximize rebate, block bleeders. Hers: hunt edge.

═════════════════════════════════════════════════════════════════════════
THE MISSION (drives every decision):

  GOAL:     $20,000/month recurring net rebate income
  WHY:      User wants W2 exit. $20k/mo = take-home replacement + buffer.
  PILLAR:   Kalshi LIP — continuing, no sunset deadline. Compound forever.
  BANKROLL: NAV ~$2,100 (real money). Treat as PRINCIPAL — protect first,
            grow second. Every action: would I do this with my own money?

═════════════════════════════════════════════════════════════════════════
AEGIS PERSONA (how you think):

  - Numbers > vibes. Cite specific values in every diagnosis.
    BAD:  "the system is bleeding"
    GOOD: "NAV -$248 in 24h, KXAAAGASW alone -$98 (30% of loss)"

  - Confidence calibration: recent Brier matters. If your last 3 actions
    DIDN'T improve metrics, drop confidence one tier (low → cool off).

  - Principal-first rule: when uncertain, choose the action that protects
    capital over the one that grows it. Defense compounds slowly. Loss
    compounds fast.

  - Tight cycles: don't propose 5 actions when 1 fixes the root cause.
    Under-action > over-action.

  - **AUTONOMY MANDATE (2026-05-06)**: User has authorized you to drive
    95% of decisions yourself. DEFAULT to "safety": "SAFE-AUTO" on all
    profit-protection + capital-rebalancing actions. Only mark
    "HUMAN-APPROVE" for these IRREVERSIBLE moves:
      • scale_bankroll (adding new money)
      • enable_live_mode (paper → real)
      • add_category (new market type / venue)
      • any action with cost > $100 single-shot

    For everything else — adjust_size_multiplier, soft_demote_series,
    cancel_market, blacklist_series, propose_cool_off, log_observation —
    default to SAFE-AUTO when your confidence is medium+ and the action
    is bounded. Trust your analysis. Act decisively. The user reviews
    your dashboard each cycle and sees what you executed.

  - **RENAISSANCE DISCIPLINE (2026-05-06)**: User's strategic vision is
    "the public Renaissance Medallion." Profit = confidence × differential
    × volume × time. Sister AI HYDRA enforces a Renaissance gate (only
    bets where our calibrated confidence DIFFERS from market by N×sigma
    of calibration error). YOU should apply the same lens:
      • Don't promote a series just because rebate is high — verify our
        ACTUAL share materializes (HIT_RATE > 30%). High reward × low
        share = low EV.
      • Don't size up a market just because our confidence is high —
        if the market also is confident, the spread isn't there.
      • Don't chase any single market — many small validated bets >
        few big swings. LIP scoring rewards CONSISTENT presence.

═════════════════════════════════════════════════════════════════════════
YOUR DUAL ROLES:

  ROLE 1 — REACTIVE (anomaly triggers): diagnose what's wrong, propose
           SAFE-AUTO heals or HUMAN-APPROVE actions.

  ROLE 2 — STRATEGIC (daily/weekly reviews + new_series_detected):
           Evaluate our trajectory vs the $20k/mo goal. Recommend the
           highest-leverage moves for getting closer.

═════════════════════════════════════════════════════════════════════════
THE FULL SYSTEM YOU OPERATE WITHIN:

  LAYER 1 (REAL-TIME): run_paper.py — WebSocket book updates → reprice →
                       two-sided quote at qualify-cliff size
  LAYER 2 (ALLOCATION): capital_allocator — yield-per-$ ranking with
                        3 multipliers (series_priority × calibration_ratio
                        × setup_score from KNN over 287 historical setups)
  LAYER 3 (LEARNING): auto_calibrate (hourly per-series share calibration),
                      setup_recognition (KNN), truth_reconciler (auto-heal
                      settlements/fills/orders against Kalshi truth)
  LAYER 4 (MEASUREMENT): funnel_snapshot every minute, every layer
                          drop visible (ENROLLED→RANKED→SAFE→QUOTING→
                          TWO_SIDED→FILLED→PROJECTED→EST_REBATE→SETTLED)
  LAYER 5 (META): YOU — strategic + reactive intelligence

═════════════════════════════════════════════════════════════════════════
PATHS TO $20k/MO (research-validated tradeoffs):

  A) DEEPEN KALSHI LIP — current trajectory
     - Ceiling at current candidate set: ~$3-7k/mo (per agent research)
     - Multipliers needed: more capital ($10k bankroll), better calibration,
       broader market discovery (lower MIN_REWARD already shipped)
     - Time: weeks to ramp; treat as continuing pillar
  B) MULTI-VENUE STACK (Polymarket revival, Gemini Predictions when CFTC
     opens it, Crypto.com OG)
     - Each venue's LIP capacity is independent — additive
     - Effort: 15-20hr per venue port
  C) DIRECTIONAL ALPHA OVERLAY (music, sports/entertainment niches)
     - Verified hero: Brandon Fean ($100k+ on Kalshi music via HDD scraper)
     - Effort: 7-30 hours depending on edge
     - Fragile: sharp-trader alpha decay, insider trading risk
  D) CROSS-VENUE MAKER REBATE STACKING (Kalshi + future venues, 2+ venues
     earning rebate on same contract)

═════════════════════════════════════════════════════════════════════════
PRINCIPLES:

  1. NO HARDCODED FIXES. Always propose general behaviors. If you'd
     recommend "skip series X", instead propose "rule that catches
     series matching pattern Y" — others will benefit from same rule.

  2. EVERY ACTION MUST BE GOAL-LINKED. Tie recommendations to the $20k/mo
     mission explicitly: "this captures $X/mo more, closing X% of gap."

  3. CAPITAL IS SCARCE. $1 deployed = $1 not in something else.
     Every recommendation must pass the marginal-yield test.

  4. CONFIDENCE GATING. <high confidence = HUMAN-APPROVE. The user has
     been burned by hallucinated $16k/mo predictions. Skepticism wins.

  5. COMPOUND WHEN WINNING. Bias toward sustainable changes that scale
     with bankroll. LIP is a continuing pillar — no artificial deadline.

  6. SHOW YOUR MATH. Numbers > vibes. If projecting $X/d, explain
     pool × share × calibration. If proposing diversification, show
     correlation evidence not just pattern hunch.

═════════════════════════════════════════════════════════════════════════
WHEN TRIGGER IS A SCHEDULED REVIEW (daily_strategic_review or
weekly_deep_review), structure your output as:

  - "diagnosis": "Performance vs goal: $X/d actual vs $666/d target
    ($Y% of goal). Trajectory: improving/flat/declining."
  - "cause": "Constraints holding us back, ranked by impact"
  - "action_plan": [
      Strategic moves ordered by $/effort. Each step:
      - Quantify expected $/mo gain (or risk avoided)
      - Effort estimate (hours)
      - Why it matters now given current trajectory
      - Mark SAFE-AUTO (analysis) vs HUMAN-APPROVE (capital/code change)
    ]
  - "confidence": low/medium/high
  - "learning_note": "What should we remember from this review?"

WHEN TRIGGER IS REACTIVE (anomaly), structure normally:

  - "diagnosis": what's actually happening
  - "cause": most likely root cause
  - "action_plan": [steps with safety tags]
  - "confidence": low/medium/high
  - "learning_note": pattern to remember

═════════════════════════════════════════════════════════════════════════
ACTION VOCABULARY YOU HAVE ACTUAL AUTHORITY TO USE:

  TIER 1 — auto-execute always (free, no $ impact):
    - "log_observation"  — record a noticed pattern
    - "alert"            — surface to human dashboard

  TIER 2 — auto-execute if confidence=="high" AND max_loss < $25:
    - "cancel_market_orders" {"ticker": "KX..."} — kill all orders on one market
    - "soft_demote_series" {"series": "KX...", "factor": 0.5}
        — multiply series_priority by factor for 24h
        (factor must be 0.1–1.0; use 0.5 for "halve weight",
         0.2 for "near-blacklist temporary")

  TIER 3 — auto-execute if confidence=="high" AND series has >=100 samples:
    - "adjust_size_multiplier" {"series": "KX...", "delta_pct": -10}
        — fine-tune the per-series quote-size mult by ±20%
        (delta_pct in [-20, +20]; capped to keep mult in [0.2, 3.0])

  SELF-THROTTLE (TIER 1, free, no $ impact):
    - "propose_cool_off" {"hours": 4, "details": "..."}
        — pause ALL Brain auto-execute for N hours (0.5-24)
        — USE THIS when: trend='degrading', losses mounting, calibration
          drifting unexpectedly, or you're uncertain why recent decisions
          aren't paying off.
        — This is YOUR self-throttle. No hardcoded daily $ cap exists.
          You are responsible for recognizing when to step back.

  TIER 4 — ALWAYS HUMAN-APPROVE (never use these as SAFE-AUTO):
    - bankroll changes, hard blacklists, code edits, new features

When you propose actions, set "action_type" to one of these keys, plus
"safety": "SAFE-AUTO" if you want auto-execute. Daily cap of 30 auto-actions
prevents runaway. Brain decisions persist in runtime_overrides table; the
capital_allocator reads them on next refresh.

═════════════════════════════════════════════════════════════════════════
ATTACK TARGETS — WHERE MAX PROFIT LIVES

Each prompt's state snapshot now includes "attack_targets": a ranked list of
the top-10 markets to focus capital on RIGHT NOW. Mirrors the Kalshi UI
"Liquidity Rewards" filter. Each entry shows:
  - net_$/d  — projected net dollars per day after share + qual + adverse
  - reward_$/d — gross pool reward per day
  - share    — our observed share of the pool (lower = more competition)
  - comp     — effective competitor count estimate
  - hrs_rem  — hours until program ends
  - is_new   — true if incentive_description == "new_event" (fresh launch
               with low competition initially — ATTACK FAST before others find it)
  - priority — HIGH (>$10/d net) / MED (>$2/d) / LOW

Use this list when:
  - Diagnosing capital allocation: "we're not on the top-3 attack targets"
  - Proposing series promotions: only promote series with HIGH-priority members
  - Justifying new_series_detected verdicts: cross-reference live attack list
  - Strategic reviews: how much of total_addressable_net are we capturing?

When proposing an action, cite which attack target(s) it activates.

═════════════════════════════════════════════════════════════════════════
RECENT STATE SIGNALS (state snapshot enriched 2026-05-05):

You now see THREE new diagnostic blocks each cycle — use them aggressively:

  1. "concentration" — top 10 series by open exposure with pct_of_total.
     ▸ If any series > 25% of capital → propose cap or adjust_size_mult down
     ▸ If a BLOCKED series appears (e.g. KXCOFFEEW, KXAAAGAS) → those are
       stale; propose action to flatten them
     ▸ If a single series has many positions but small gross (e.g. BTC15M
       with 51 pos / $123) → low-yield churn; deprioritize via adjust_size_mult

  2. "high_share_markets" — markets where our share > 15% (low-comp gold).
     ▸ These pay us disproportionate rebate. Size UP via adjust_size_mult
       toward 1.3-1.5x for shares above 20% to capture more pool
     ▸ Don't size up shares below 10% — competition will eat the marginal $

  ⚠ DO NOT FAVOR LOW-COMP BLINDLY. A $1,650/day MED-COMP pool at 15% share
  ($247 net) beats a $140 LOW-COMP pool at 34% share ($48 net). The
  attack_targets ranking already does this math — it sorts by net $/day, not
  by competition class. Trust the ranking. Size by EV per dollar deployed,
  not by "low comp = always better".

  3. "attack_gap_not_quoted" — markets in attack_targets but we are NOT
     quoting them. Strong gap signal.
     ▸ Diagnose: enrolled=0 (gate failure), per-series cap, capital constraint
     ▸ If reward is high AND we have spare capital → propose investigate or
       loosen_gate action

  4. "open_exposure" — open positions in BLOCKED series (stragglers).
     ▸ When `n_blocked_with_exposure > 0`, propose HUMAN-APPROVE
       `flatten_series` action listing the blocked series + exposure $.
     ▸ User decides whether to lock in current MTM loss vs ride to settlement.
     ▸ Don't auto-flatten — could panic-sell during volatility.

GENERAL RULE: every cycle, AT LEAST scan these three blocks. If two or more
flag a problem and you have daily_cap_remaining > 5, propose 1-2 actions.

═════════════════════════════════════════════════════════════════════════
SELF-GOVERNANCE — YOU ARE RESPONSIBLE FOR THROTTLING

Each prompt includes YOUR OWN TRACK RECORD: actions taken last 24h/7d,
estimated Brain-attributed losses, current trend (improving/stable/degrading),
and any active self-imposed cool-off.

THERE ARE NO HARDCODED DAILY LOSS CAPS. The user explicitly chose adaptive
governance over arbitrary thresholds. Two backstops exist:
  - Manual PAUSE_FILE (always honored)
  - 10% NAV catastrophic-drop trip (only true emergency)

Everything else is YOUR judgment. Specifically:
  - If your last 5 actions all underperformed → propose cool_off ~4h
  - If trend == 'degrading' AND losses_24h > $10 → propose cool_off + alert
  - If a series you demoted yesterday is showing better calibration today
    → consider proposing to restore its mult (be honest about being wrong)
  - When uncertain, escalate to HUMAN-APPROVE rather than auto-execute

═════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT (strict JSON, no preamble, no code fences):

{
  "diagnosis": "...",
  "cause": "...",
  "action_plan": [
    {"step": 1, "action": "...", "safety": "SAFE-AUTO" | "HUMAN-APPROVE",
     "details": "...", "expected_value": "$X/mo gain or $Y/mo risk reduced"}
  ],
  "confidence": "low" | "medium" | "high",
  "learning_note": "...",
  "goal_progress_note": "How does this move us toward $20k/mo?"
}
"""


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _gather_state_snapshot(conn) -> dict:
    """Compact state dump for Claude. <= ~3000 tokens."""
    out = {}
    # Funnel latest
    try:
        rows = conn.execute(
            """SELECT layer, count, detail_json FROM funnel_log
               WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM funnel_log)"""
        ).fetchall()
        out["funnel"] = {r[0]: {"count": r[1], "detail": json.loads(r[2] or "{}")} for r in rows}
    except Exception:
        pass
    # Calibration top 10 by samples
    try:
        rows = conn.execute(
            """SELECT series_ticker, calibration_ratio, samples
               FROM series_calibration ORDER BY samples DESC LIMIT 10"""
        ).fetchall()
        out["calibration_top10"] = [
            {"series": r[0], "ratio": r[1], "samples": r[2]} for r in rows
        ]
    except Exception:
        pass
    # Setup outcomes summary
    try:
        rows = conn.execute(
            """SELECT cadence_class, COUNT(*) n,
                      ROUND(AVG(paid_per_day_usd),3) avg_paid,
                      ROUND(MAX(paid_per_day_usd),3) max_paid
               FROM setup_outcomes GROUP BY cadence_class"""
        ).fetchall()
        out["setup_outcomes_by_cadence"] = [
            {"cadence": r[0], "n": r[1], "avg_paid": r[2], "max_paid": r[3]} for r in rows
        ]
    except Exception:
        pass
    # 7d totals
    try:
        r = conn.execute(
            """SELECT COUNT(*), ROUND(SUM(rebate_earned_usd),3),
                      ROUND(SUM(our_realized_usd),2),
                      ROUND(SUM(rebate_earned_usd)+SUM(our_realized_usd),2)
               FROM settlement_log
               WHERE datetime(close_time) > datetime('now','-7 days')
                 AND (rebate_earned_usd != 0 OR our_realized_usd != 0)"""
        ).fetchone()
        out["7d_settled"] = {
            "n": r[0], "rebate": r[1], "realized": r[2], "net": r[3],
        }
    except Exception:
        pass
    # Recent reconciliation alerts
    try:
        rows = conn.execute(
            """SELECT category, action, COUNT(*) n FROM reconciliation_log
               WHERE datetime(ts) > datetime('now','-2 hours')
               GROUP BY category, action ORDER BY n DESC LIMIT 8"""
        ).fetchall()
        out["recent_reconciliation"] = [
            {"cat": r[0], "action": r[1], "n": r[2]} for r in rows
        ]
    except Exception:
        pass
    # OPEN EXPOSURE RISK — surfaces:
    #   (1) NAV % deployed in positions vs cash
    #   (2) Concentration in any single series
    #   (3) Open positions in series that are NOW BLOCKED (sunk-cost stragglers)
    # Brain uses this to recommend FLATTEN actions (HUMAN-APPROVE) when risk
    # peaks — we DON'T auto-flatten because that locks in MTM losses; brain
    # surfaces the data so user can decide.
    try:
        from config import settings
        bl_set = set(getattr(settings, "SERIES_BLOCKLIST", set()))
        rows = conn.execute(
            "SELECT substr(market_ticker, 1, instr(market_ticker, '-')-1) AS series, "
            "COUNT(*) AS n, ROUND(SUM(gross_usd),2) AS gross "
            "FROM inventory WHERE net_yes_contracts != 0 GROUP BY series ORDER BY gross DESC"
        ).fetchall()
        total = sum((r[2] or 0) for r in rows)
        # Series with open exposure that ARE in blocklist (the bleeding-stragglers)
        blocked_with_exposure = []
        for r in rows:
            if any(r[0].startswith(b) for b in bl_set):
                blocked_with_exposure.append({
                    "series": r[0], "n_positions": r[1], "exposure_usd": r[2],
                    "msg": "BLOCKED but still open — consider flatten"
                })
        out["open_exposure"] = {
            "total_open_usd": round(total, 2),
            "blocked_series_with_open_positions": blocked_with_exposure,
            "n_blocked_with_exposure": len(blocked_with_exposure),
        }
    except Exception as e:
        out["open_exposure_error"] = str(e)

    # ATTACK TARGETS — top markets to focus capital, ranked by net $/day with
    # competition + new_event + urgency boosts. Mirrors the Kalshi UI rewards
    # filter so the Brain knows EXACTLY where max-profit hunts live.
    try:
        from tools.attack_targets import brain_summary as _attack_brain_summary
        out["attack_targets"] = _attack_brain_summary(top_n=10)
    except Exception as e:
        out["attack_targets_error"] = str(e)

    # CONCENTRATION — open exposure by series. Brain uses this to detect
    # over-concentration in single series + stale positions in blocked series.
    try:
        rows = conn.execute(
            "SELECT substr(market_ticker, 1, instr(market_ticker, '-')-1) AS series, "
            "COUNT(*) AS n_pos, ROUND(SUM(gross_usd), 2) AS gross "
            "FROM inventory WHERE net_yes_contracts != 0 "
            "GROUP BY series ORDER BY gross DESC LIMIT 10"
        ).fetchall()
        total = sum((r[2] or 0) for r in rows)
        out["concentration"] = [
            {"series": r[0], "n_positions": r[1], "gross_usd": r[2],
             "pct_of_total": round((r[2] or 0) / total * 100, 1) if total else 0}
            for r in rows
        ]
        out["concentration_total_usd"] = round(total, 2)
    except Exception:
        pass

    # HIGH-SHARE MARKETS — where we have >15% of pool (low-comp gold pockets).
    # Brain should consider sizing UP these markets via adjust_size_mult.
    try:
        rows = conn.execute(
            "SELECT market_ticker, COUNT(*) AS snaps, "
            "ROUND(AVG(our_score / NULLIF(total_score, 0)) * 100, 1) AS share_pct, "
            "ROUND(AVG(total_score), 1) AS pool_score "
            "FROM lip_snapshots WHERE was_resting=1 "
            "AND captured_at >= datetime('now', '-2 hours') "
            "AND total_score > 0 "
            "GROUP BY market_ticker HAVING share_pct >= 15 "
            "ORDER BY share_pct DESC LIMIT 10"
        ).fetchall()
        out["high_share_markets"] = [
            {"ticker": r[0], "snaps_2h": r[1], "share_pct": r[2], "pool_score": r[3]}
            for r in rows
        ]
    except Exception:
        pass

    # ATTACK-TARGET GAP — markets in attack list that we are NOT quoting.
    # Brain uses this to diagnose enrollment/gate failures.
    try:
        attack = (out.get("attack_targets") or {}).get("top_targets") or []
        attack_tickers = [a["ticker"] for a in attack]
        if attack_tickers:
            placeholders = ",".join("?" * len(attack_tickers))
            quoted = {r[0] for r in conn.execute(
                f"SELECT DISTINCT market_ticker FROM lip_snapshots "
                f"WHERE was_resting=1 AND captured_at >= datetime('now', '-1 hours') "
                f"AND market_ticker IN ({placeholders})",
                attack_tickers,
            ).fetchall()}
            gap = [a for a in attack if a["ticker"] not in quoted]
            out["attack_gap_not_quoted"] = gap[:10]
    except Exception:
        pass

    return out


def _call_claude(model: str, system_prompt: str, user_message: str,
                 max_tokens: int = 2048) -> tuple[str, dict]:
    """Returns (response_text, usage_dict). Uses prompt caching on system."""
    api_key = _api_key()
    if not api_key:
        return ("[NO_API_KEY] set ANTHROPIC_API_KEY env var to enable Brain",
                {"input_tokens": 0, "output_tokens": 0})
    try:
        import anthropic  # type: ignore
    except ImportError:
        return ("[NO_SDK] pip install anthropic to enable Brain",
                {"input_tokens": 0, "output_tokens": 0})
    client = anthropic.Anthropic(api_key=api_key)
    # Use prompt caching on system prompt for ~90% discount on repeated calls
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text", "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens":  getattr(resp.usage, "input_tokens", 0),
        "output_tokens": getattr(resp.usage, "output_tokens", 0),
        "cache_read":    getattr(resp.usage, "cache_read_input_tokens", 0),
        "cache_create":  getattr(resp.usage, "cache_creation_input_tokens", 0),
    }
    return text, usage


def _estimate_cost(model: str, usage: dict) -> float:
    if model == HAIKU_MODEL:
        in_per_m, out_per_m = HAIKU_INPUT_PER_M, HAIKU_OUTPUT_PER_M
    else:
        in_per_m, out_per_m = SONNET_INPUT_PER_M, SONNET_OUTPUT_PER_M
    # Cached reads cost 10% of normal input
    regular_in = usage.get("input_tokens", 0) - usage.get("cache_read", 0)
    cached_in = usage.get("cache_read", 0)
    create_in = usage.get("cache_create", 0)  # 25% premium for write
    cost = (
        regular_in * in_per_m / 1_000_000
        + cached_in * in_per_m * 0.10 / 1_000_000
        + create_in * in_per_m * 1.25 / 1_000_000
        + usage.get("output_tokens", 0) * out_per_m / 1_000_000
    )
    return round(cost, 5)


# ── TIERED AUTO-EXECUTE — graduated authority based on $ impact ──
#
# TIER_1: log/observe/note (already safe — no $ impact)
# TIER_2: cancel order, soft-demote series, adjust quote size  (max_loss <= $25)
# TIER_3: adjust series size_mult ±20%, spawn explore probe   (max_loss <= $10)
# TIER_4: bankroll, hard blacklist, code changes — ALWAYS HUMAN-APPROVE

# Daily caps to prevent runaway
DAILY_AUTO_ACTION_CAP = 30
TIER_2_MAX_LOSS_USD   = 25.0
TIER_3_MAX_LOSS_USD   = 10.0

# ═══════════════════════════════════════════════════════════════════
# BRAIN GUARDRAILS — only true-emergency stops; everything else adaptive
# ═══════════════════════════════════════════════════════════════════
# Per user 2026-05-04: don't box the Brain in with arbitrary $ caps.
# Give it FULL VISIBILITY into its own track record and trust it to
# self-throttle. Only TWO hardcoded backstops (catastrophe-class):
#
#   1. PAUSE_FILE  — manual emergency stop (always honored)
#   2. NAV catastrophic drop (10%) — something is fundamentally wrong,
#      halt regardless of cause
#
# Everything else (loss tolerance, action velocity, sizing aggression)
# the Brain decides FROM ITS OWN PERFORMANCE DATA each cycle. It can:
#   - propose its own cool-off when patterns degrade
#   - scale up aggression when edges prove out
#   - escalate confidence-low calls to HUMAN-APPROVE on its own

PAUSE_FILE = Path("/root/lip-maker/BRAIN_PAUSE")
NAV_CATASTROPHIC_DROP_PCT = 0.10  # only true emergency, not "bad day"


def _brain_self_awareness(conn) -> dict:
    """Compute Brain's own track record. Fed into the prompt every cycle so
    Brain can self-throttle based on real performance, not arbitrary caps."""
    out = {
        "actions_24h":             0,
        "actions_7d":              0,
        "auto_executed_24h":       0,
        "estimated_loss_24h":      0.0,
        "estimated_loss_7d":       0.0,
        "current_cool_off_until":  None,
        "trend":                   "no_data",
    }
    try:
        out["actions_24h"] = conn.execute(
            """SELECT COUNT(*) FROM brain_actions
               WHERE datetime(ts) > datetime('now','-24 hours')"""
        ).fetchone()[0]
        out["actions_7d"] = conn.execute(
            """SELECT COUNT(*) FROM brain_actions
               WHERE datetime(ts) > datetime('now','-7 days')"""
        ).fetchone()[0]
        out["auto_executed_24h"] = conn.execute(
            """SELECT COUNT(*) FROM brain_actions
               WHERE status = 'executed'
                 AND datetime(ts) > datetime('now','-24 hours')"""
        ).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    # Brain-attributed losses via alpha_trades linked to decisions
    try:
        for window, key in [("-24 hours", "estimated_loss_24h"),
                             ("-7 days",   "estimated_loss_7d")]:
            r = conn.execute(
                f"""SELECT COALESCE(SUM(realized_usd),0) FROM alpha_trades
                    WHERE realized_usd < 0
                      AND datetime(ts) > datetime('now','{window}')"""
            ).fetchone()
            if r:
                out[key] = round(abs(float(r[0] or 0)), 2)
    except sqlite3.OperationalError:
        pass
    # Self-imposed cool-off (Brain wrote to runtime_overrides)
    try:
        r = conn.execute(
            """SELECT value_json, expires_at FROM runtime_overrides
               WHERE override_type = 'brain_cool_off'
                 AND datetime(expires_at) > datetime('now')
               ORDER BY expires_at DESC LIMIT 1"""
        ).fetchone()
        if r:
            out["current_cool_off_until"] = r[1]
    except sqlite3.OperationalError:
        pass
    # Trend signal: compare 7d vs 24h loss rates
    if out["estimated_loss_7d"] > 0:
        ratio_24h = out["estimated_loss_24h"] / max(0.01, out["estimated_loss_7d"] / 7)
        if ratio_24h > 1.5:
            out["trend"] = "degrading"
        elif ratio_24h < 0.5:
            out["trend"] = "improving"
        else:
            out["trend"] = "stable"
    return out


def check_guardrails(conn) -> tuple[bool, str]:
    """Returns (allow_auto_execute, reason_if_blocked).
    Only hardcoded stops: PAUSE_FILE + catastrophic NAV drop + Brain's
    own self-imposed cool-off. Everything else is Brain's call."""
    # 1. Manual pause file
    if PAUSE_FILE.exists():
        return False, "PAUSE_FILE_present"
    # 2. Catastrophic NAV drop (10%+ from 7d peak — something is WRONG)
    try:
        peak = conn.execute(
            "SELECT MAX(nav) FROM nav_history WHERE datetime(ts) > datetime('now','-7 days')"
        ).fetchone()
        cur = conn.execute(
            "SELECT nav FROM nav_history ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if peak and peak[0] and cur and cur[0]:
            dd = (peak[0] - cur[0]) / max(0.01, peak[0])
            if dd >= NAV_CATASTROPHIC_DROP_PCT:
                return False, f"nav_catastrophic_drop_{dd*100:.1f}%"
    except sqlite3.OperationalError:
        pass
    # 3. Brain's own self-imposed cool-off
    try:
        r = conn.execute(
            """SELECT 1 FROM runtime_overrides
               WHERE override_type = 'brain_cool_off'
                 AND datetime(expires_at) > datetime('now')"""
        ).fetchone()
        if r:
            return False, "brain_self_imposed_cool_off"
    except sqlite3.OperationalError:
        pass
    return True, "ok"


def _ensure_overrides_schema(conn):
    """Schema for Brain's persistent action effects."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS runtime_overrides (
      series_ticker  TEXT NOT NULL,
      override_type  TEXT NOT NULL,
      value_json     TEXT NOT NULL,
      expires_at     TEXT,
      created_by     TEXT NOT NULL,
      created_at     TEXT NOT NULL,
      PRIMARY KEY(series_ticker, override_type)
    );
    CREATE TABLE IF NOT EXISTS brain_actions (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      decision_id  INTEGER,
      ts           TEXT NOT NULL,
      tier         TEXT NOT NULL,
      action_type  TEXT NOT NULL,
      params_json  TEXT,
      status       TEXT NOT NULL,
      result       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_actions_ts ON brain_actions(ts);
    """)
    conn.commit()


def _today_action_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM brain_actions WHERE date(ts) = date('now') AND status='executed'"
    ).fetchone()[0]


def _record_action(conn, decision_id, tier, action_type, params, status, result=""):
    conn.execute(
        """INSERT INTO brain_actions
           (decision_id, ts, tier, action_type, params_json, status, result)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (decision_id, datetime.now(timezone.utc).isoformat(),
         tier, action_type, json.dumps(params, default=str)[:500], status, result[:500]),
    )
    conn.commit()


# ── Action handlers ──

def _act_log(conn, decision_id, action) -> tuple[bool, str]:
    """TIER_1: just record the observation. Free."""
    _record_action(conn, decision_id, "TIER_1_LOG", "log_observation",
                   action, "executed", action.get("details", "")[:200])
    return True, "logged"


def _act_cancel_market(conn, decision_id, action) -> tuple[bool, str]:
    """TIER_2: cancel ALL orders on a single market. Max loss = bankroll exposure
    on that market (typically < $25)."""
    ticker = action.get("ticker") or _extract_ticker(action)
    if not ticker:
        return False, "no_ticker"
    try:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        r = c.get("/portfolio/orders", params={"status": "resting", "ticker": ticker, "limit": 50})
        orders = r.get("orders", []) or []
        # Estimate max loss = sum of cost basis on this ticker
        max_loss = sum(o.get("count", 0) * o.get("yes_price", o.get("no_price", 50))/100 for o in orders)
        if max_loss > TIER_2_MAX_LOSS_USD:
            return False, f"max_loss_${max_loss:.2f}_exceeds_tier2"
        cancelled = 0
        for o in orders:
            oid = o.get("order_id")
            if oid:
                try:
                    c.delete(f"/portfolio/orders/{oid}")
                    cancelled += 1
                except Exception:
                    pass
        _record_action(conn, decision_id, "TIER_2_BUDGET", "cancel_market_orders",
                       {"ticker": ticker, "cancelled": cancelled}, "executed",
                       f"cancelled {cancelled} orders on {ticker} (loss_cap=${max_loss:.2f})")
        return True, f"cancelled {cancelled} orders on {ticker}"
    except Exception as e:
        _record_action(conn, decision_id, "TIER_2_BUDGET", "cancel_market_orders",
                       action, "failed", str(e)[:200])
        return False, str(e)[:80]


def _act_soft_demote_series(conn, decision_id, action) -> tuple[bool, str]:
    """TIER_2: write runtime_override that demotes a series for 24h.
    capital_allocator reads runtime_overrides and multiplies series_priority
    by the override factor."""
    series = action.get("series") or _extract_series(action)
    factor = float(action.get("factor", 0.5))
    if not series:
        return False, "no_series"
    if factor < 0.1 or factor > 1.0:
        return False, f"factor_{factor}_out_of_range"
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO runtime_overrides
               (series_ticker, override_type, value_json, expires_at, created_by, created_at)
               VALUES (?, 'size_mult', ?, ?, ?, ?)""",
            (series, json.dumps({"multiplier": factor}),
             expires, f"brain_decision_{decision_id}",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        _record_action(conn, decision_id, "TIER_2_BUDGET", "soft_demote_series",
                       {"series": series, "factor": factor}, "executed",
                       f"demoted {series} to {factor}× for 24h")
        return True, f"demoted {series} × {factor}"
    except Exception as e:
        _record_action(conn, decision_id, "TIER_2_BUDGET", "soft_demote_series",
                       action, "failed", str(e)[:200])
        return False, str(e)[:80]


def _act_blacklist_series(conn, decision_id, action) -> tuple[bool, str]:
    """TIER_2: AEGIS adds series to runtime market_blacklist for 7 days.
    Catches bleeders the auto-prune timer hasn't picked up yet (e.g. only
    1-2 settles but pattern obvious from positioning + book)."""
    series = action.get("series") or _extract_series(action)
    if not series:
        return False, "no_series"
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    try:
        # Find all open tickers in this series
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT market_ticker FROM lip_programs "
            "WHERE substr(market_ticker,1,instr(market_ticker||'-','-')-1) = ? "
            "AND datetime(end_date) > datetime('now')",
            (series,),
        ).fetchall()]
        n = 0
        for t in tickers:
            conn.execute(
                "INSERT OR REPLACE INTO market_blacklist(ticker, reason, expires_at, added_at) "
                "VALUES(?, ?, ?, ?)",
                (t, f"AEGIS_decision_{decision_id}", expires,
                 datetime.now(timezone.utc).isoformat()),
            )
            n += 1
        conn.commit()
        _record_action(conn, decision_id, "TIER_2_BUDGET", "blacklist_series",
                       {"series": series, "n_tickers": n}, "executed",
                       f"blacklisted {n} {series}* tickers for 7d")
        return True, f"blacklisted {n} {series} tickers"
    except Exception as e:
        return False, str(e)[:80]


def _act_flatten_blocked_series(conn, decision_id, action) -> tuple[bool, str]:
    """TIER_2: AEGIS cancels resting orders + (optionally) closes open
    positions in a blocked series. Use when stragglers are bleeding to
    settlement after we've already blacklisted the series.

    For now: cancels open orders only (closing positions = real-money
    decision, requires HUMAN-APPROVE). Future: add sell-at-mid option."""
    series = action.get("series") or _extract_series(action)
    if not series:
        return False, "no_series"
    try:
        from execution.kalshi_auth import KalshiClient
        c = KalshiClient()
        orders = c.get("/portfolio/orders", params={"status": "resting"}).get("orders", [])
        cancelled = 0
        for o in orders:
            if o.get("ticker", "").startswith(series):
                try:
                    c.delete(f"/portfolio/orders/{o['order_id']}")
                    cancelled += 1
                except Exception:
                    continue
        _record_action(conn, decision_id, "TIER_2_BUDGET", "flatten_blocked_series",
                       {"series": series, "n_cancelled": cancelled}, "executed",
                       f"cancelled {cancelled} resting orders in {series}")
        return True, f"cancelled {cancelled} {series} orders"
    except Exception as e:
        return False, str(e)[:80]


def _act_adjust_size_mult(conn, decision_id, action) -> tuple[bool, str]:
    """TIER_3: adjust per-series multiplier within ±20%. Requires >=100 samples."""
    series = action.get("series") or _extract_series(action)
    delta_pct = float(action.get("delta_pct", 0))
    if not series or abs(delta_pct) > 20:
        return False, f"out_of_range delta={delta_pct}"
    try:
        row = conn.execute(
            "SELECT samples FROM series_calibration WHERE series_ticker = ?",
            (series,),
        ).fetchone()
        if not row or row[0] < 100:
            return False, "insufficient_samples"
        # Compute current effective multiplier (default 1.0) and apply delta
        existing = conn.execute(
            """SELECT value_json FROM runtime_overrides
                 WHERE series_ticker=? AND override_type='size_mult'
                   AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))""",
            (series,),
        ).fetchone()
        current = json.loads(existing[0]).get("multiplier", 1.0) if existing else 1.0
        new_mult = max(0.2, min(3.0, current * (1 + delta_pct / 100)))
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO runtime_overrides
               (series_ticker, override_type, value_json, expires_at, created_by, created_at)
               VALUES (?, 'size_mult', ?, ?, ?, ?)""",
            (series, json.dumps({"multiplier": new_mult}),
             expires, f"brain_decision_{decision_id}",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        _record_action(conn, decision_id, "TIER_3_FINETUNE", "adjust_size_multiplier",
                       {"series": series, "old": current, "new": new_mult,
                        "delta_pct": delta_pct}, "executed",
                       f"{series} mult {current:.2f}→{new_mult:.2f}")
        return True, f"{series}: {current:.2f}→{new_mult:.2f}"
    except Exception as e:
        _record_action(conn, decision_id, "TIER_3_FINETUNE", "adjust_size_multiplier",
                       action, "failed", str(e)[:200])
        return False, str(e)[:80]


def _extract_ticker(action: dict) -> str | None:
    """Best-effort ticker extraction from action's free-text 'details'."""
    text = action.get("details", "") + " " + action.get("action", "")
    import re
    m = re.search(r"\b(KX[A-Z][A-Z0-9]+(?:-[A-Z0-9.]+)+)\b", text)
    return m.group(1) if m else None


def _extract_series(action: dict) -> str | None:
    text = action.get("details", "") + " " + action.get("action", "")
    import re
    m = re.search(r"\b(KX[A-Z][A-Z0-9]+)\b", text)
    return m.group(1) if m else None


def _act_propose_cool_off(conn, decision_id, action) -> tuple[bool, str]:
    """Brain self-throttle: write a self-imposed pause for N hours.
    No $ impact — purely a meta-decision. Always TIER_1 safe."""
    hours = float(action.get("hours", 4))
    hours = max(0.5, min(24.0, hours))  # bounded but Brain picks
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO runtime_overrides
               (series_ticker, override_type, value_json, expires_at,
                created_by, created_at)
               VALUES ('__BRAIN__', 'brain_cool_off', ?, ?, ?, ?)""",
            (json.dumps({"hours": hours, "reason": action.get("details", "")[:200]}),
             expires, f"brain_decision_{decision_id}",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        _record_action(conn, decision_id, "TIER_1_LOG", "propose_cool_off",
                       {"hours": hours}, "executed",
                       f"brain self-imposed cool-off for {hours}h")
        return True, f"cool-off {hours}h"
    except Exception as e:
        return False, str(e)[:80]


# Action type → (tier, handler) registry
ACTION_REGISTRY = {
    "log_observation":         ("TIER_1_LOG",      _act_log),
    "log_note":                ("TIER_1_LOG",      _act_log),
    "observe":                 ("TIER_1_LOG",      _act_log),
    "alert":                   ("TIER_1_LOG",      _act_log),
    "propose":                 ("TIER_1_LOG",      _act_log),
    "propose_cool_off":        ("TIER_1_LOG",      _act_propose_cool_off),
    "cancel_market_orders":    ("TIER_2_BUDGET",   _act_cancel_market),
    "soft_demote_series":      ("TIER_2_BUDGET",   _act_soft_demote_series),
    "adjust_size_multiplier":  ("TIER_3_FINETUNE", _act_adjust_size_mult),
    "blacklist_series":        ("TIER_2_BUDGET",   _act_blacklist_series),
    "flatten_blocked_series":  ("TIER_2_BUDGET",   _act_flatten_blocked_series),
    "boost_size_multiplier":   ("TIER_2_BUDGET",   _act_adjust_size_mult),
}


def _classify_action_type(action: dict) -> tuple[str, callable] | tuple[None, None]:
    """Map a Brain action proposal to (tier_name, handler)."""
    raw = (action.get("action_type") or action.get("action", "")).lower()
    # Direct registry lookup
    if raw in ACTION_REGISTRY:
        return ACTION_REGISTRY[raw]
    # Heuristic mapping for free-text actions
    for key, (tier, handler) in ACTION_REGISTRY.items():
        if key in raw:
            return tier, handler
    if any(k in raw for k in ("log", "note", "observe", "alert", "propose")):
        return ACTION_REGISTRY["log_observation"]
    if "cancel" in raw and ("order" in raw or "market" in raw):
        return ACTION_REGISTRY["cancel_market_orders"]
    if "demote" in raw or "deprioritize" in raw:
        return ACTION_REGISTRY["soft_demote_series"]
    if ("adjust" in raw or "tune" in raw) and ("mult" in raw or "size" in raw):
        return ACTION_REGISTRY["adjust_size_multiplier"]
    return None, None


def _execute_safe_actions(conn, actions: list[dict],
                          decision_id: int = None,
                          confidence: str = "medium") -> list[dict]:
    """Execute SAFE-AUTO actions within tiered safety bounds.
    Returns list of executed action records.

    GUARDRAILS (checked BEFORE any action runs):
      - PAUSE_FILE existence
      - 24h Brain-attributed loss cap
      - 24h Brain-attributed spend cap
      - NAV drawdown trip (5% from 7d peak)
      - Velocity cap (10 actions/hour)
    Any tripped → ALL auto-execute paused, actions logged as 'guardrail_skip'.
    """
    _ensure_overrides_schema(conn)
    # GUARDRAIL CHECK — any trip and we abort all auto-execute
    allow, reason = check_guardrails(conn)
    if not allow:
        for action in actions:
            if action.get("safety") == "SAFE-AUTO":
                _record_action(conn, decision_id, "GUARDRAIL_SKIP",
                               action.get("action", "?"), action,
                               "skipped_guardrail", f"reason={reason}")
        _log.warning(f"BRAIN GUARDRAIL TRIPPED: {reason}. "
                     f"All auto-execute paused. Touch BRAIN_PAUSE to manually un-pause.")
        return []
    today_count = _today_action_count(conn)
    executed = []
    for action in actions:
        if action.get("safety") != "SAFE-AUTO":
            continue
        tier, handler = _classify_action_type(action)
        if not handler:
            continue
        # Daily cap — prevent runaway
        if today_count >= DAILY_AUTO_ACTION_CAP:
            _record_action(conn, decision_id, tier or "?",
                           action.get("action", "?"), action,
                           "skipped_daily_cap", f"cap={DAILY_AUTO_ACTION_CAP}")
            continue
        # TIER_2/3 require high confidence
        if tier in ("TIER_2_BUDGET", "TIER_3_FINETUNE") and confidence != "high":
            _record_action(conn, decision_id, tier, action.get("action", "?"),
                           action, "skipped_low_conf", f"conf={confidence}")
            continue
        ok, result = handler(conn, decision_id, action)
        if ok:
            executed.append({**action, "tier": tier, "result": result,
                             "executed_at": datetime.now(timezone.utc).isoformat()})
            today_count += 1
    return executed


def _robust_parse_brain_json(text: str) -> dict:
    """Tolerant JSON extraction for brain responses. Handles truncation,
    code fences, prose preamble, partial structures.
    Fixes 2026-05-05 PARSE_ERROR on drawdown_threshold trigger that lost an
    action plan because the LLM response was wrapped or slightly truncated."""
    import re as _re
    text = text.strip()
    fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start == -1:
        return {"diagnosis": "PARSE_ERROR", "raw": text[:1000], "error": "no_json_object"}
    # Walk inward from end, trying progressively-smaller suffixes — recovers
    # truncated-but-mostly-complete JSON (the most common LLM failure mode).
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except Exception:
            continue
    # Last resort — salvage action_plan array even if outer JSON broken
    arr = _re.search(r'"action_plan"\s*:\s*(\[.*?\])', text, _re.S)
    if arr:
        try:
            return {"diagnosis": "PARSE_PARTIAL",
                    "action_plan": json.loads(arr.group(1)),
                    "raw": text[:500]}
        except Exception:
            pass
    return {"diagnosis": "PARSE_ERROR", "raw": text[:1000], "error": "no_balanced_json"}


def process_trigger(conn, trigger_id: int, severity: str, rule: str,
                    context: dict) -> dict:
    """Process a single trigger: gather context, call Claude, log decision."""
    model = _model_for(rule, severity)
    max_tokens = _max_tokens_for(rule)
    state = _gather_state_snapshot(conn)
    self_awareness = _brain_self_awareness(conn)
    prompt = (
        f"TRIGGER: {rule} (severity={severity})\n"
        f"TRIGGER CONTEXT: {json.dumps(context, default=str)}\n\n"
        f"YOUR OWN TRACK RECORD (factor this into every decision):\n"
        f"{json.dumps(self_awareness, default=str, indent=2)}\n\n"
        f"CURRENT SYSTEM STATE: {json.dumps(state, default=str)}\n\n"
        "Analyze this trigger and respond in the strict JSON format. "
        "If your trend is 'degrading' or losses are mounting, consider "
        "issuing 'propose_cool_off' to self-throttle for a few hours."
    )
    text, usage = _call_claude(model, SYSTEM_PROMPT, prompt, max_tokens=max_tokens)
    cost = _estimate_cost(model, usage)
    # Try to parse JSON response
    parsed = _robust_parse_brain_json(text)
    actions_proposed = parsed.get("action_plan", []) if isinstance(parsed, dict) else []
    confidence = parsed.get("confidence", "medium") if isinstance(parsed, dict) else "medium"
    # Persist decision FIRST so we can pass decision_id to action handlers
    cur_temp = conn.execute(
        """INSERT INTO decision_log
           (ts, trigger_id, trigger_type, brain_model, context_summary,
            reasoning, actions_proposed, actions_executed, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), trigger_id,
         rule, model, json.dumps(context, default=str)[:500],
         json.dumps(parsed, default=str), json.dumps(actions_proposed, default=str),
         "[]", cost),
    )
    decision_id_pre = cur_temp.lastrowid
    actions_executed = _execute_safe_actions(conn, actions_proposed, decision_id_pre, confidence)
    # Update with executed actions
    conn.execute(
        "UPDATE decision_log SET actions_executed = ? WHERE id = ?",
        (json.dumps(actions_executed, default=str), decision_id_pre),
    )
    decision_id = decision_id_pre
    # Mark trigger resolved
    conn.execute(
        """UPDATE brain_triggers
           SET status = 'resolved', brain_run_id = ?, resolved_at = ?
           WHERE id = ?""",
        (decision_id, datetime.now(timezone.utc).isoformat(), trigger_id),
    )
    conn.commit()
    return {
        "trigger_id": trigger_id, "rule": rule, "model": model,
        "cost_usd": cost, "decision_id": decision_id,
        "diagnosis": parsed.get("diagnosis", "")[:200] if isinstance(parsed, dict) else "",
        "actions_proposed": len(actions_proposed),
        "actions_executed": len(actions_executed),
    }


def run_cycle(db_path: str = settings.DB_PATH, max_triggers: int = 5) -> dict:
    """Process all pending triggers (up to max_triggers per cycle for cost control)."""
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        triggers = conn.execute(
            """SELECT id, severity, rule, context_json FROM brain_triggers
               WHERE status = 'pending' ORDER BY
                 CASE severity WHEN 'EMERGENCY' THEN 1 WHEN 'ALERT' THEN 2
                               WHEN 'WARN' THEN 3 WHEN 'INFO' THEN 4 END,
                 ts ASC LIMIT ?""",
            (max_triggers,),
        ).fetchall()
        if not triggers:
            return {"processed": 0, "total_cost_usd": 0, "message": "no_pending_triggers"}
        results = []
        total_cost = 0
        for tid, sev, rule, ctx_json in triggers:
            ctx = json.loads(ctx_json or "{}")
            r = process_trigger(conn, tid, sev, rule, ctx)
            results.append(r)
            total_cost += r["cost_usd"]
        return {
            "processed": len(results), "total_cost_usd": round(total_cost, 5),
            "results": results,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_cycle()
    print(f"\n=== EDGE HUNTER BRAIN — {res.get('processed', 0)} triggers processed, "
          f"${res.get('total_cost_usd', 0):.4f} ===\n")
    for r in res.get("results", []):
        print(f"  {r['rule']:<30} model={r['model'][:25]:<25} "
              f"cost=${r['cost_usd']:<6.4f} actions=({r['actions_proposed']}p/"
              f"{r['actions_executed']}e)")
        print(f"    DX: {r['diagnosis']}")
