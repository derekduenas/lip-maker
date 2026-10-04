"""Patch 21: Polymarket US on the SAME engine as Kalshi (PAPER ONLY).

Patch 20 ran PM US as a separate, simpler side module. Patch 21 replaces it
with a *feed* for the production ``RunLoop``: PM US programs and books are
translated into the loop's frames, so PM US markets go through the same
selector (net $/day per $ capital, competition-aware share, durable/short
buckets, 48 h min-to-close, event window, sports policy), size ladder
allocator, re-peg, fast-move pull, cross guard, inventory skew, unpaired
caps, fill log + markouts, status, recorder, and the RiskEngine's unified
cross-venue caps. Only the reward rules differ (``ProgramParams.rules ==
"pmus"`` in engine/lip_scorer.py, reward-optimal rung in
mm/selector.pmus_side_rung).

Venue mapping. A PM US market is one instrument with bids and offers
(https://docs.polymarket.us/learn/trading/basics/buying-yes-vs-selling-no).
It is mapped onto the Kalshi-shaped book as yes_bids = PM bids and
no_bids = 100c - PM offers. Resting a "NO bid" at n cents == resting a PM
offer (sell) at 100 - n; its collateral is n cents per contract, as for a
Kalshi NO bid. Ticker = "PMUS:<marketSlug>".

Data (public gateway, no key; https://docs.polymarket.us/api-reference/introduction):
- GET /v1/incentives (paged with page_size/page_token, ALL pages, snake_case
  params; https://docs.polymarket.us/api-reference/incentives/overview). The
  documented host is api.polymarket.us (auth); the public gateway serves the
  same payload without a key (verified live 2026-10-01).
- GET /v1/market/slug/{slug} (endDate, orderPriceMinTickSize, marketType,
  gameStartTime, category).
- GET /v1/markets/{slug}/book (bids/offers + stats.sharesTraded/lastTradePx).
- GET /v1/markets/{slug}/settlement ({slug, settlement}: "Settlement price";
  404 = not found or not settled; public gateway, no auth;
  https://docs.polymarket.us/api-reference/markets/get-market-settlement),
  polled for held positions past close (``poll_settlements``). Winning
  contracts settle at $1.00, losing at $0.00
  (https://docs.polymarket.us/learn/markets/contract-settlement); the docs
  do not define the value further, so only exactly 1 (our YES = the PM US
  long side wins) or 0 is booked. Any other value, or none, leaves the
  position to RunLoop's release fallback (LIP_PMUS_UNSETTLED_RELEASE_S).
Streaming: the Markets WebSocket (wss://api.polymarket.us/v1/ws/markets)
requires API-key auth in the handshake
(https://docs.polymarket.us/api-reference/websocket/markets); there is no
public stream. So books are POLLED: quoted markets every LIP_PMUS_POLL_QUOTED_S
(3 s), other candidates round-robin, all GETs through one limiter at
LIP_PMUS_MAX_RPS (8/s; the public limit is 20 req/s per IP,
https://docs.polymarket.us/api-reference/rate-limits).

Reward rules used (https://docs.polymarket.us/incentives/liquidity):
- Periods (FAQ "What do the time periods mean?"): early/pre-game until 6 h
  before the event; day_of from 6 h before until start; live from start to
  settlement; daily_event midnight-to-midnight ET.
- rewardPool = "Total reward pool for this period in USD" on each market's
  TimePeriod (https://docs.polymarket.us/api-reference/incentives/overview).
  SHARED across the program's markets (verified 2026-10-02 against
  https://polymarket.us/rewards, the live schedule the docs point to):
  "Reward pool: The total amount paid out for a time period, shared across
  the program's markets - never summed per market" and "Reward pool, discount
  factor and target size are set per program (per time period) and apply to
  every market in it - they are not summed across markets"; that page lists
  each program with its market count (e.g. mlb_postseason_futures_20260929
  $100, 513 markets), which equals count_pool_members on the API records.
  So the pool is DIVIDED across the distinct active member markets carrying
  the same (programId, period) - LIP_PMUS_POOL_SPLIT=members, the default.
  The rule lives in polymarket/engine/pm_us_lip_scorer.py (count_pool_members
  / split_pool_usd) and is shared with that scorer. LIP_PMUS_POOL_SPLIT=market
  (whole pool per market) contradicts that page; kept only for comparison.
- $1 minimum payout ("Rewards under $1.00 are not paid out"): unit not named
  in the docs; applied per (market, ET date), the only payout granularity PM
  US publishes (earnings rows, status PAID/PENDING/SKIPPED, "Each entry sums
  all payouts for a single (market, date) pair"). A market whose split pool
  cannot reach $1 per ET day (or per period, if shorter) even at 100% share
  is screened out as below_min_payout before ranking and before any
  metadata fetch (max_payable_usd); selector.pmus_reward_per_day applies the
  same floor to our FULL-day payout (not to what is left of today).
- Scoring (score_snapshot_pmus): DF^(ticks from that side's best) x size;
  walk each side to Target Size on raw size, whole levels; per-side
  normalization; maxSpread only when the API sends it; volumeProgram
  periods are ignored everywhere (records, member counts, parser).
- Period types: early, pre_day, pre_game, day_of, live, daily_event, daily
  ("Daily" on polymarket.us/rewards; same per-day ET window as daily_event,
  accepted wherever daily_event is). Any
  other period type is rejected (no window is guessed for it).
  LIP_PMUS_PERIODS (comma list) restricts accepted period types further.
- daily_event with no `end` ("Ongoing programs omit end"): window = the
  current ET day clipped to `start`; ASSUMPTION (conservative): the pool is
  spread over the FULL ET day (rate = pool / 86400 s) even if the period
  started mid-day.
- Missing `end` on other periods: early -> eventStartTime - 6 h, day_of ->
  eventStartTime, live -> market endDate (longer window = lower rate).
Policy (same as Kalshi): day_of/live periods are single-event windows; they
are excluded (sports_match for sports/uncategorised, event_window otherwise:
the Kalshi engine pulls 6 h before an event and never quotes in-play).
Other periods are screened with the Kalshi exclusion_reason (48 h min to
close, sports short-dated, long-dated). Sub-cent tick markets are excluded
(the engine's book/scorer is a 1c grid).

Fills (no public trade tape): from book polls only.
- A polled book that crosses our paper price (best offer <= our bid, or
  best bid >= our offer) is filled by RunLoop's paper cross-fill (the same
  rule now applies to Kalshi books, see RunLoop._paper_cross_fill).
- An increase in stats.sharesTraded becomes at most one SYNTHETIC print at
  stats.lastTradePx; the taker side is inferred from the previous poll's
  best bid/offer (at/below best bid = seller, at/above best offer = buyer),
  else skipped. The delta is cumulative over everything traded between two
  polls (both sides, all prices), so the print size is CAPPED at the
  observed drop in resting depth on the side it would hit, at prices at or
  through the print price (bids >= px for a seller, offers <= px for a
  buyer), between the two polls. Without depth from the previous poll, or
  with no drop, no print is emitted. Prints carry "synthetic": True and the
  paper fills they cause carry it too (low fidelity). The production
  PaperFillSimulator queue model applies.

Book staleness. Books are REST polls. The gateway serves cached market data
(polymarket/execution/pm_book_gate.py: the public book endpoint is a ~30 s
cache; Cloudflare HIT with Age 15-16 s seen live), so a "fresh" poll can
show a book up to ~30 s old. The frame timestamp (``ts`` and ``data_ts``)
is therefore the OLDEST of the local receive time and the HTTP Date /
receive-Age headers when present (``ts_source`` = "server"), else the local
receive time (``ts_source`` = "local"). ``recv_ts`` and ``poll_latency_s``
(request round trip) are recorded on every frame. marketData.transactTime
("Transaction time of the market data",
docs.polymarket.us/api-reference/markets/get-market-book) is recorded as
``server_transact_ts`` but not used: whether it is the snapshot time or the
last-change time is unverified, and the latter would make every quiet book
look stale. LIP_PMUS_TS_SOURCE=local ignores the headers.

ORDER ENDPOINTS ARE HARD-DISABLED: the only HTTP client here is GET-only
with a path allowlist (incentives, market book/bbo/settlement, market by slug); any
path containing "order", a "."/".." segment, a backslash or a
percent-encoded "/", "\\", "." or "%" is refused (PMUSOrderBlocked). No API key is loaded.
RunLoop additionally refuses any non-paper action on a pmus market.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

_log = logging.getLogger("lip.pmus")
GATEWAY = "https://gateway.polymarket.us"
PREFIX = "PMUS:"
_ALLOWED = (
    re.compile(r"^/v1/incentives(\?[A-Za-z0-9_=&.\-%]*)?$"),
    re.compile(r"^/v1/markets/[A-Za-z0-9_.\-]+/(book|bbo|settlement)$"),
    re.compile(r"^/v1/market/slug/[A-Za-z0-9_.\-]+$"),
)
DAY_OF_S = 6 * 3600.0
KNOWN_PERIODS = ("daily_event", "daily", "early", "pre_day", "pre_game", "day_of", "live")
# "daily" (polymarket.us/rewards label "Daily", e.g. crypto_20260924,
# soccer_futures_20260924) is a per-day pool like "daily_event" ("Daily (per
# event)"); it shares that window and is accepted wherever daily_event is.
DAILY_PERIODS = ("daily_event", "daily")
MIN_PAYOUT_USD = 1.0  # "Rewards under $1.00 are not paid out" (docs.polymarket.us/incentives/liquidity)
SPORTS_CATEGORIES = {"spr", "sports", "sport", "esports"}


class PMUSOrderBlocked(RuntimeError):
    """Raised for any non-GET or non-allowlisted PM US request."""


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return _num("LIP_PMUS_PAPER_ENABLE", 0.0) > 0


def check_request(method: str, path: str) -> None:
    from mm.venues.readonly import path_has_traversal
    if str(method).upper() != "GET":
        raise PMUSOrderBlocked(f"pmus paper: {method} refused (read-only)")
    if "order" in path.lower() or path_has_traversal(path) or not any(rx.match(path) for rx in _ALLOWED):
        raise PMUSOrderBlocked(f"pmus paper: path not allowlisted: {path[:80]}")


class GatewayResponse(dict):
    """Decoded JSON body; ``headers`` keeps the HTTP Date / Age for staleness."""

    def __init__(self, data, headers=None) -> None:
        super().__init__(data if isinstance(data, dict) else {})
        self.headers = dict(headers or {})


def http_get(path: str, timeout: float = 20.0) -> dict:
    check_request("GET", path)
    req = urllib.request.Request(GATEWAY + path, method="GET",
                                 headers={"User-Agent": "lip-maker-paper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        hdrs = {k: v for k, v in resp.headers.items() if k.lower() in ("date", "age")}
        return GatewayResponse(json.loads(resp.read().decode("utf-8")), hdrs)


def server_ts_from_headers(headers, *, now: float) -> float | None:
    """Data time implied by HTTP caching headers: min(Date, now - Age).

    A shared cache either keeps the origin's Date (then Date ~= now - Age) or
    rewrites Date to its own clock (then now - Age is the data time); the
    minimum covers both without double counting. None without usable headers."""
    if not headers:
        return None
    low = {str(k).lower(): v for k, v in dict(headers).items()}
    out = []
    if low.get("date"):
        from email.utils import parsedate_to_datetime
        try:
            out.append(parsedate_to_datetime(str(low["date"])).timestamp())
        except (TypeError, ValueError, IndexError):
            pass
    if low.get("age") is not None:
        try:
            age = float(low["age"])
            if age >= 0:
                out.append(float(now) - age)
        except (TypeError, ValueError):
            pass
    return min(out) if out else None


def _ts(s) -> float | None:
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", txt)
    if m:  # nanosecond fractions -> microseconds (fromisoformat limit)
        frac = (m.group(2) or "")[:7]
        txt = m.group(1) + frac + m.group(3)
    if re.search(r"[+-]\d\d$", txt):
        txt += ":00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def et_day_bounds(now: float) -> tuple[float, float]:
    """[midnight, next midnight) America/New_York containing ``now`` (DST-aware)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    d = datetime.fromtimestamp(now, tz).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def allowed_periods() -> tuple:
    """Accepted PM US period types: LIP_PMUS_PERIODS (comma list) intersected
    with KNOWN_PERIODS, else every known type. Unknown types never pass."""
    raw = os.environ.get("LIP_PMUS_PERIODS")
    if raw is None or not raw.strip():
        return KNOWN_PERIODS
    want = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if "daily_event" in want and "daily" not in want:
        want.append("daily")
    return tuple(p for p in want if p in KNOWN_PERIODS)


