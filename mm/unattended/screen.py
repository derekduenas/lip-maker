"""Candidate screen for the read-only production book path.

Joins incentive programs with market metadata (GET /markets?tickers=...,
batched) and series category (GET /series/{ticker}), both cached on disk.
Applies the selector's exclusion policy up front so the RunLoop and the
websocket only carry the top ``LIP_CANDIDATE_TOP`` candidates.

Read-only. Every call goes through ``ReadOnlyKalshiTransport.get``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

_log = logging.getLogger("lip.screen")

DEFAULT_CACHE_PATH = "/var/lib/lip-maker/exchange_index_cache.json"
BATCH = 100
PAUSE_S = 0.12
META_TTL_S = 6 * 3600.0
SERIES_TTL_S = 7 * 86400.0
CACHE_VERSION = 2
DEFAULT_NEWS_CATEGORIES = "Politics,Elections,World,Entertainment,Sports,Esports,Social,Mentions,Culture"


def news_categories() -> set[str]:
    raw = os.environ.get("LIP_NEWS_CATEGORIES") or DEFAULT_NEWS_CATEGORIES
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


# Units of ``rank_penalty_per_day``: $/day for this many contracts per side,
# both sides quoted. RunLoop scales it by size / RANK_PENALTY_UNIT.
RANK_PENALTY_UNIT = 100.0


def rank_score(frame: dict, meta: dict, *, category: str | None, days: float | None,
               size: float = 100.0) -> dict:
    """Expected net $/day per $ of capital at our size, from market metadata only.

    reward  = pool/day x share, share = 2S / (2S + touch depth on both sides),
              S = min(target, ``size``)
    markout = per contract per day: 2 sides x fill fraction/day x adverse
              cents (the family MARKOUT_PRIOR_CENTS), with the fill fraction
              scaled by 24 h volume, and the whole charge scaled up as
              days-to-close shrinks (1 + LIP_RANK_SHORT_K / days) and for
              news-driven categories (x LIP_RANK_NEWS_MULT).
    score   = (reward - S x markout) / capital, capital = S x (yes bid + no bid).

    Returned ``penalty`` (stored as the frame's ``rank_penalty_per_day``) is
    NOT the full markout: selector.quote_economics already subtracts the
    base markout prior (2 x fill fraction x |prior|) from ``net``, and RunLoop
    subtracts this penalty from that net. So ``penalty`` is only the
    increment the volume / time / news multipliers add on top of the base
    prior, in $/day per RANK_PENALTY_UNIT (100) contracts per side. net -
    penalty then charges the markout prior exactly once.
    ``penalty_full`` is the full charge at S used in ``score``.
    """
    from mm.fair_value import family_for_series
    from mm.selector import FILL_FRACTION_PER_DAY, MARKOUT_PRIOR_CENTS
    S = float(min(float(frame.get("target_size") or size), size)) or size
    yb = meta.get("yes_bid")
    ya = meta.get("yes_ask")
    yb = 0.0 if yb is None else float(yb)
    no_bid = 0.0 if ya is None else max(0.0, 1.0 - float(ya))
    capital = S * max(0.05, yb + no_bid)
    depth = float(meta.get("yes_bid_size") or 0.0) + float(meta.get("yes_ask_size") or 0.0)
    share = (2.0 * S) / (2.0 * S + depth)
    reward = pool_per_day(frame) * share
    family = family_for_series(str(frame.get("series") or ""))
    base_fill = FILL_FRACTION_PER_DAY.get(family, FILL_FRACTION_PER_DAY["event"])
    vol = float(meta.get("volume_24h") or 0.0)
    vol_mult = min(3.0, 1.0 + vol / 5000.0)
    adverse = abs(MARKOUT_PRIOR_CENTS.get(family, MARKOUT_PRIOR_CENTS["event"]))
    mult = 1.0 + _env_float("LIP_RANK_SHORT_K", 3.0) / max(0.5, float(days if days is not None else 0.5))
    news = bool(category and category.strip().lower() in news_categories())
    if news:
        mult *= _env_float("LIP_RANK_NEWS_MULT", 2.0)
    base_per_contract = 2.0 * base_fill * adverse / 100.0          # already in quote_economics net
    full_per_contract = base_per_contract * vol_mult * mult
    penalty_full = S * full_per_contract
    incremental = RANK_PENALTY_UNIT * max(0.0, full_per_contract - base_per_contract)
    return {"score": (reward - penalty_full) / capital, "reward": reward, "penalty": incremental,
            "penalty_full": penalty_full, "penalty_unit_contracts": int(RANK_PENALTY_UNIT),
            "capital": capital, "share": share, "news": news}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def candidate_top() -> int:
    return int(_env_float("LIP_CANDIDATE_TOP", 300))


def _ts(raw) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(raw) -> float | None:
    try:
        return None if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        return None


def market_meta(row: dict, now: float | None = None) -> dict:
    """Fields kept from one GET /markets row. Effective close = min(close, occurrence)."""
    close = _ts(row.get("close_time"))
    occ = _ts(row.get("occurrence_datetime"))
    eff = min([x for x in (close, occ) if x is not None], default=None)
    ei = row.get("exchange_index")
    return {
        "exchange_index": None if ei is None else int(ei),
        "close_ts": close,
        "occurrence_ts": occ,
        "effective_close_ts": eff,
        "event_ticker": row.get("event_ticker"),
        "status": row.get("status"),
        "volume_24h": _num(row.get("volume_24h_fp")),
        "yes_bid": _num(row.get("yes_bid_dollars")),
        "yes_ask": _num(row.get("yes_ask_dollars")),
        "yes_bid_size": _num(row.get("yes_bid_size_fp")),
        "yes_ask_size": _num(row.get("yes_ask_size_fp")),
        "fetched": time.time() if now is None else float(now),
    }


class MetaCache:
    """Per-ticker market metadata and per-series category, persisted as JSON."""

    def __init__(self, path: str | None = None, *, sleep=time.sleep, clock=time.time) -> None:
        self.path = Path(path or os.environ.get("LIP_META_CACHE") or DEFAULT_CACHE_PATH)
        self.sleep = sleep
        self.clock = clock
        self.markets: dict[str, dict] = {}
        self.series: dict[str, dict] = {}
        self.lookups = 0
        self.series_lookups = 0
        self.failures = 0
        self.dirty = False

    # -- persistence
    def load(self) -> "MetaCache":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("version") or 0) >= CACHE_VERSION:
                self.markets = dict(data.get("markets") or {})
            self.series = dict(data.get("series") or {})
        except FileNotFoundError:
            pass
        except Exception:
            _log.warning("meta cache unreadable at %s; starting empty", self.path)
        return self

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "saved": self.clock(),
                                       "markets": self.markets, "series": self.series}),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception:
            _log.exception("meta cache save failed")

    # -- lookups
    def stale_markets(self, tickers) -> list[str]:
        now = self.clock()
        out = []
        for t in tickers:
            row = self.markets.get(t)
            if row is None or now - float(row.get("fetched") or 0) > META_TTL_S:
                out.append(t)
        return out

    def fetch_markets(self, reader, tickers) -> int:
        from mm.venues.readonly import ReadOnlyHTTPError
        todo = list(dict.fromkeys(tickers))
        got = 0
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            try:
                payload = reader.get("/markets", params={"tickers": ",".join(chunk), "limit": 1000})
            except ReadOnlyHTTPError as exc:
                self.failures += 1
                _log.warning("market batch lookup HTTP %s (%d tickers)", exc.status, len(chunk))
                self.sleep(2.0 if exc.status == 429 else PAUSE_S)
                continue
            self.lookups += 1
            for row in payload.get("markets") or []:
                t = row.get("ticker")
                if t:
                    self.markets[t] = market_meta(row, self.clock())
                    got += 1
                    self.dirty = True
            self.sleep(PAUSE_S)
        return got

    def stale_series(self, names) -> list[str]:
        now = self.clock()
        return [s for s in dict.fromkeys(names)
                if s and (s not in self.series
                          or "fee_type" not in self.series[s]
                          or now - float(self.series[s].get("fetched") or 0) > SERIES_TTL_S)]

    def fetch_series(self, reader, names) -> int:
        from mm.venues.readonly import ReadOnlyHTTPError, path_has_traversal
        got = 0
        for s in names:
            path = f"/series/{quote(s, safe='')}"
            if path_has_traversal(path):
                # the read-only transport would refuse (and exit); skip the name
                self.failures += 1
                _log.warning("series name not fetchable read-only: %r", s[:40])
                continue
            try:
                payload = reader.get(path)
            except ReadOnlyHTTPError as exc:
                self.failures += 1
                if exc.status == 404:
                    self.series[s] = {"category": None, "tags": [], "fetched": self.clock()}
                    self.dirty = True
                self.sleep(2.0 if exc.status == 429 else PAUSE_S)
                continue
            self.series_lookups += 1
            ser = payload.get("series") or {}
            # Patch 21: fee_type / fee_multiplier (GET /series/{t}: quadratic |
            # quadratic_with_maker_fees | quadratic_with_combo_maker_fees | flat,
            # docs.kalshi.com get-series) so maker fees enter net $/day. Unknown,
            # missing and "flat" are priced as maker-fee series (screen()).
            self.series[s] = {"category": ser.get("category"), "tags": ser.get("tags") or [],
                              "frequency": ser.get("frequency"), "fee_type": ser.get("fee_type"),
                              "fee_multiplier": ser.get("fee_multiplier"), "fetched": self.clock()}
            self.dirty = True
            got += 1
            self.sleep(PAUSE_S)
        return got


# Kalshi fee types this code can price (mm/accounting.maker_coefficient).
KNOWN_FEE_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
CONSERVATIVE_FEE_TYPE = "quadratic_with_maker_fees"


def conservative_fee_type(raw) -> str:
    """Known Kalshi fee_type as-is; missing, unknown or "flat" (the "Specific
    Trading Fees Table", formula not confirmed here) -> the standard maker-fee
    type, so a series we cannot price never ranks as maker-fee-free."""
    return raw if raw in KNOWN_FEE_TYPES else CONSERVATIVE_FEE_TYPE


def pool_per_day(frame: dict) -> float:
    secs = float(frame.get("period_seconds") or 86400) or 86400.0
    return float(frame.get("period_reward_usd") or 0.0) / (secs / 86400.0)


def screen(frames: list[dict], cache: MetaCache, *, now: float | None = None,
           top: int | None = None) -> tuple[list[dict], dict]:
    """Return (candidates, stats). Candidates carry market close, shard, category.

    Programs whose market metadata or series category is not cached yet are
    reported as ``pending_meta`` / ``pending_category`` and left out.
    """
    from mm.selector import KalshiMarket, exclusion_reason
    now = time.time() if now is None else float(now)
    top = candidate_top() if top is None else int(top)
    reasons: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    ok: list[dict] = []
    seen: set = set()

    def _bump(why: str, series: str) -> None:
        base = why.split(":", 1)[0]
        import re
        base = re.sub(r"_[0-9.]+d$", "", base)
        reasons[base] = reasons.get(base, 0) + 1
        bucket = samples.setdefault(base, [])
        if series not in bucket and len(bucket) < 8:
            bucket.append(series)

    # Patch 21: overlapping programs on one market -> keep the richest one,
    # deterministically (input order used to decide, which flapped re-feeds).
    frames = sorted(frames, key=lambda f: (-pool_per_day(f), str(f.get("program_id") or "")))
    for frame in frames:
        market = frame.get("market")
        if not market or market in seen:
            continue
        seen.add(market)
        series = str(frame.get("series") or market.split("-", 1)[0])
        meta = cache.markets.get(market)
        if meta is None:
            _bump("pending_meta", series)
            continue
        if meta.get("status") not in (None, "active", "open"):
            _bump("market_not_active", series)
            continue
        eff = meta.get("effective_close_ts")
        days = None if eff is None else max(0.0, (float(eff) - now) / 86400.0)
        cat_row = cache.series.get(series)
        category = None if cat_row is None else cat_row.get("category")
        probe = KalshiMarket(
            market=market, series=series,
            period_reward_usd=float(frame.get("period_reward_usd") or 0),
            period_seconds=float(frame.get("period_seconds") or 86400),
            seconds_left=max(0.0, float(frame.get("end_ts") or now) - now),
            discount_factor=float(frame.get("discount_factor") or 0.5),
            target_size=float(frame.get("target_size") or 100),
            days_to_settle=days, exchange_index=meta.get("exchange_index"),
            category=category,
        )
        why = exclusion_reason(probe)
        if why:
            _bump(why, series)
            continue
        if cat_row is None:
            _bump("pending_category", series)
            continue
        out = dict(frame)
        out["exchange_index"] = meta.get("exchange_index")
        out["close_ts"] = eff
        out["days_to_settle"] = days
        out["category"] = category
        raw_fee = (cat_row or {}).get("fee_type")
        out["fee_type"] = conservative_fee_type(raw_fee)
        out["fee_type_raw"] = raw_fee
        out["fee_multiplier"] = (cat_row or {}).get("fee_multiplier")
        out["days_from_close"] = True
        rk = rank_score(frame, meta, category=category, days=days)
        out["rank_score"] = round(rk["score"], 6)
        out["rank_penalty_per_day"] = round(rk["penalty"], 6)
        out["occurrence_ts"] = meta.get("occurrence_ts")
        out["event_ticker"] = meta.get("event_ticker")
        ok.append(out)
    ok.sort(key=lambda f: (-f["rank_score"], -pool_per_day(f)))
    chosen = ok[:top]
    if len(ok) > top:
        reasons["below_candidate_top"] = len(ok) - top
    stats = {
        "programs_total": len(seen),
        "eligible": len(ok),
        "candidates": len(chosen),
        "candidate_top": top,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "samples": samples,
        "meta_cached": len(cache.markets),
        "series_cached": len(cache.series),
        "market_batches": cache.lookups,
        "series_lookups": cache.series_lookups,
        "lookup_failures": cache.failures,
        "ts": now,
    }
    return chosen, stats


def needs_series(frames: list[dict], cache: MetaCache, *, now: float | None = None) -> list[str]:
    """Series still missing a category among programs that pass every non-category check."""
    from mm.selector import KalshiMarket, exclusion_reason
    now = time.time() if now is None else float(now)
    want = []
    for frame in frames:
        market = frame.get("market")
        meta = cache.markets.get(market or "")
        if not market or meta is None:
            continue
        series = str(frame.get("series") or market.split("-", 1)[0])
        if series in cache.series:
            continue
        eff = meta.get("effective_close_ts")
        days = None if eff is None else max(0.0, (float(eff) - now) / 86400.0)
        probe = KalshiMarket(
            market=market, series=series, period_reward_usd=1.0, period_seconds=86400,
            seconds_left=86400, discount_factor=0.5, target_size=100,
            days_to_settle=days, exchange_index=meta.get("exchange_index"), category=None,
        )
        if not exclusion_reason(probe):
            want.append(series)
    return list(dict.fromkeys(want))
