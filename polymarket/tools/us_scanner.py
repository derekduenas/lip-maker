"""Polymarket US profitable-setup scanner.

Ranks ACTIVE US markets by:
  1. Spread (tighter = more capture, but more competitive)
  2. Top-of-book size (smaller = our quote is bigger share)
  3. Mid-book volume (depth = liquidity)
  4. Time to settle (longer = more cycles, but exposure risk)
  5. State (must be MARKET_STATE_OPEN)

Polymarket US doesn't expose maker-rebate rates via public API yet,
so this scanner uses BOOK STRUCTURE as the EV proxy. Markets where
top-of-book size is small + spread is wide = high opportunity.

USAGE:
  python tools/us_scanner.py                 # top 20
  python tools/us_scanner.py --top 50
  python tools/us_scanner.py --my-size 100   # rank assuming we'd quote 100 contracts
  python tools/us_scanner.py --json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.pm_auth import _load_dotenv_simple
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_simple(str(PROJECT_ROOT / ".env"))

from polymarket_us import PolymarketUS
from engine.rewards_schedule import expected_daily_reward, classify_market


def get_client() -> PolymarketUS:
    return PolymarketUS(
        key_id=os.getenv("PM_API_KEY_ID"),
        secret_key=(PROJECT_ROOT / "config" / "polymarket_secret_key.b64").read_text().strip(),
    )


def list_active_events(client: PolymarketUS, limit: int = 200) -> list[dict]:
    """Find active events across ALL categories on Polymarket US.

    2026-04-29 REBUILD: dropped broken /search-by-keyword approach
    (search ignores q param) for the proven events.list with
    orderBy=start_date desc. This returns events sorted by most-recent
    creation, so all currently-active inventory surfaces in first pages.

    Paginate through, filter out closed/past-end. Returns events with
    nested .markets[] which extract_markets() will flatten downstream.
    """
    out: list[dict] = []
    seen_slugs: set[str] = set()
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = None
    pages = 0

    # ── Path 2: brute-force discover weather + macro slugs ──
    # These categories aren't in events.list (Polymarket-side limitation).
    # Generate today + tomorrow candidates, verify via markets.book.
    from datetime import timedelta
    cities = ["lax", "mia", "nyc", "mdw", "sfo", "atl", "bos", "chi", "dfw"]
    today = datetime.now(timezone.utc).date()
    candidate_slugs: list[str] = []
    for d_offset in (0, 1):
        date_str = (today + timedelta(days=d_offset)).isoformat()
        for city in cities:
            # Common temperature ranges; markets exist for each
            for cond in ("lt55f", "lt60f", "lt65f", "gte65f", "gte70f", "gte75f",
                         "gte80f", "gte85f", "gte90f"):
                candidate_slugs.append(f"tc-temp-{city}high-{date_str}-{cond}")
    # Macro/politics doc-listed slugs
    candidate_slugs.extend([
        "usfed-fomc-2026-04-29",
        "uscpi-apr2026yoy-2026-05-12",
    ])
    weather_added = 0
    for slug in candidate_slugs:
        if slug in seen_slugs:
            continue
        try:
            b = client.markets.book(slug)
            md = b.get("marketData", {}) if isinstance(b, dict) else {}
            if md.get("state") != "MARKET_STATE_OPEN":
                continue
        except Exception:
            continue
        seen_slugs.add(slug)
        out.append({
            "slug": slug,
            "title": slug,
            "category": "weather" if slug.startswith("tc-") else "macro",
            "endDate": (today + timedelta(days=1)).isoformat() + "T23:59:00Z",
            "markets": [{"slug": slug, "endDate": (today + timedelta(days=1)).isoformat() + "T23:59:00Z",
                         "question": slug}],
        })
        weather_added += 1
        if weather_added >= 30:  # cap brute-force
            break

    while pages < 8:  # cap at 8 pages × 200 = 1600 events
        params = {
            "limit": 200,
            "orderBy": "start_date",
            "orderDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = client.events.list(params)
        except Exception:
            break
        events = r.get("events", []) if isinstance(r, dict) else r
        if not events:
            break
        for e in events:
            slug = e.get("slug")
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            if e.get("closed") or e.get("archived"):
                continue
            end = e.get("endDate", "") or ""
            if end and end <= now_iso:
                continue
            out.append(e)
        cursor = r.get("nextCursor") if isinstance(r, dict) else None
        pages += 1
        if not cursor:
            break
        if len(out) >= limit:
            break
    return out[:limit]


def extract_markets(events: list[dict]) -> list[dict]:
    """Flatten nested markets out of events. Each event can have N markets."""
    out = []
    for e in events:
        markets = e.get("markets", []) if isinstance(e, dict) else []
        for m in markets:
            if isinstance(m, dict) and m.get("slug"):
                # Inherit event metadata for context
                m["_event_slug"] = e.get("slug")
                m["_event_title"] = e.get("title")
                m["_event_category"] = e.get("category")
                m["_series_slug"] = e.get("seriesSlug")
                out.append(m)
    return out


def fetch_book_metrics(client: PolymarketUS, slug: str) -> dict | None:
    """Pull book + extract metrics: best bid/ask, top size, depth, spread."""
    try:
        r = client.markets.book(slug)
    except Exception:
        return None
    md = r.get("marketData", {}) if isinstance(r, dict) else {}
    bids = md.get("bids", []) or []
    offers = md.get("offers", []) or []

    def _px(level) -> float | None:
        try:
            return float(level.get("px", {}).get("value"))
        except Exception:
            return None

    def _qty(level) -> float | None:
        try:
            return float(level.get("qty"))
        except Exception:
            return None

    if not bids or not offers:
        return None

    best_bid = _px(bids[0])
    best_ask = _px(offers[0])
    top_bid_size = _qty(bids[0]) or 0
    top_ask_size = _qty(offers[0]) or 0
    if best_bid is None or best_ask is None:
        return None

    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2

    # Total visible depth (sum of all bid/ask sizes)
    total_bid_depth = sum((_qty(b) or 0) for b in bids)
    total_ask_depth = sum((_qty(o) or 0) for o in offers)

    return {
        "best_bid":         best_bid,
        "best_ask":         best_ask,
        "spread":           round(spread, 4),
        "midpoint":         round(midpoint, 4),
        "top_bid_size":     int(top_bid_size),
        "top_ask_size":     int(top_ask_size),
        "total_bid_depth":  int(total_bid_depth),
        "total_ask_depth":  int(total_ask_depth),
        "state":            md.get("state"),
        "current_px":       float(md.get("stats", {}).get("currentPx", {}).get("value", 0))
                              if md.get("stats") else None,
    }


def score_setup(market: dict, book: dict, our_size: int = 100) -> dict:
    """Compute our setup score for one market.

    Higher score = more profitable opportunity.

    Components:
      - spread_capture: wider spread = more cents to capture as maker
      - share_at_best: our_size / (top_size + our_size). Higher = bigger share
      - depth_quality: deeper book = harder to fake-spoof
      - time_window: hours until settle (sweet spot 4-72h)
    """
    # PM docs: "No rewards distributed for canceled or postponed games"
    # Whitelist explicit OPEN state. Anything else (cancelled, settled,
    # paused, suspended) → drop. Defensive against PM adding new states.
    if not book or book.get("state") != "MARKET_STATE_OPEN":
        return None

    # Spread capture: each cent we're on top = ~1c per fill
    spread_cents = book["spread"] * 100
    if spread_cents <= 0:
        return None

    # Top-of-book share
    avg_top_size = (book["top_bid_size"] + book["top_ask_size"]) / 2
    share_at_best = our_size / max(1, avg_top_size + our_size)

    # Depth quality (avoid 1-quote spoofs)
    avg_depth = (book["total_bid_depth"] + book["total_ask_depth"]) / 2
    depth_factor = min(1.0, avg_depth / 1000)  # caps at 1.0 above 1k contracts

    # Time-to-settle window
    end_date_str = market.get("endDate") or market.get("_event_endDate", "")
    hours_to_settle = 0
    try:
        ed = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        hours_to_settle = (ed - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        pass

    # Time factor: peak in 4-72h window
    if hours_to_settle < 4:
        time_factor = max(0.1, hours_to_settle / 4)
    elif hours_to_settle <= 72:
        time_factor = 1.0
    else:
        time_factor = max(0.3, 72 / hours_to_settle)

    # ── REAL EV via reward schedule (#136 upgrade) ──
    # Use top-of-book size as proxy for competitor density at best.
    avg_top = int((book["top_bid_size"] + book["top_ask_size"]) / 2)
    ev = expected_daily_reward(market.get("slug"), hours_to_settle,
                               our_size=our_size, top_of_book_size=avg_top)

    # ── 2026-04-30: UNIFIED YIELD EQUATION ──
    # Single physics for both venues (engine/yield_equation.py).
    # Replaces ad-hoc multiplier patchwork. Uses SMOOTH qualify_prob
    # (sigmoid around target_size) instead of binary gate that
    # silently dropped marginal markets.
    from engine.yield_equation import MarketYield

    # Niche category boost (PM substring match — bug fixed today)
    cat = (ev.get("category") or "").lower()
    if "weather" in cat or "politics" in cat or "macro" in cat:
        niche_mult = 1.4
    elif "pga" in cat and ev.get("period") == "early":
        niche_mult = 1.3
    else:
        niche_mult = 1.0

    # Pool per day: convert ev's reward_pool/period back to per-day rate
    # rewards_schedule already exposes pool_usd which is per-period; for
    # current_period this is approximately per-day (our scheduled periods
    # are daily-bucketed). Fall back to ev["reward_pool"].
    pool_per_day = float(ev.get("reward_pool", 0) or 0)
    target_size = int(ev.get("target_size", 1) or 1)
    discount_factor = float(ev.get("discount_factor", 0.5) or 0.5)
    midpoint = book.get("midpoint", 0.5)

    y = MarketYield(
        market_id=market.get("slug", "?"),
        pool_per_day=pool_per_day,
        our_size=our_size,
        top_book_size=avg_top,
        target_size=target_size,
        discount_factor=discount_factor,
        hours_to_settle=hours_to_settle,
        midpoint=midpoint,
        calibration=0.10,            # PM empirical (revisit after first payouts)
        series_priority=niche_mult,  # niche_mult flows through as series boost
    )

    expected_usd_raw = ev.get("expected_usd", 0.0)  # legacy field for compat
    expected_usd_adjusted = y.expected_daily_rebate

    # Backward-compat fields for downstream code
    time_decay = y.time_factor
    adverse_cost_per_day = y.adverse_cost_per_day
    days_to_settle = max(0.04, hours_to_settle / 24)
    worst_leg_price = max(midpoint, 1.0 - midpoint)
    worst_position_usd = our_size * worst_leg_price

    # Concentration metric for visibility (no longer drives EV directly —
    # subsumed by smooth qualify_prob + observed_share blend in MarketYield)
    bid_depth_total = max(1.0, float(book.get("total_bid_depth") or 0))
    ask_depth_total = max(1.0, float(book.get("total_ask_depth") or 0))
    bid_top = float(book.get("top_bid_size") or 0)
    ask_top = float(book.get("top_ask_size") or 0)
    concentration = max(bid_top / bid_depth_total, ask_top / ask_depth_total)
    competition_mult = max(0.4, min(1.6, 1.0 + (0.50 - concentration) * 1.5))

    # 2026-04-30: dropped PM_MIN_PAYOUT $1 filter at this capital tier.
    # At $260 bankroll most markets have qualify_prob<<1 vs PM target_size,
    # making theoretical EV < $1. Filter would drop all candidates.
    # Better: quote for SNAPSHOT presence + fill data collection.
    # When capital ≥ $2K per market we'll meet target_size and re-enable filter.
    # Still drop genuinely zero-EV markets (degenerate books).
    if expected_usd_adjusted < 0.01:
        expected_usd_adjusted = 0.0

    capital_at_risk = max(0.01, worst_position_usd)
    yield_pct_daily = expected_usd_adjusted / capital_at_risk

    # Composite — heuristic; spread×share×depth×time still useful for
    # qualitative comparison in the scanner UI.
    raw_score = spread_cents * share_at_best * depth_factor * time_factor

    return {
        "slug":           market.get("slug"),
        "event_title":    market.get("_event_title", "")[:50],
        "category":       ev.get("category") or market.get("_event_category"),
        "period":         ev.get("period"),
        "reward_pool":    ev.get("reward_pool", 0),
        "target_size":    ev.get("target_size", 0),
        "qualifies":      ev.get("qualifies", False),
        "best_bid":       book["best_bid"],
        "best_ask":       book["best_ask"],
        "spread_cents":   round(spread_cents, 2),
        "midpoint":       book["midpoint"],
        "top_bid_size":   book["top_bid_size"],
        "top_ask_size":   book["top_ask_size"],
        "share_at_best":  round(share_at_best, 4),
        "total_depth":    int(book["total_bid_depth"] + book["total_ask_depth"]),
        "hours_to_settle": round(hours_to_settle, 1),
        "score":          round(raw_score, 4),
        # v1 raw EV (kept for visibility / backward-compat with dashboard)
        "expected_usd_raw": round(expected_usd_raw, 4),
        # v2 adjusted EV — what selection should USE
        "expected_usd":   round(expected_usd_adjusted, 4),
        "yield_pct":      round(yield_pct_daily * 100, 2),
        "time_decay":     round(time_decay, 3),
        "adverse_cost":   round(adverse_cost_per_day, 3),
        "competition_mult": round(competition_mult, 3),
        "concentration":  round(concentration, 3),
        "niche_mult":     round(niche_mult, 3),
        "ev_reason":      ev.get("reason", ""),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--my-size", type=int, default=100,
                   help="contracts we'd quote per market (default 100)")
    p.add_argument("--max-fetch", type=int, default=200,
                   help="max markets to fetch books for (rate limit safety)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    client = get_client()

    print(f"\n  pulling active events...", file=sys.stderr)
    events = list_active_events(client, limit=500)
    print(f"  {len(events)} active events", file=sys.stderr)

    markets = extract_markets(events)
    print(f"  {len(markets)} markets across them", file=sys.stderr)

    # Fetch book for top N (rate-limit safety: 60/min)
    n_fetch = min(len(markets), a.max_fetch)
    print(f"  fetching books for top {n_fetch} (rate-limit ~1/sec)", file=sys.stderr)

    scored = []
    for i, m in enumerate(markets[:n_fetch]):
        if i > 0 and i % 10 == 0:
            print(f"    progress: {i}/{n_fetch}", file=sys.stderr)
        book = fetch_book_metrics(client, m["slug"])
        if book is None:
            continue
        s = score_setup(m, book, our_size=a.my_size)
        if s:
            scored.append(s)

    # Primary sort: real expected $/day. Tiebreak by composite score.
    scored.sort(key=lambda r: (-r["expected_usd"], -r["score"]))

    if a.json:
        print(json.dumps(scored[:a.top], indent=2, default=str))
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  POLYMARKET US PROFITABLE SETUP SCANNER — {now}")
    print(f"  size assumption: {a.my_size} contracts per market")
    print(f"══════════════════════════════════════════════════════════════\n")
    print(f"  {'rank':>4s}  {'slug':<38s}  {'category':<22s}  {'period':<7s}  "
          f"{'pool':>7s}  {'tgt':>6s}  {'topB':>5s}  {'qual':<4s}  {'EV/d':>7s}")
    print(f"  {'-'*4}  {'-'*38}  {'-'*22}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*4}  {'-'*7}")
    total_ev = 0.0
    for i, r in enumerate(scored[:a.top], 1):
        qual_mark = "✓" if r.get("qualifies") else "✗"
        total_ev += r.get("expected_usd", 0)
        print(f"  {i:>4d}  {r['slug'][:38]:<38s}  "
              f"{(r['category'] or '?')[:22]:<22s}  "
              f"{(r.get('period') or '?')[:7]:<7s}  "
              f"${r.get('reward_pool', 0):>6.0f}  "
              f"{r.get('target_size', 0):>6d}  "
              f"{r['top_bid_size']:>5d}  "
              f"{qual_mark:<4s}  "
              f"${r.get('expected_usd', 0):>6.2f}")
    print(f"\n  TOTAL EV (top {a.top}, {a.my_size} contracts each): ${total_ev:.2f}/day")

    print(f"\n  ── COLUMN GUIDE ──")
    print(f"  spr   = spread in cents (tighter = more competition)")
    print(f"  topB/topA = contracts at top-of-book bid/ask (smaller = our size = bigger share)")
    print(f"  share = our share at best with {a.my_size} contracts vs current top")
    print(f"  hrs   = hours until settle (sweet spot 4-72)")
    print(f"  score = spread × share × depth × time_window")
    print()
    print(f"  CAVEAT: rewards $/day not exposed via API yet — score is BOOK-BASED proxy.")
    print(f"  For absolute $/day, cross-reference with your polymarket.us /rewards page.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