def period_allowed(period: str) -> bool:
    return str(period or "") in allowed_periods()


def max_payable_usd(pool_usd: float, pool_seconds: float) -> float:
    """Upper bound on what one market can pay per payout unit: its whole
    (split) pool per ET day for windows longer than a day, else the whole
    period pool. Mirrors the $1 floor in mm.selector.reward_per_day."""
    if pool_usd <= 0 or pool_seconds <= 0:
        return 0.0
    return float(pool_usd) * min(1.0, 86400.0 / float(pool_seconds))


def program_window(tp: dict, event_ts: float | None, close_ts: float | None,
                   now: float) -> tuple[float, float, float] | None:
    """(window start, window end, pool seconds) for the period paying at ``now``, else None."""
    period = str(tp.get("period") or "")
    if not period_allowed(period):
        return None
    start, end = _ts(tp.get("start")), _ts(tp.get("end"))
    if period in DAILY_PERIODS:
        d0, d1 = et_day_bounds(now)
        ws = max(d0, start) if start is not None else d0
        we = min(d1, end) if end is not None else d1
        pool_s = d1 - d0
    else:
        if period in ("early", "pre_day", "pre_game"):
            ws = start
            we = end if end is not None else (event_ts - DAY_OF_S if event_ts else close_ts)
        elif period == "day_of":
            ws = start if start is not None else (event_ts - DAY_OF_S if event_ts else None)
            we = end if end is not None else event_ts
        else:  # live (period_allowed rejected every unknown type above)
            ws = start if start is not None else event_ts
            we = end if end is not None else close_ts
        if ws is None or we is None:
            return None
        pool_s = we - ws
    if ws is None or we is None or we <= ws or pool_s <= 0 or not (ws <= now < we):
        return None
    return ws, we, pool_s


