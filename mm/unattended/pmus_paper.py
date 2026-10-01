"""Patch 20: Polymarket US paper maker venue (read-only data, PAPER ONLY).

What it does (background thread ``lip-pmus``, off unless LIP_PMUS_PAPER_ENABLE=1):
1. Every LIP_PMUS_REFRESH_S (900 s) pulls active liquidity programs from the
   PUBLIC gateway ``GET https://gateway.polymarket.us/v1/incentives`` (no key),
   paged up to LIP_PMUS_MAX_PAGES (20 x 100 markets).
2. Per market: effective pool = rewardPool / members of the same programId
   (the gateway repeats one pool on every member; conservative split), rate =
   effective pool / period seconds. Periods allowed: LIP_PMUS_PERIODS
   (default ``daily_event,early,pre_day``; ``day_of``/``live`` single-game
   sports are excluded by default for the same adverse-selection reason the
   Kalshi side drops single-game sports).
3. Top LIP_PMUS_CANDIDATES (30) by rate get a public book
   ``GET /v1/markets/{slug}/book``. A paper quote joins the best bid (buy YES)
   and the best offer (buy NO at 1 - offer), never crossing. Size is picked
   from LIP_PMUS_SIZES to maximise expected (reward + maker rebate) per $ under
   the caps; scoring uses polymarket.engine.pm_us_lip_scorer (official LIP
   formula: DF^ticks from best x size, walk to Target Size, optional Max Spread).
4. Unified risk: PM capital <= min(LIP_PMUS_BUDGET_USD (300),
   gross_cap x alloc fraction - the full Kalshi allocation budget), so Kalshi +
   PM can never exceed the shared gross cap; per-market LIP_PMUS_MARKET_CAP_USD
   (100); unpaired inventory caps reuse LIP_MARKET_INV_CAP_USD ($25) and
   LIP_EVENT_INV_CAP_USD ($75) and block the side that adds.
5. Fills (pessimistic, book polling only, no trade tape): a side is filled in
   full at our price only when the book trades THROUGH it between polls (best
   offer <= our bid, or best bid >= our offer). Maker rebate 0.0125*C*p*(1-p).
   Markout = MTM vs current mid. Rewards accrue left-Riemann between polls
   (gaps > 120 s are not credited).

Order endpoints are HARD-DISABLED: the only HTTP client here is GET-only with
a path allowlist (incentives, market book/bbo, market by slug). There is no
code path to POST/DELETE anything, and no API key is loaded or needed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

_log = logging.getLogger("lip.pmus")
GATEWAY = "https://gateway.polymarket.us"
_ALLOWED = (
    re.compile(r"^/v1/incentives(\?[A-Za-z0-9_=&.\-%]*)?$"),
    re.compile(r"^/v1/markets/[A-Za-z0-9_.\-]+/(book|bbo)$"),
    re.compile(r"^/v1/market/slug/[A-Za-z0-9_.\-]+$"),
)
PERIOD_DEFAULT_S = {"live": 4 * 3600.0, "day_of": 6 * 3600.0, "daily_event": 86400.0,
                    "early": 86400.0, "pre_day": 86400.0}


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
    if str(method).upper() != "GET":
        raise PMUSOrderBlocked(f"pmus paper: {method} refused (read-only)")
    if "order" in path.lower() or not any(rx.match(path) for rx in _ALLOWED):
        raise PMUSOrderBlocked(f"pmus paper: path not allowlisted: {path[:80]}")


def http_get(path: str, timeout: float = 20.0) -> dict:
    check_request("GET", path)
    req = urllib.request.Request(GATEWAY + path, method="GET",
                                 headers={"User-Agent": "lip-maker-paper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ts(s) -> float | None:
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", txt):
        txt += ":00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


DAY_OF_S = 6 * 3600.0


def period_open(period: str, now: float, start: float | None, end: float | None,
                event_ts: float | None) -> bool:
    """Is this LIP time period paying right now? (docs: early/pre-game until
    6 h before the event, day-of from 6 h before until start, live from start
    until settlement, daily_event midnight-to-midnight ET)."""
    if start is not None and now < start:
        return False
    if end is not None and now >= end:
        return False
    if event_ts is None or period == "daily_event":
        return True
    if period == "live":
        return now >= event_ts
    if period == "day_of":
        return event_ts - DAY_OF_S <= now < event_ts
    if period in ("early", "pre_day"):
        return now < event_ts - DAY_OF_S
    return True


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
    return {"bids": bids, "offers": offers, "state": md.get("state")}


def programs_from_incentives(records: list, *, now: float, periods: set) -> list:
    """Flatten gateway records -> one row per (market, active liquidity period)."""
    rows, members = [], {}
    for rec in records:
        for tp in rec.get("timePeriods") or []:
            if tp.get("programType", "liquidityProgram") != "liquidityProgram" or tp.get("status") != "active":
                continue
            members[tp.get("programId")] = members.get(tp.get("programId"), 0) + 1
    for rec in records:
        slug = rec.get("marketSlug") or ""
        if rec.get("instrumentState") not in (None, "INSTRUMENT_STATE_OPEN"):
            continue
        for tp in rec.get("timePeriods") or []:
            if tp.get("programType", "liquidityProgram") != "liquidityProgram" or tp.get("status") != "active":
                continue
            period = str(tp.get("period") or "")
            if periods and period not in periods:
                continue
            try:
                pool = float(tp["rewardPool"])
                df = float(tp["discountFactor"])
                target = float(tp["targetSize"])
            except (KeyError, TypeError, ValueError):
                continue
            start, end = _ts(tp.get("start")), _ts(tp.get("end"))
            if not period_open(period, now, start, end, _ts(rec.get("eventStartTime"))):
                continue
            secs = (end - start) if (start and end and end > start) else PERIOD_DEFAULT_S.get(period, 86400.0)
            n = max(1, members.get(tp.get("programId"), 1))
            rows.append({
                "slug": slug, "program_id": tp.get("programId"), "period": period,
                "pool_usd": pool, "n_markets": n, "pool_eff_usd": pool / n,
                "period_s": secs, "rate_per_s": pool / n / secs, "df": df, "target": target,
                "max_spread": None if tp.get("maxSpread") is None else float(tp["maxSpread"]),
                "category": rec.get("category"), "event": slug.rsplit("-", 1)[0],
            })
    best = {}
    for r in rows:  # one row per market: highest rate
        if r["slug"] not in best or r["rate_per_s"] > best[r["slug"]]["rate_per_s"]:
            best[r["slug"]] = r
    return sorted(best.values(), key=lambda r: -r["rate_per_s"])


def evaluate(prog: dict, book: dict, size: float, tick: float | None = None,
             sides: tuple = ("yes", "no")) -> dict | None:
    """Our paper quote joining best bid / best offer at ``size``; share & $/s."""
    from polymarket.engine.pm_us_lip_scorer import Order, score_snapshot
    if not book["bids"] or not book["offers"]:
        return None
    tick = tick or infer_tick(book)
    bb, bo = book["bids"][0][0], book["offers"][0][0]
    if bo - bb < tick - 1e-9:
        return None  # locked/crossed: joining would take
    bid_px, ask_px = bb, bo
    bids = [Order(p, s) for p, s in book["bids"]] + ([Order(bid_px, size, ours=True)] if "yes" in sides else [])
    asks = [Order(p, s) for p, s in book["offers"]] + ([Order(ask_px, size, ours=True)] if "no" in sides else [])
    snap = score_snapshot(bids, asks, tick=tick, discount_factor=prog["df"],
                          target_size=prog["target"], max_spread_usd=prog["max_spread"])
    share = snap.our_share
    capital = (size * bid_px if "yes" in sides else 0.0) + (size * (1.0 - ask_px) if "no" in sides else 0.0)
    return {"bid_px": bid_px, "ask_px": ask_px, "size": size, "share": share,
            "paid": snap.paid, "reason": snap.reason, "capital": capital,
            "usd_per_s": share * prog["rate_per_s"],
            "mid": (bb + bo) / 2.0}


class PMUSPaperVenue:
    def __init__(self, *, kalshi_loop=None, fetch=None, clock=time.time) -> None:
        self.kalshi_loop = kalshi_loop
        self.fetch = fetch or http_get
        self.clock = clock
        self.programs: list = []
        self.quotes: dict = {}     # slug -> quote dict
        self.position: dict = {}   # slug -> {"yes","no","yes_cost","no_cost","event"}
        self.fills: list = []
        self.reward_usd = 0.0
        self.rebate_usd = 0.0
        self.stats = {"refreshes": 0, "gets": 0, "errors": 0, "polls": 0, "last_refresh": None,
                      "records": 0, "blocked_writes": 0, "last_error": None}
        self.started_ts = clock()
        self._books: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- caps
    def headroom_usd(self) -> float:
        budget = _num("LIP_PMUS_BUDGET_USD", 300.0)
        loop = self.kalshi_loop
        if loop is not None:
            try:
                from mm.unattended.loop import alloc_cap_fraction
                lim = loop.risk.limits
                gross = float(lim.gross_usd) * alloc_cap_fraction()
                kal = max(float(loop.alloc_budget_usd or 0.0),
                          float(sum(float(v) for v in list(loop.committed.values()))))
                budget = min(budget, max(0.0, gross - kal))
            except Exception:
                pass
        return budget

    def _unpaired_usd(self, slug: str) -> float:
        p = self.position.get(slug)
        if not p:
            return 0.0
        out = 0.0
        for side, other in (("yes", "no"), ("no", "yes")):
            extra = p[side] - p[other]
            if extra > 0 and p[side] > 0:
                out += extra * p[f"{side}_cost"] / p[side]
        return out

    def side_blocked(self, slug: str, side: str, event: str) -> str:
        p = self.position.get(slug) or {"yes": 0.0, "no": 0.0}
        other = "no" if side == "yes" else "yes"
        adds = p.get(side, 0.0) - p.get(other, 0.0) >= 0
        cap_m = _num("LIP_MARKET_INV_CAP_USD", 25.0)
        if adds and p.get(side, 0.0) > p.get(other, 0.0) and self._unpaired_usd(slug) >= cap_m:
            return "market_inventory"
        cap_e = _num("LIP_EVENT_INV_CAP_USD", 75.0)
        held = sum(self._unpaired_usd(s) for s, q in self.position.items() if q.get("event") == event)
        if adds and held >= cap_e:
            return "event_inventory"
        return ""

    # ------------------------------------------------------------- network
    def _get(self, path: str) -> dict:
        self.stats["gets"] += 1
        return self.fetch(path)

    def refresh_programs(self) -> None:
        periods = {p.strip() for p in os.environ.get(
            "LIP_PMUS_PERIODS", "daily_event,early,pre_day").split(",") if p.strip()}
        recs, tok = [], None
        for _ in range(int(_num("LIP_PMUS_MAX_PAGES", 20))):
            q = "/v1/incentives?page_size=100&statuses=active&program_type=liquidityProgram"
            if tok:
                q += "&page_token=" + urllib.request.quote(str(tok), safe="")
            d = self._get(q)
            recs.extend(d.get("programs") or [])
            tok = d.get("nextPageToken")
            if not tok:
                break
            if self._stop.wait(0.3):
                break
        self.stats["records"] = len(recs)
        self.programs = programs_from_incentives(recs, now=self.clock(), periods=periods)
        self.stats["refreshes"] += 1
        self.stats["last_refresh"] = self.clock()

    def select(self) -> None:
        """Pick paper quotes for the top candidates under the caps."""
        sizes = [float(x) for x in os.environ.get("LIP_PMUS_SIZES", "50,100,200,500").split(",") if x.strip()]
        cap_m = _num("LIP_PMUS_MARKET_CAP_USD", 100.0)
        cands = []
        for prog in self.programs[: int(_num("LIP_PMUS_CANDIDATES", 30))]:
            try:
                book = parse_book(self._get(f"/v1/markets/{prog['slug']}/book"))
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = type(e).__name__
                continue
            self._books[prog["slug"]] = book
            best = None
            for s in sizes:
                ev = evaluate(prog, book, s)
                if ev is None or ev["capital"] > cap_m or ev["capital"] <= 0 or ev["usd_per_s"] <= 0:
                    continue
                ratio = ev["usd_per_s"] / ev["capital"]
                if best is None or ratio > best[0] + 1e-15 or (abs(ratio - best[0]) < 1e-15 and s > best[1]["size"]):
                    best = (ratio, ev)
            if best is not None:
                cands.append((best[0], prog, best[1]))
            if self._stop.wait(0.4):
                return
        cands.sort(key=lambda x: -x[0])
        room = self.headroom_usd()
        new = {}
        for ratio, prog, ev in cands:
            if ev["capital"] > room:
                continue
            sides = tuple(sd for sd in ("yes", "no") if not self.side_blocked(prog["slug"], sd, prog["event"]))
            if not sides:
                continue
            if sides != ("yes", "no"):
                ev = evaluate(prog, self._books[prog["slug"]], ev["size"], sides=sides)
                if ev is None or ev["capital"] > room:
                    continue
            room -= ev["capital"]
            new[prog["slug"]] = dict(ev, prog=prog, sides=sides, ts=self.clock(), score_ts=self.clock())
        with self._lock:
            self.quotes = new

    def poll_once(self) -> None:
        """Refresh each quoted book: accrue reward, detect trade-through fills."""
        from mm.accounting import pm_us_maker_rebate_usd
        for slug in list(self.quotes):
            q = self.quotes.get(slug)
            if q is None:
                continue
            try:
                book = parse_book(self._get(f"/v1/markets/{slug}/book"))
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = type(e).__name__
                continue
            now = self.clock()
            dt = min(120.0, max(0.0, now - q["score_ts"]))
            self.reward_usd += dt * q["usd_per_s"]
            q["score_ts"] = now
            self._books[slug] = book
            bb = book["bids"][0][0] if book["bids"] else None
            bo = book["offers"][0][0] if book["offers"] else None
            filled = []
            if "yes" in q["sides"] and bo is not None and bo <= q["bid_px"] + 1e-9:
                filled.append(("yes", q["bid_px"]))
            if "no" in q["sides"] and bb is not None and bb >= q["ask_px"] - 1e-9:
                filled.append(("no", 1.0 - q["ask_px"]))
            for side, px in filled:
                p = self.position.setdefault(slug, {"yes": 0.0, "no": 0.0, "yes_cost": 0.0,
                                                    "no_cost": 0.0, "event": q["prog"]["event"]})
                p[side] += q["size"]
                p[f"{side}_cost"] += q["size"] * px
                cents = int(round(px * 100))
                reb = float(pm_us_maker_rebate_usd(cents, q["size"]))
                self.rebate_usd += reb
                self.fills.append({"slug": slug, "side": side, "px": px, "size": q["size"],
                                   "ts": now, "rebate_usd": reb})
                _log.info("pmus paper fill %s %s %.0f@%.3f rebate %.2f", slug, side, q["size"], px, reb)
            if filled:
                sides = tuple(sd for sd in q["sides"] if sd not in [f[0] for f in filled]
                              and not self.side_blocked(slug, sd, q["prog"]["event"]))
                # re-join at the new touch for sides still allowed
                ev = evaluate(q["prog"], book, q["size"], sides=sides) if sides else None
                if ev is None:
                    self.quotes.pop(slug, None)
                else:
                    self.quotes[slug] = dict(ev, prog=q["prog"], sides=sides, ts=now, score_ts=now)
            else:
                ev = evaluate(q["prog"], book, q["size"], sides=q["sides"])
                if ev is not None:  # follow the touch (paper re-join; queue reset)
                    self.quotes[slug] = dict(ev, prog=q["prog"], sides=q["sides"], ts=q["ts"], score_ts=now)
            if self._stop.wait(0.4):
                return
        self.stats["polls"] += 1

    def markout_usd(self) -> float:
        out = 0.0
        for f in self.fills:
            book = self._books.get(f["slug"])
            if not book or not book["bids"] or not book["offers"]:
                continue
            mid = (book["bids"][0][0] + book["offers"][0][0]) / 2.0
            side_mid = mid if f["side"] == "yes" else 1.0 - mid
            out += f["size"] * (side_mid - f["px"])
        return out

    # -------------------------------------------------------------- thread
    def start(self) -> "PMUSPaperVenue":
        self._thread = threading.Thread(target=self._run, name="lip-pmus", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        refresh_s = max(120.0, _num("LIP_PMUS_REFRESH_S", 900.0))
        poll_s = max(10.0, _num("LIP_PMUS_POLL_S", 30.0))
        next_refresh = 0.0
        while not self._stop.is_set():
            try:
                if self.clock() >= next_refresh:
                    self.refresh_programs()
                    self.select()
                    next_refresh = self.clock() + refresh_s
                else:
                    self.poll_once()
            except PMUSOrderBlocked:
                self.stats["blocked_writes"] += 1
                _log.exception("pmus blocked request")
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = type(e).__name__
                _log.warning("pmus paper cycle failed: %s", type(e).__name__)
            self._stop.wait(poll_s)

    def summary(self) -> dict:
        el = max(1.0, self.clock() - self.started_ts)
        quotes = list(self.quotes.items())
        cap = sum(q["capital"] for _s, q in quotes)
        rate_day = sum(q["usd_per_s"] for _s, q in quotes) * 86400.0
        mk = self.markout_usd()
        return {
            "enabled": True, "paper": True, "order_endpoints": "hard-disabled (GET allowlist)",
            "programs_eligible": len(self.programs), "quoted_n": len(quotes),
            "capital_usd": round(cap, 2), "headroom_usd": round(self.headroom_usd(), 2),
            "est_reward_raw_usd": round(self.reward_usd, 4),
            "est_reward_rate_per_day_now": round(rate_day, 2),
            "fills_n": len(self.fills), "rebates_usd": round(self.rebate_usd, 4),
            "markout_usd": round(mk, 4),
            "unpaired_usd": round(sum(self._unpaired_usd(s) for s in self.position), 4),
            "net_usd": round(self.reward_usd + self.rebate_usd + mk, 4),
            "session_s": round(el, 0),
            "top": [{"slug": s, "period": q["prog"]["period"], "size": q["size"], "bid": q["bid_px"],
                     "ask": q["ask_px"], "share": round(q["share"], 4),
                     "usd_day": round(q["usd_per_s"] * 86400.0, 2), "paid": q["paid"],
                     "sides": list(q["sides"])}
                    for s, q in sorted(quotes, key=lambda kv: -kv[1]["usd_per_s"])[:8]],
            **{k: v for k, v in self.stats.items()},
        }
