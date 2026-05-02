"""Polymarket US reward schedule (static, derived from official docs).

Source: https://docs.polymarket.us — Liquidity Incentive Program section.
Updated 2026-04-28. Re-fetch when /v1/incentives API endpoint is live.

For each event category, defines:
  - reward_pool_per_period: $ per (early/day_of/live)
  - target_size_per_period: contracts to qualify
  - discount_factor_per_period: scoring factor

KEY USAGE:
  classify_market(slug) → (category, event_subtype)
  expected_daily_reward(category, current_phase, our_size, top_book_size)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# 2026-04-30 added: tripwire constant. Bump this every time you verify
# docs.polymarket.us hasn't changed pool/TS/DF values OR add new categories.
# Startup check in run_pm.py warns if (today - LAST_VERIFIED) > 14 days.
LAST_VERIFIED = date(2026, 4, 30)
DRIFT_WARN_DAYS = 14


@dataclass
class PeriodReward:
    pool_usd:        float    # reward pool for this period
    target_size:     int      # min aggregate size on each side
    discount_factor: float    # scoring exponent base (closer = higher)


@dataclass
class CategorySchedule:
    name:       str
    early:      PeriodReward | None
    day_of:     PeriodReward | None
    live:       PeriodReward | None
    pre_event:  PeriodReward | None    # for "futures" events (no day-of/live distinction)
    notes:      str = ""


# ── Per-category reward schedules (per-event/match/game) ─────────────────
SCHEDULES: dict[str, CategorySchedule] = {
    # ── BASKETBALL ──
    "nba_playoff_game": CategorySchedule(
        name="NBA Playoffs game",
        early=     PeriodReward(pool_usd=5_000,   target_size=25_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=10_000,  target_size=25_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=85_000,  target_size=25_000, discount_factor=0.30),
        pre_event=None,
        notes="NBA Playoffs/Play-In ML+Spreads+Totals combined $100k/game",
    ),
    "nba_series_futures": CategorySchedule(
        name="NBA series winner futures",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=10_000,  target_size=10_000, discount_factor=0.90),
        notes="$10k/day pro-rata across instruments",
    ),

    # ── BASEBALL ──
    "mlb_game": CategorySchedule(
        name="MLB game",
        early=     PeriodReward(pool_usd=1_250,   target_size=20_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=3_750,   target_size=20_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=20_000,  target_size=20_000, discount_factor=0.30),
        pre_event=None,
        notes="$25k/game total ML+Spreads+Totals",
    ),
    "mlb_futures": CategorySchedule(
        name="MLB futures",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=5_000,   target_size=2_500,  discount_factor=0.90),
    ),

    # ── HOCKEY ──
    "nhl_playoff_game": CategorySchedule(
        name="NHL Playoffs game",
        early=     PeriodReward(pool_usd=1_000,   target_size=20_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=4_000,   target_size=20_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=20_000,  target_size=20_000, discount_factor=0.30),
        pre_event=None,
    ),
    "nhl_series_futures": CategorySchedule(
        name="NHL series winner futures",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=5_000,   target_size=5_000,  discount_factor=0.90),
    ),

    # ── CRICKET ──
    "ipl_game": CategorySchedule(
        name="IPL game",
        early=     PeriodReward(pool_usd=1_000,   target_size=20_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=3_000,   target_size=20_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=36_000,  target_size=20_000, discount_factor=0.30),
        pre_event=None,
    ),

    # ── SOCCER ──
    "epl_game": CategorySchedule(
        name="EPL game",
        early=None, day_of=None,
        live=      PeriodReward(pool_usd=10_000,  target_size=10_000, discount_factor=0.30),
        pre_event=None,
        notes="LIVE only — split across 3 ML instruments",
    ),
    "epl_futures": CategorySchedule(
        name="EPL futures",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=2_500,   target_size=2_500,  discount_factor=0.90),
    ),
    "ucl_game": CategorySchedule(
        name="UCL match",
        early=     PeriodReward(pool_usd=1_000,   target_size=10_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=1_000,   target_size=10_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=8_000,   target_size=10_000, discount_factor=0.30),
        pre_event=None,
    ),
    "ucl_futures": CategorySchedule(
        name="UCL futures",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=2_500,   target_size=2_500,  discount_factor=0.90),
    ),
    "laliga_seriea_bundesliga_game": CategorySchedule(
        name="La Liga / Serie A / Bundesliga game",
        early=     PeriodReward(pool_usd=500,     target_size=10_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=500,     target_size=10_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=4_000,   target_size=10_000, discount_factor=0.30),
        pre_event=None,
    ),
    "mls_futures": CategorySchedule(
        name="MLS futures",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=2_500,   target_size=2_500,  discount_factor=0.90),
    ),

    # ── COMBAT ──
    "ufc_main_card": CategorySchedule(
        name="UFC main card match",
        early=     PeriodReward(pool_usd=2_500,   target_size=20_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=2_500,   target_size=20_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=20_000,  target_size=20_000, discount_factor=0.30),
        pre_event=None,
    ),
    "ufc_prelim": CategorySchedule(
        name="UFC preliminary match",
        early=     PeriodReward(pool_usd=500,     target_size=10_000, discount_factor=0.40),
        day_of=    PeriodReward(pool_usd=500,     target_size=10_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=4_000,   target_size=10_000, discount_factor=0.30),
        pre_event=None,
    ),

    # ── TENNIS ──
    "atp_wta_match": CategorySchedule(
        name="ATP / WTA match",
        early=None,
        day_of=    PeriodReward(pool_usd=500,     target_size=15_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=2_000,   target_size=15_000, discount_factor=0.30),
        pre_event=None,
        notes="$2,500/match total (pre-game $500 + live $2k)",
    ),

    # ── ESPORTS ──
    "lol_match": CategorySchedule(
        name="League of Legends match",
        early=None,
        day_of=    PeriodReward(pool_usd=1_000,   target_size=15_000, discount_factor=0.35),
        live=      PeriodReward(pool_usd=4_000,   target_size=15_000, discount_factor=0.30),
        pre_event=None,
    ),

    # ── GOLF ──
    "pga_tournament": CategorySchedule(
        name="PGA Tour tournament",
        early=     PeriodReward(pool_usd=25_000,  target_size=10_000, discount_factor=0.90),
        day_of=    PeriodReward(pool_usd=75_000,  target_size=10_000, discount_factor=0.40),
        live=      PeriodReward(pool_usd=50_000,  target_size=10_000, discount_factor=0.35),
        pre_event=None,
        notes="Multi-day: pre $25k + Day 1 $75k + Day 2-4 $50k each = $250k total",
    ),

    # ── WEATHER / CLIMATE ──
    "weather_daily": CategorySchedule(
        name="Daily weather market",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=5_000,   target_size=1_000,  discount_factor=0.90),
        notes="Per-market per-day, ends 11:59 PM ET",
    ),

    # ── POLITICS ──
    "politics_eligible": CategorySchedule(
        name="Politics (House/Senate Midterm Winner)",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=5_000,   target_size=10_000, discount_factor=0.90),
        notes="Pro-rata across instruments. Eligible: usho-midterms-2026-11-03, usse-midterms-2026-11-03",
    ),

    # ── MACRO ──
    "macro_eligible": CategorySchedule(
        name="Macro (CPI / FOMC)",
        early=None, day_of=None, live=None,
        pre_event= PeriodReward(pool_usd=10_000,  target_size=10_000, discount_factor=0.30),
        notes="Eligible: uscpi-apr2026yoy-2026-05-12, usfed-fomc-2026-04-29",
    ),
}


# ── Slug classification ─────────────────────────────────────────────────
def classify_market(slug: str) -> str | None:
    """Map a slug to a SCHEDULES key. Returns None if not recognized."""
    if not slug:
        return None
    s = slug.lower()

    # Sport prefixes — PM uses several event-type prefixes (aec/tec/atc/asc/tsc/cpic).
    # Match the SPORT keyword inside the slug, not just the leading prefix.
    if "-mlb-" in s or s.startswith("mlb-"):
        return "mlb_game"
    if "-nba-" in s or s.startswith("nba-"):
        return "nba_playoff_game"
    if "-nhl-" in s or s.startswith("nhl-"):
        return "nhl_playoff_game"
    if "-ipl-" in s or s.startswith("ipl-"):
        return "ipl_game"
    if "-epl-" in s or s.startswith("epl-"):
        return "epl_game"
    if "-ucl-" in s or s.startswith("ucl-"):
        return "ucl_game"
    if "-mls-" in s or s.startswith("mls-"):
        return "mls_futures"
    if "-ufc-" in s or s.startswith("ufc-"):
        return "ufc_main_card"
    if "-atp-" in s or "-wta-" in s:
        return "atp_wta_match"
    # Esports: LoL OR CS2 (docs explicitly list both with same schedule)
    if "-lol-" in s or s.startswith("lol-") or "-cs2-" in s or s.startswith("cs2-"):
        return "lol_match"
    if "-pga-" in s or s.startswith("pga-"):
        return "pga_tournament"
    if s.startswith("tc-temp-") or "weather" in s:
        return "weather_daily"
    # Politics + Macro: docs whitelist SPECIFIC slugs only — be strict to
    # avoid scoring non-enrolled lookalike markets ($0 expected reward).
    POLITICS_ELIGIBLE_EVENTS = (
        "usho-midterms-2026-11-03",
        "usse-midterms-2026-11-03",
    )
    if any(ev in s for ev in POLITICS_ELIGIBLE_EVENTS):
        return "politics_eligible"
    MACRO_ELIGIBLE_EVENTS = (
        "uscpi-apr2026yoy-2026-05-12",
        "usfed-fomc-2026-04-29",
    )
    if any(ev in s for ev in MACRO_ELIGIBLE_EVENTS):
        return "macro_eligible"
    # La Liga / Serie A / Bundesliga (PM uses atc- prefix + lal/sea/bun infix)
    # Bug fixed 2026-04-30: 87 markets were scoring $0 because we matched
    # wrong prefix. PM uses atc-lal-* (LaLiga), atc-sea-* (Serie A),
    # atc-bun-* (Bundesliga). Substring match catches both formats.
    if (any(s.startswith(p) for p in ("aec-laliga-", "aec-seriea-", "aec-bundesliga-"))
            or "-lal-" in s or "-sea-" in s or "-bun-" in s
            or "laliga" in s or "seriea" in s or "bundesliga" in s):
        return "laliga_seriea_bundesliga_game"

    return None


def schedule_for_slug(slug: str) -> CategorySchedule | None:
    cat = classify_market(slug)
    return SCHEDULES.get(cat) if cat else None


def current_period(schedule: CategorySchedule, hours_to_close: float | None) -> tuple[str, PeriodReward] | None:
    """Pick which time period applies right now.

    early:    > 6 hours before event
    day_of:   0-6 hours before event
    live:     in-progress (we treat negative hours_to_close as live, but
              the API would tell us state more directly)
    pre_event: futures markets (no timing distinction — same all the time)
    """
    if hours_to_close is None:
        # Default: try day_of, fall back to live
        for label in ("day_of", "live", "early", "pre_event"):
            r = getattr(schedule, label, None)
            if r:
                return label, r
        return None

    # Match correct period
    if schedule.pre_event:
        # Futures-style: only one period, always active
        return "pre_event", schedule.pre_event

    if hours_to_close <= 0:
        # In-progress (boundary fix: docs say "Live: from event start" — at
        # exact event start moment hours_to_close == 0 → live, not day_of).
        if schedule.live:
            return "live", schedule.live
    elif hours_to_close <= 6:
        if schedule.day_of:
            return "day_of", schedule.day_of
        if schedule.live:
            return "live", schedule.live
    else:
        if schedule.early:
            return "early", schedule.early
        if schedule.day_of:
            return "day_of", schedule.day_of
    # Fallback
    for label in ("live", "day_of", "early"):
        r = getattr(schedule, label, None)
        if r:
            return label, r
    return None


def expected_daily_reward(slug: str,
                          hours_to_close: float | None,
                          our_size: int,
                          top_of_book_size: int,
                          our_distance_cents: float = 0.0,
                          max_spread_cents: float = 2.0) -> dict:
    """Compute expected reward for our quote on this market.

    2026-05-02 PREDATOR Y5: respect spread position when computing share.
    PM's actual scoring is `spread_score = ((max_spread - distance)/max_spread)^2`.
    Previously we credited our full size regardless of distance from best,
    overstating EV by 4-100x when not at top-of-book. New behavior:
      our_distance_cents = 0   → factor 1.0  (at best)
      ≤ max_spread             → factor (1 - d/max)^2 (quadratic decay)
      > max_spread             → factor 0.0  (NO rebate eligible)
    Default args preserve old behavior for callers that don't pass distance.

    Returns dict with: category, period, qualifies, our_share, expected_usd.
    """
    sched = schedule_for_slug(slug)
    if not sched:
        return {"slug": slug, "category": None, "expected_usd": 0.0,
                "reason": "unknown_category"}

    pp = current_period(sched, hours_to_close)
    if not pp:
        return {"slug": slug, "category": sched.name, "expected_usd": 0.0,
                "reason": "no_active_period"}
    period_label, period = pp

    # Qualification check: aggregate book size must meet target_size.
    # We approximate aggregate ≈ top_of_book_size + our_size.
    # In reality there's depth beyond top, so this is conservative.
    aggregate_estimate = top_of_book_size + our_size
    qualifies = aggregate_estimate >= period.target_size

    if not qualifies:
        # Even if we're at-best, reward is $0 if target not met
        return {
            "slug": slug,
            "category": sched.name,
            "period": period_label,
            "reward_pool": period.pool_usd,
            "target_size": period.target_size,
            "aggregate_size_est": aggregate_estimate,
            "qualifies": False,
            "our_share": 0.0,
            "expected_usd": 0.0,
            "reason": f"target {period.target_size} not met (~{aggregate_estimate})",
        }

    # If at best, our_score = our_size × DF^0 = our_size.
    # Aggregate score at best = top_of_book_size + our_size.
    raw_share = our_size / max(1, top_of_book_size + our_size)

    # 2026-05-02 PREDATOR Y5: spread-position factor (quadratic decay).
    # Penalize quotes not at top of book. Outside max_spread → 0.
    if max_spread_cents <= 0 or our_distance_cents < 0:
        spread_factor = 1.0  # malformed input, preserve old behavior
    elif our_distance_cents >= max_spread_cents:
        spread_factor = 0.0  # outside max_spread = no rebate eligible
    else:
        spread_factor = ((max_spread_cents - our_distance_cents) / max_spread_cents) ** 2

    our_share = raw_share * spread_factor
    expected = our_share * period.pool_usd

    return {
        "slug": slug,
        "category": sched.name,
        "period": period_label,
        "reward_pool": period.pool_usd,
        "target_size": period.target_size,
        "discount_factor": period.discount_factor,
        "aggregate_size_est": aggregate_estimate,
        "qualifies": True,
        "raw_share": round(raw_share, 4),
        "spread_factor": round(spread_factor, 4),
        "our_share": round(our_share, 4),
        "expected_usd": round(expected, 2),
    }