def infer_tick(book: dict) -> float:
    """Tick from displayed prices (0.01, 0.005 or 0.001)."""
    pxs = [p for p, _s in book.get("bids", [])] + [p for p, _s in book.get("offers", [])]

    def on(t):
        return all(abs(p / t - round(p / t)) < 1e-6 for p in pxs)
    for t in (0.01, 0.005):
        if on(t):
            return t
    return 0.001


def parse_book(payload: dict) -> dict:
    md = payload.get("marketData") or payload

    def lv(rows):
        out = []
        for r in rows or []:
            try:
                out.append((float((r.get("px") or {}).get("value")), float(r.get("qty"))))
            except (TypeError, ValueError):
                continue
        return out
    bids = sorted(lv(md.get("bids")), key=lambda x: -x[0])
    offers = sorted(lv(md.get("offers")), key=lambda x: x[0])
    stats = md.get("stats") or {}
    last = (stats.get("lastTradePx") or {}).get("value")
    try:
        traded = float(stats.get("sharesTraded")) if stats.get("sharesTraded") is not None else None
    except (TypeError, ValueError):
        traded = None
    try:
        last = float(last) if last is not None else None
    except (TypeError, ValueError):
        last = None
    return {"bids": bids, "offers": offers, "state": md.get("state"),
            "shares_traded": traded, "last_px": last,
            "transact_ts": _ts(md.get("transactTime"))}


def book_frame(slug: str, book: dict, ts: float) -> dict:
    """PM US book -> Kalshi-shaped orderbook_snapshot frame for RunLoop."""
    yes = [[f"{p:.4f}", f"{q:.4f}"] for p, q in book["bids"] if q > 0]
    no = [[f"{1.0 - p:.4f}", f"{q:.4f}"] for p, q in book["offers"] if q > 0]
    return {"type": "orderbook_snapshot", "ts": ts, "sid": None, "seq": None, "venue": "pmus",
            "msg": {"market_ticker": PREFIX + slug, "yes_dollars_fp": yes, "no_dollars_fp": no}}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def poll_state(book: dict) -> dict:
    """What synth_trades needs from the previous poll: cumulative volume,
    touch, and the full depth on both sides."""
    return {"shares_traded": book.get("shares_traded"),
            "bb": book["bids"][0][0] if book.get("bids") else None,
            "bo": book["offers"][0][0] if book.get("offers") else None,
            "bids": [tuple(x) for x in book.get("bids") or []],
            "offers": [tuple(x) for x in book.get("offers") or []]}


def _depth_drop(prev_levels, levels, hit) -> float:
    before = sum(q for p, q in prev_levels if hit(p))
    after = sum(q for p, q in levels if hit(p))
    return before - after


def synth_trades(slug: str, prev: dict | None, book: dict, ts: float) -> list[dict]:
    """At most one SYNTHETIC trade-print frame from one poll.

    The stats.sharesTraded delta is everything that traded between the two
    polls (both sides, all prices), at one price: stats.lastTradePx. The
    taker side is inferred from the previous poll's touch (else skipped).
    The print size is capped at the drop in resting depth, between the two
    polls, on the side the print would hit at or through its price (bids >=
    px for a seller, offers <= px for a buyer). Without depth from the
    previous poll, or with no drop, nothing is printed. Cancels also drop
    depth, so the cap is an upper bound, not an estimate. Prints carry
    ``synthetic: True``. A book that crosses our paper quote is filled by
    RunLoop's paper cross-fill (venue-neutral), not here."""
    out = []
    ticker = PREFIX + slug
    eps = 1e-9
    if prev and prev.get("shares_traded") is not None and book.get("shares_traded") is not None \
            and book["shares_traded"] > prev["shares_traded"] + eps and book.get("last_px") is not None:
        qty = book["shares_traded"] - prev["shares_traded"]
        px = book["last_px"]
        taker = None
        if prev.get("bb") is not None and px <= prev["bb"] + eps:
            taker = "no"   # seller hit the bids  (our YES bid side)
        elif prev.get("bo") is not None and px >= prev["bo"] - eps:
            taker = "yes"  # buyer lifted offers (our NO side == PM offer)
        if taker == "no" and prev.get("bids") is not None:
            drop = _depth_drop(prev["bids"], book.get("bids") or [], lambda p: p >= px - eps)
        elif taker == "yes" and prev.get("offers") is not None:
            drop = _depth_drop(prev["offers"], book.get("offers") or [], lambda p: p <= px + eps)
        else:
            drop = 0.0     # no depth from the previous poll: no print
        qty = min(qty, drop)
        if taker and qty > eps:
            out.append({"trade_id": f"pmus:{slug}:{book['shares_traded']:.4f}", "ticker": ticker,
                        "count": qty, "yes_price_dollars": f"{px:.4f}", "no_price_dollars": f"{1 - px:.4f}",
                        "taker_side": taker, "created_time": _iso(ts), "synthetic": True})
    return [{"type": "trade", "ts": ts, "trade": t, "venue": "pmus", "synthetic": True} for t in out]


def _event_slug(slug: str) -> str:
    return slug.rsplit("-", 1)[0] if "-" in slug else slug


def records_to_programs(records: list, meta: dict, *, now: float) -> tuple[list, dict, list]:
    """Gateway records -> (program frames, stats, slugs needing metadata).

    Same policy as the Kalshi screen (mm/unattended/screen.py): the
    KalshiMarket probe goes through selector.exclusion_reason.
    """
    from mm.selector import KalshiMarket, exclusion_reason
    from mm.unattended.screen import rank_score
    from polymarket.engine.pm_us_lip_scorer import (
        count_pool_members, pool_key, pool_split_mode, split_pool_usd,
    )
    reasons: dict = {}
    frames, need = [], []
    split = pool_split_mode()
    members = count_pool_members(records)
    periods_ok = allowed_periods()

    def bump(why):
        reasons[why] = reasons.get(why, 0) + 1

    seen = set()
    for rec in records:
        slug = str(rec.get("marketSlug") or "")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        if rec.get("instrumentState") not in (None, "INSTRUMENT_STATE_OPEN"):
            bump("instrument_not_open")
            continue
        m = meta.get(slug)
        close_ts = None if m is None else m.get("close_ts")
        event_ts = _ts(rec.get("eventStartTime"))
        cat = str(rec.get("category") or (m or {}).get("category") or "").strip().lower()
        active = [tp for tp in rec.get("timePeriods") or []
                  if tp.get("programType", "liquidityProgram") == "liquidityProgram" and tp.get("status") == "active"]
        if not active:
            bump("no_program")
            continue
        if all(str(tp.get("period")) in ("day_of", "live") for tp in active):
            # single-event windows (6 h pre-start / in-play): same policy as Kalshi
            bump("sports_match" if (cat in SPORTS_CATEGORIES or not cat) else "event_window")
            continue
        active = [tp for tp in active if str(tp.get("period") or "") in periods_ok]
        if not active:
            bump("period_not_allowed")  # unknown type, or outside LIP_PMUS_PERIODS
            continue
        best = None
        for tp in active:
            if tp.get("programType", "liquidityProgram") != "liquidityProgram" or tp.get("status") != "active":
                continue
            try:
                pool, df, target = float(tp["rewardPool"]), float(tp["discountFactor"]), float(tp["targetSize"])
            except (KeyError, TypeError, ValueError):
                continue
            win = program_window(tp, event_ts, close_ts, now)
            if win is None:
                continue
            pool = split_pool_usd(pool, members.get(pool_key(tp.get("programId"), tp.get("period")), 1), split)
            rate = pool / win[2]
            if best is None or rate > best[0]:
                best = (rate, tp, win, pool, df, target)
        if best is None:
            if m is None and any(str(tp.get("period")) not in DAILY_PERIODS for tp in active):
                need.append(slug)  # window may depend on endDate
                bump("pending_meta")
            else:
                bump("no_open_period")
            continue
        _rate, tp, (ws, we, pool_s), pool, df, target = best
        period = str(tp.get("period"))
        if period in ("day_of", "live"):
            bump("sports_match" if (cat in SPORTS_CATEGORIES or not cat) else "event_window")
            continue
        if max_payable_usd(pool, pool_s) < MIN_PAYOUT_USD:
            # Even 100% of this market's (split) pool for a full day / period
            # is under PM US's $1 minimum payout: it can never pay, so it must
            # not take a candidate slot (selector.reward_per_day zeroes it).
            bump("below_min_payout")
            continue
        if m is None:
            need.append(slug)
            bump("pending_meta")
            continue
        if m.get("tick") is not None and abs(float(m["tick"]) - 0.01) > 1e-9:
            bump("subcent_tick")
            continue
        if not m.get("active", True) or m.get("closed"):
            bump("market_not_active")
            continue
        occ = m.get("occurrence_ts")
        eff = min(x for x in (close_ts, occ) if x is not None) if (close_ts or occ) else None
        days = None if eff is None else max(0.0, (eff - now) / 86400.0)
        sports = cat in SPORTS_CATEGORIES
        single = sports and str(m.get("market_type") or "").lower() not in ("futures", "future")
        series = PREFIX + "-".join(slug.split("-")[:2])
        probe = KalshiMarket(
            market=PREFIX + slug, series=series, period_reward_usd=pool, period_seconds=pool_s,
            seconds_left=max(0.0, we - now), discount_factor=df, target_size=target,
            days_to_settle=days, exchange_index=0, category="Sports" if sports else (cat or None),
            venue="pmus", max_spread_usd=None if tp.get("maxSpread") is None else float(tp["maxSpread"]),
            sports_single=single,
        )
        why = exclusion_reason(probe)
        if why:
            bump(re.sub(r"_[0-9.]+d$", "", why.split(":", 1)[0]))
            continue
        frame = {
            "kind": "program", "venue": "pmus", "market": PREFIX + slug, "series": series,
            "program_id": f"{tp.get('programId')}@{int(ws)}", "period_reward_usd": pool,
            "period_seconds": pool_s, "discount_factor": df, "target_size": target,
            "start_ts": ws, "end_ts": we, "close_ts": eff, "days_to_settle": days,
            "exchange_index": 0, "category": probe.category, "days_from_close": True,
            "occurrence_ts": occ, "event_ticker": PREFIX + _event_slug(slug),
            "max_spread_usd": probe.max_spread_usd, "sports_single": single,
            "fee_type": "pmus_maker_rebate", "pm_period": period,
        }
        rk = rank_score(frame, {"yes_bid": m.get("best_bid"), "yes_ask": m.get("best_ask")},
                        category=probe.category, days=days)
        frame["rank_score"] = round(rk["score"], 6)
        frame["rank_penalty_per_day"] = round(rk["penalty"], 6)
        frames.append(frame)
    frames.sort(key=lambda f: -f["rank_score"])
    top = int(_num("LIP_PMUS_CANDIDATES", 200))
    if len(frames) > top:
        reasons["below_candidate_top"] = len(frames) - top
        frames = frames[:top]
    stats = {"records": len(records), "markets": len(seen), "eligible": len(frames),
             "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])), "ts": now,
             "pool_split": split, "periods": list(periods_ok)}
    return frames, stats, need


def market_meta(payload: dict, now: float) -> dict:
    mk = payload.get("market") or payload
    game = _ts(mk.get("gameStartTime"))
    mtype = str(mk.get("marketType") or mk.get("sportsMarketType") or "")
    try:
        tick = float(mk.get("orderPriceMinTickSize")) if mk.get("orderPriceMinTickSize") is not None else None
    except (TypeError, ValueError):
        tick = None

    def q(name):
        try:
            return float((mk.get(name) or {}).get("value"))
        except (TypeError, ValueError, AttributeError):
            return None
    return {"close_ts": _ts(mk.get("endDate")), "tick": tick, "market_type": mtype,
            "category": mk.get("category"), "active": bool(mk.get("active", True)),
            "closed": bool(mk.get("closed", False)),
            # futures have no fixed start (docs: daily_event "an event without a fixed start")
            "occurrence_ts": None if mtype.lower() in ("futures", "future") else game,
            "best_bid": q("bestBidQuote"), "best_ask": q("bestAskQuote"), "fetched": now}


def settlement_result(slug: str, payload) -> str | None:
    """"yes" / "no" from a GET /v1/markets/{slug}/settlement payload: the
    long (our YES) side's settlement price, exactly 1 or 0, for this slug.
    None for anything else (another slug, a missing or non-binary value)."""
    if not isinstance(payload, dict) or str(payload.get("slug") or "") != slug:
        return None
    raw = payload.get("settlement")
    if isinstance(raw, dict):
        raw = raw.get("value")
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    if abs(px - 1.0) < 1e-9:
        return "yes"
    if abs(px) < 1e-9:
        return "no"
    return None


class PMUSFeed:
    """Background poller (thread ``lip-pmus``) that feeds PM US frames to RunLoop.ext_queue."""

    def __init__(self, loop, *, fetch=None, clock=time.time, sleep=None) -> None:
        self.loop = loop
        self.fetch = fetch or http_get
        self.clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self.meta: dict = {}
        self.fed: dict = {}       # market -> program_id fed
        self.cands: list = []     # slugs being polled
        self.prev: dict = {}      # slug -> last poll state
        self._next_poll: dict = {}
        self._last_get = 0.0
        self._rr = 0
        self._settle_checked: dict = {}
        self.stats = {"refreshes": 0, "pages": 0, "records": 0, "gets": 0, "errors": 0, "http_429": 0,
                      "settle_polls": 0, "settled": 0, "settle_unusable": 0,
                      "book_polls": 0, "trades_synth": 0, "trades_synth_dropped": 0, "blocked_writes": 0,
                      "last_error": None, "last_poll_latency_s": None,
                      "last_refresh": None, "last_book_ts": None, "refresh_s": None, "eligible": 0}
        self.screen: dict = {}
        self.started_ts = clock()

    # ---------------------------------------------------------------- http
    def _get(self, path: str) -> dict:
        gap = 1.0 / max(0.5, _num("LIP_PMUS_MAX_RPS", 8.0))
        wait = self._last_get + gap - self.clock()
        if wait > 0:
            self._sleep(wait)
        self._last_get = self.clock()
        self.stats["gets"] += 1
        try:
            return self.fetch(path)
        except PMUSOrderBlocked:
            self.stats["blocked_writes"] += 1
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == 429:
                self.stats["http_429"] += 1
                self._sleep(2.0)  # docs: stop, wait >= 1 s, back off
            raise

    def _put(self, frame: dict) -> None:
        self.loop.ext_queue.put(frame)

    # ------------------------------------------------------------ programs
    def refresh(self) -> None:
        t0 = self.clock()
        recs, tok = [], None
        for _ in range(int(_num("LIP_PMUS_MAX_PAGES", 1000))):
            q = "/v1/incentives?page_size=100&statuses=active&program_type=liquidityProgram"
            if tok:
                q += "&page_token=" + urllib.request.quote(str(tok), safe="")
            d = self._get(q)
            self.stats["pages"] += 1
            recs.extend(d.get("programs") or [])
            tok = d.get("nextPageToken")
            if not tok or self._stop.is_set():
                break
            self.poll_due()  # keep quoted books fresh while paging
        now = self.clock()
        frames, stats, need = records_to_programs(recs, self.meta, now=now)
        ttl = _num("LIP_PMUS_META_TTL_S", 6 * 3600.0)
        stale = [s for s, m in self.meta.items() if now - float(m.get("fetched") or 0) > ttl]
        for slug in (need + stale)[: int(_num("LIP_PMUS_META_MAX", 300))]:
            try:
                self.meta[slug] = market_meta(self._get(f"/v1/market/slug/{slug}"), self.clock())
            except PMUSOrderBlocked:
                raise
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"meta:{type(exc).__name__}"
            self.poll_due()
        if need:
            frames, stats, _need = records_to_programs(recs, self.meta, now=self.clock())
        for f in frames:
            if self.fed.get(f["market"]) != f["program_id"]:
                self._put(f)
                self.fed[f["market"]] = f["program_id"]
        self.cands = [f["market"][len(PREFIX):] for f in frames]
        self.stats.update(records=len(recs), refreshes=self.stats["refreshes"] + 1,
                          last_refresh=self.clock(), refresh_s=round(self.clock() - t0, 1),
                          eligible=len(frames))
        self.screen = stats
        self._put({"kind": "screen_pmus", "stats": stats})
        _log.info("pmus refresh: %d records, %d eligible, reasons %s", len(recs), len(frames), stats["reasons"])

    # --------------------------------------------------------------- books
    def poll_due(self) -> int:
        if not self.cands:
            return 0
        now = self.clock()
        quoted = {m[len(PREFIX):] for m in getattr(self.loop, "resting_view", frozenset()) if m.startswith(PREFIX)}
        fast, slow = _num("LIP_PMUS_POLL_QUOTED_S", 3.0), _num("LIP_PMUS_POLL_S", 30.0)
        due = [s for s in self.cands if now >= self._next_poll.get(s, 0.0)]
        due.sort(key=lambda s: (s not in quoted, self._next_poll.get(s, 0.0)))
        n = 0
        for slug in due[: int(_num("LIP_PMUS_POLL_BATCH", 8))]:
            self._next_poll[slug] = self.clock() + (fast if slug in quoted else slow)
            try:
                self.poll_book(slug)
                n += 1
            except PMUSOrderBlocked:
                raise
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"book:{type(exc).__name__}"
        return n

    def poll_book(self, slug: str) -> None:
        t0 = self.clock()
        payload = self._get(f"/v1/markets/{slug}/book")
        recv = self.clock()
        latency = max(0.0, recv - t0)
        book = parse_book(payload)
        server = None
        if os.environ.get("LIP_PMUS_TS_SOURCE", "server").strip().lower() != "local":
            server = server_ts_from_headers(getattr(payload, "headers", None), now=recv)
        ts = recv if server is None else min(recv, server)
        self.stats["book_polls"] += 1
        self.stats["last_poll_latency_s"] = round(latency, 4)
        if book.get("state") not in (None, "MARKET_STATE_OPEN"):
            return  # not open: no snapshot -> the loop pulls on a stale book
        # prints first (they happened before this book state), then the book
        prev = self.prev.get(slug)
        traded = (prev and prev.get("shares_traded") is not None and book.get("shares_traded") is not None
                  and book["shares_traded"] > prev["shares_traded"])
        prints = synth_trades(slug, prev, book, ts)
        if traded and not prints:
            self.stats["trades_synth_dropped"] += 1
        for tr in prints:
            self.stats["trades_synth"] += 1
            self._put(tr)
        frame = book_frame(slug, book, ts)
        frame.update({"data_ts": ts, "recv_ts": recv, "poll_latency_s": round(latency, 4),
                      "ts_source": "local" if server is None else "server",
                      "server_transact_ts": book.get("transact_ts")})
        self._put(frame)
        self.prev[slug] = poll_state(book)
        self.stats["last_book_ts"] = ts

    # ---------------------------------------------------------- settlement
    def poll_settlements(self) -> int:
        """GET /v1/markets/{slug}/settlement for held PM US positions the
        loop lists as due (``loop.settle_view``: past close or no program
        left), each at most once per LIP_PMUS_SETTLE_POLL_S (900 s). A
        settlement of exactly 1 / 0 for the same slug becomes a
        ``settlement`` frame (yes / no) on the loop's queue. Returns the
        number queued."""
        now = self.clock()
        every = _num("LIP_PMUS_SETTLE_POLL_S", 900.0)
        due = [m[len(PREFIX):] for m, venue in getattr(self.loop, "settle_view", ())
               if venue == "pmus" and m.startswith(PREFIX)]
        n = 0
        for slug in due:
            if now - self._settle_checked.get(slug, -1e18) < every:
                continue
            self._settle_checked[slug] = now
            self.stats["settle_polls"] += 1
            try:
                payload = self._get(f"/v1/markets/{slug}/settlement")
            except PMUSOrderBlocked:
                raise
            except Exception as exc:
                if getattr(exc, "code", None) != 404:  # 404: not settled yet
                    self.stats["errors"] += 1
                    self.stats["last_error"] = f"settlement:{type(exc).__name__}"
                continue
            result = settlement_result(slug, payload)
            if result is None:
                self.stats["settle_unusable"] += 1
                _log.warning("pmus settlement for %s not booked: %r", slug, dict(payload or {}))
                continue
            self._put({"kind": "settlement", "market": PREFIX + slug, "result": result,
                       "ts": now, "source": "pmus_gateway"})
            self.stats["settled"] += 1
            n += 1
        return n

    # -------------------------------------------------------------- thread
    def start(self) -> "PMUSFeed":
        self._thread = threading.Thread(target=self._run, name="lip-pmus", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        refresh_s = max(120.0, _num("LIP_PMUS_REFRESH_S", 900.0))
        next_refresh = 0.0
        next_settle = 0.0
        while not self._stop.is_set():
            try:
                if self.clock() >= next_refresh:
                    next_refresh = self.clock() + refresh_s
                    try:
                        self.refresh()
                    except Exception:
                        # A failed refresh (gateway blip at start) is retried
                        # soon, not after a whole LIP_PMUS_REFRESH_S.
                        next_refresh = self.clock() + min(refresh_s, max(5.0, _num("LIP_PMUS_REFRESH_RETRY_S", 30.0)))
                        raise
                elif self.clock() >= next_settle:
                    next_settle = self.clock() + 60.0
                    self.poll_settlements()
                elif not self.poll_due():
                    self._sleep(0.25)
            except PMUSOrderBlocked:
                _log.error("pmus blocked a non-allowlisted request (bug); feed continues read-only")
                self._sleep(5.0)
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = type(exc).__name__
                _log.warning("pmus feed cycle failed: %s", type(exc).__name__)
                self._sleep(5.0)

    def summary(self) -> dict:
        now = self.clock()
        last = self.stats.get("last_book_ts")
        return {"enabled": True, "paper": True, "engine": "shared RunLoop (patch 21)",
                "order_endpoints": "hard-disabled (GET allowlist)", "feed": "poll (WS requires auth)",
                "candidates": len(self.cands), "book_age_s": None if last is None else round(now - last, 1),
                "screen": self.screen, **self.stats}
