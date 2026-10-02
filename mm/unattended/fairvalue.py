"""Patch 16: external fair value (defensive only), from free public sources.

Source: Polymarket Gamma API (public, no key) matched to Kalshi market titles
(Kalshi public market endpoint, unauthenticated, read-only) by conservative
fuzzy matching. A background thread refreshes every LIP_FV_REFRESH_S; the
engine only reads the cached dict (never network on the tick path).

The value is used only to *withhold* quotes: if |fv - kalshi_mid| exceeds
LIP_FV_DISAGREE_CENTS, the side that would be picked off is not rested.

Model fair value: for the families in LIP_FV_QUOTE_FAMILIES (default
KXHIGH) the cache also prices a model fair value
(mm/unattended/fv_weather.py, source ``open_meteo_ensemble_high``) on the
same background thread when FV quoting (LIP_FV_QUOTE_ENABLE, default off) or
calibration (LIP_FV_CALIB_ENABLE, default on with LIP_FV_ENABLE) is on.
Calibration only records it (RunLoop._fv_calib_tick). FV-driven quoting
(paper only) uses it to drive quoting for those markets instead of the
disagreement guard (``side_edge_cents`` / ``side_ev_usd_day`` below;
RunLoop._fv_quote_row). A model value is used by RunLoop only in paper mode
(never by the guard or markouts outside paper) and by the guard only when
FV quoting is on for its family. Markets in every other family keep the
defensive guard only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("lip.fairvalue")

GAMMA = "https://gamma-api.polymarket.com/markets"
KALSHI_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"

MONTHS = {"jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun",
          "june", "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october",
          "nov", "november", "dec", "december"}
STOP = {"will", "the", "a", "an", "be", "of", "in", "on", "by", "at", "to", "for", "or", "and",
        "is", "than", "this", "that", "et", "pm", "am", "before", "end", "its", "what", "who",
        "how", "much", "many", "price", "yes", "no", "s", "there", "any", "between"}
ALIAS = {"eth": "ethereum", "btc": "bitcoin", "sol": "solana", "doge": "dogecoin",
         "fed": "federal", "gop": "republican", "republicans": "republican",
         "democrats": "democrat", "democratic": "democrat"}
DOWN = {"below", "under", "less", "dip", "dips", "low", "lower", "min", "minimum", "fall", "falls",
        "drop", "drops", "fewer", "lowest"}
UP = {"above", "over", "more", "reach", "reaches", "hit", "high", "higher", "max", "maximum",
      "exceed", "exceeds", "greater", "highest", "least"}


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return _env("LIP_FV_ENABLE", 0.0) > 0


# ---------------------------------------------------------------- FV-driven quoting (paper)
# Favourite-longshot bias on Kalshi: "low-price contracts win far less often
# than required to break even" (Makers and Takers: The Economics of the Kalshi
# Prediction Market, MPRA paper 126350, 2025,
# https://ideas.repec.org/p/pra/mprapa/126350.html). LIP_FV_LONGSHOT_TILT
# shades YES bids priced below this many cents by that many cents of edge.
LONGSHOT_BELOW_CENTS = 15


def fv_quote_enabled() -> bool:
    """LIP_FV_QUOTE_ENABLE (default 0): model fair value drives paper quoting
    for the LIP_FV_QUOTE_FAMILIES series. Needs LIP_FV_ENABLE for the cache."""
    return _env("LIP_FV_QUOTE_ENABLE", 0.0) > 0


def fv_calib_enabled() -> bool:
    """LIP_FV_CALIB_ENABLE (default 1; needs LIP_FV_ENABLE): price the model
    families (LIP_FV_QUOTE_FAMILIES) and record calibration samples
    (mm/unattended/fv_calib.py) even when FV-driven quoting is off. Paper
    data collection only: it changes no quote."""
    return enabled() and _env("LIP_FV_CALIB_ENABLE", 1.0) > 0


def fv_model_active(series: str) -> bool:
    """The model prices ``series`` (cache targets, calibration watch, the
    screen's early feed in paper): FV quoting OR calibration is on and the
    series is in one of LIP_FV_QUOTE_FAMILIES. Quoting itself uses
    ``fv_quote_active``."""
    if not (fv_quote_enabled() or fv_calib_enabled()):
        return False
    s = str(series or "").upper()
    return any(s.startswith(p) for p in fv_quote_families())


def fv_quote_families() -> tuple:
    """LIP_FV_QUOTE_FAMILIES: comma list of series prefixes (default KXHIGH)."""
    raw = os.environ.get("LIP_FV_QUOTE_FAMILIES")
    raw = "KXHIGH" if raw is None else raw
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def fv_quote_active(series: str) -> bool:
    """FV-driven quoting is on and ``series`` is in one of its families."""
    if not fv_quote_enabled():
        return False
    s = str(series or "").upper()
    return any(s.startswith(p) for p in fv_quote_families())


def fv_model_supports(series: str) -> bool:
    """A model can price this series (fv_weather.STATIONS: verified daily-HIGH
    settlement stations only)."""
    from mm.unattended.fv_weather import station_for
    return station_for(series) is not None


def fv_min_conf() -> float:
    """LIP_FV_MIN_CONF (0.6): minimum confidence for a fair value to be used."""
    return _env("LIP_FV_MIN_CONF", 0.6)


def fv_max_giveup_cents() -> float:
    """LIP_FV_MAX_GIVEUP_CENTS (0): the most a resting side may pay above fair
    value (negative edge) for LIP rewards. 0 = never pay up versus FV."""
    return max(0.0, _env("LIP_FV_MAX_GIVEUP_CENTS", 0.0))


def fv_longshot_tilt_cents() -> float:
    """LIP_FV_LONGSHOT_TILT (0 = off): cents of edge removed from YES bids
    below LONGSHOT_BELOW_CENTS."""
    return max(0.0, _env("LIP_FV_LONGSHOT_TILT", 0.0))


def fv_min_close_hours():
    """LIP_FV_MIN_HOURS_TO_CLOSE: the selector's minimum hours to close for a
    market of an FV-quoted family WITH a usable fair value (and for the screen
    to feed such markets so a value can be computed). Unset = the global
    minimum applies to them too."""
    raw = os.environ.get("LIP_FV_MIN_HOURS_TO_CLOSE")
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def side_edge_cents(fv_cents: float, side: str, price_cents: float) -> float:
    """Expected cents per contract vs fair value for a resting bid at
    ``price_cents``: YES bid FV - b, NO bid (100 - FV) - n; YES bids under
    LONGSHOT_BELOW_CENTS lose LIP_FV_LONGSHOT_TILT more."""
    if side == "yes":
        edge = float(fv_cents) - float(price_cents)
        if float(price_cents) < LONGSHOT_BELOW_CENTS:
            edge -= fv_longshot_tilt_cents()
        return edge
    return (100.0 - float(fv_cents)) - float(price_cents)


def side_ev_usd_day(edge_cents: float, fills_per_day: float, reward_usd_day: float,
                    fee_usd_per_contract: float) -> float:
    """$/day of one resting side: edge x expected fills/day (the selector's
    fill model) + that side's expected LIP reward - maker fees on the fills."""
    return (float(fills_per_day) * float(edge_cents) / 100.0 + float(reward_usd_day)
            - float(fee_usd_per_contract) * float(fills_per_day))


def fv_side_ok(edge_cents: float) -> bool:
    """A side may rest only when it gives up at most LIP_FV_MAX_GIVEUP_CENTS."""
    return float(edge_cents) >= -fv_max_giveup_cents()


def _iso_ts(raw):
    """Epoch seconds of an ISO-8601 time (Kalshi close_time), None if absent/bad."""
    if not raw:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def tokens(text: str) -> tuple[set, set]:
    """(words, numbers). Years and day-of-month after a month name are dropped."""
    t = re.sub(r"\d{1,2}:\d{2}", " ", (text or "").lower().replace(",", ""))
    raw = re.findall(r"\d+(?:\.\d+)?k?|[a-z]+", t)
    words, nums = set(), set()
    prev = ""
    for tok in raw:
        if tok[0].isdigit():
            mult = 1000.0 if tok.endswith("k") else 1.0
            val = float(tok.rstrip("k")) * mult
            is_year = mult == 1.0 and 2020 <= val <= 2035 and "." not in tok
            is_day = prev in MONTHS and val <= 31
            if not (is_year or is_day):
                nums.add(f"{val:g}")
        elif tok not in STOP and len(tok) > 1:
            words.add(ALIAS.get(tok, tok))
        prev = tok
    return words, nums


def _polarity(words: set) -> int:
    d, u = bool(words & DOWN), bool(words & UP)
    return 0 if d == u else (-1 if d else 1)


def match_score(k_text: str, pm_text: str) -> float:
    """0..1. Zero unless numbers agree exactly and direction words agree."""
    kw, kn = tokens(k_text)
    pw, pn = tokens(pm_text)
    if kn != pn:
        return 0.0
    if _polarity(kw) != _polarity(pw):
        return 0.0
    kw2, pw2 = kw - DOWN - UP - MONTHS, pw - DOWN - UP - MONTHS
    if not kw2 or not pw2:
        return 0.0
    return len(kw2 & pw2) / len(kw2 | pw2)


def pm_yes_cents(m: dict, max_spread: float, min_liq: float) -> float | None:
    """Polymarket YES mid in cents, only for liquid, tight, binary Yes/No markets."""
    try:
        outcomes = m.get("outcomes")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        if [str(o).lower() for o in (outcomes or [])] != ["yes", "no"]:
            return None
        if float(m.get("liquidity") or m.get("liquidityNum") or 0) < min_liq:
            return None
        bid, ask = float(m.get("bestBid")), float(m.get("bestAsk"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not (0 < bid <= ask < 1) or ask - bid > max_spread:
        return None
    return round((bid + ask) / 2 * 100, 2)


CITY_LL = {"washington dc": (38.85, -77.04), "new york city": (40.78, -73.97), "new york": (40.78, -73.97),
           "nyc": (40.78, -73.97), "chicago": (41.79, -87.75), "los angeles": (33.94, -118.41),
           "miami": (25.79, -80.32), "houston": (29.64, -95.28), "dallas": (32.85, -96.85),
           "austin": (30.18, -97.68), "denver": (39.85, -104.66), "philadelphia": (39.87, -75.23),
           "seattle": (47.44, -122.31), "san francisco": (37.62, -122.37), "boston": (42.36, -71.01),
           "atlanta": (33.63, -84.44), "phoenix": (33.43, -112.01), "las vegas": (36.08, -115.15),
           "new orleans": (29.99, -90.25), "minneapolis": (44.88, -93.23), "detroit": (42.23, -83.33),
           "san antonio": (29.53, -98.47), "oklahoma city": (35.39, -97.60), "nashville": (36.12, -86.68)}
RAIN_RE = re.compile(r"rain in (.+?) between (\w+ \d+) and (\w+ \d+), (\d{4})", re.I)


def ensemble_rain_prob(hourly: dict, days: list) -> float | None:
    """Fraction of ensemble members with >=0.25 mm (0.01 in) on any of `days` (local)."""
    times = hourly.get("time") or []
    keys = [k for k in hourly if k.startswith("precipitation")]
    if not times or len(keys) < 10:
        return None
    wet = 0
    for k in keys:
        tot = {}
        for t, v in zip(times, hourly[k]):
            if t[:10] in days and v is not None:
                tot[t[:10]] = tot.get(t[:10], 0.0) + float(v)
        if len(tot) < len(days):
            return None
        wet += any(x >= 0.25 for x in tot.values())
    return wet / len(keys)


def best_match(k_text: str, pm_rows: list, index: dict, min_conf: float):
    kw, _ = tokens(k_text)
    cand = set()
    for w in kw:
        cand.update(index.get(w, ()))
    best, best_s, second = None, 0.0, 0.0
    for i in cand:
        s = match_score(k_text, pm_rows[i]["question"])
        if s > best_s:
            best, best_s, second = i, s, best_s
        elif s > second:
            second = s
    # Conservative: confident and unambiguous.
    if best is None or best_s < min_conf or (second >= min_conf and best_s - second < 0.1):
        return None, best_s
    return pm_rows[best], best_s


class FairValueCache:
    """Background refresher. `get(market)` is a dict read; safe from the tick path."""

    def __init__(self, session=None, *, sleep=time.sleep) -> None:
        import requests
        self.http = session or requests.Session()
        self.sleep = sleep
        self.values: dict[str, dict] = {}
        self.titles: dict[str, str] = {}
        # Strike metadata per market: ``hints`` set by RunLoop (from the
        # screen's GET /markets rows), ``market_meta`` from our own public fetch.
        self.hints: dict[str, dict] = {}
        self.market_meta: dict[str, dict] = {}
        self.wx = None  # fv_weather.WeatherHighModel, created on first use
        self._seen: dict[str, float] = {}   # market -> last refresh that targeted it
        self.stats = {"refreshes": 0, "pm_markets": 0, "targets": 0, "matched": 0,
                      "errors": 0, "last_refresh_ts": None, "last_error": None}
        self._thread = None

    # --- read side -------------------------------------------------------
    def get(self, market: str, now: float | None = None):
        row = self.values.get(market)
        if row is None:
            return None
        if (now or time.time()) - row["ts"] > _env("LIP_FV_MAX_AGE_S", 900):
            return None
        return row

    def note_market(self, market: str, meta: dict) -> None:
        """Strike fields for ``market`` (called from the engine thread; a
        single dict assignment, read by the refresh thread)."""
        self.hints[market] = dict(meta)

    def summary(self) -> dict:
        from mm.unattended.fv_weather import SOURCE as _WX
        vals = self.values
        return dict(self.stats, enabled=enabled(),
                    sources=["polymarket_gamma", "open_meteo_ensemble", _WX],
                    weather_high=(dict(self.wx.stats) if self.wx is not None else None),
                    matches=[{"market": k, "fv_cents": v["fv_cents"], "conf": round(v["conf"], 3),
                              "src": v.get("source", "polymarket"),
                              "pm": v["pm_question"][:90]} for k, v in sorted(vals.items())][:40])

    # --- refresh side ----------------------------------------------------
    def _kalshi_title(self, market: str) -> str | None:
        if market in self.titles:
            return self.titles[market]
        base = os.environ.get("LIP_FV_KALSHI_BASE", KALSHI_PUBLIC)
        r = self.http.get(f"{base}/markets/{market}", timeout=10)
        if r.status_code != 200:
            return None
        m = r.json().get("market") or {}
        title = " ".join(x for x in (m.get("title"), m.get("yes_sub_title")) if x)
        self.titles[market] = title
        self.market_meta[market] = {"strike_type": m.get("strike_type"), "floor_strike": m.get("floor_strike"),
                                    "cap_strike": m.get("cap_strike"), "title": m.get("title"),
                                    "close_ts": _iso_ts(m.get("close_time"))}
        self.sleep(0.2)
        return title

    def _rain_fv(self, market: str, now: float):
        from datetime import datetime
        title = self._kalshi_title(market) or ""
        m = RAIN_RE.search(title)
        if not m:
            return None
        ll = CITY_LL.get(m.group(1).strip().lower())
        if ll is None:
            return None
        yr = m.group(4)
        d1, d2 = (datetime.strptime(f"{x} {yr}", "%b %d %Y").date() for x in (m.group(2), m.group(3)))
        days = [d1.isoformat(), d2.isoformat()] if d2 > d1 else [d1.isoformat()]
        if (d1 - datetime.utcfromtimestamp(now).date()).days > 10:
            return None  # beyond useful ensemble skill
        r = self.http.get("https://ensemble-api.open-meteo.com/v1/ensemble",
                          params={"latitude": ll[0], "longitude": ll[1], "hourly": "precipitation",
                                  "models": "gfs_seamless", "timezone": "America/New_York",
                                  "start_date": days[0], "end_date": days[-1]}, timeout=15)
        if r.status_code != 200:
            return None
        p = ensemble_rain_prob(r.json().get("hourly") or {}, days)
        if p is None:
            return None
        fv = min(97.0, max(3.0, round(p * 100, 1)))  # raw ensemble: keep away from 0/100
        return {"fv_cents": fv, "conf": 1.0, "pm_question": f"open-meteo GFS ensemble rain {m.group(1)} {days}",
                "source": "open_meteo_ensemble", "thr": _env("LIP_FV_WEATHER_DISAGREE_CENTS", 20),
                "kalshi_title": title, "ts": now}

    def _pm_rows(self) -> list:
        rows = []
        pages = int(_env("LIP_FV_PM_PAGES", 40))
        offset = 0
        for _ in range(pages):
            r = self.http.get(GAMMA, params={"active": "true", "closed": "false", "limit": 500,
                                             "offset": offset, "order": "volume24hr",
                                             "ascending": "false"}, timeout=15)
            if r.status_code == 422 and rows:  # Gamma caps the offset (~2000)
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(m for m in batch if m.get("question"))
            offset += len(batch)
            self.sleep(0.2)
        return rows

    META_KEEP_AFTER_CLOSE_S = 86400.0
    META_KEEP_UNTARGETED_S = 7 * 86400.0

    def _close_ts(self, market: str):
        """Close of ``market`` if known: the public market fetch's close_time,
        a hint's close_ts, or a daily-HIGH market's settlement window end."""
        for src in (self.market_meta.get(market), self.hints.get(market)):
            if isinstance(src, dict) and src.get("close_ts") is not None:
                try:
                    return float(src["close_ts"])
                except (TypeError, ValueError):
                    pass
        from mm.unattended import fv_weather as W
        st, d = W.station_for(market.split("-", 1)[0]), W.measurement_date(market)
        if st is not None and d is not None:
            return W.settlement_window(st, d)[1]
        return None

    def _prune_meta(self, targets, now: float) -> int:
        """Forget titles / hints / public market metadata of markets not asked
        for now that closed more than a day ago, or (close unknown) were last
        asked for more than a week ago. Runs on the refresh thread; the engine
        thread only assigns single hint entries (note_market)."""
        tset = set(targets)
        for m in tset:
            self._seen[m] = now
        known = set(list(self.titles)) | set(list(self.hints)) | set(list(self.market_meta)) | set(self._seen)
        gone = []
        for m in known - tset:
            close = self._close_ts(m)
            if close is not None:
                stale = now > close + self.META_KEEP_AFTER_CLOSE_S
            else:
                stale = now - self._seen.get(m, now) > self.META_KEEP_UNTARGETED_S
            if stale:
                gone.append(m)
        for m in gone:
            for d in (self.titles, self.hints, self.market_meta, self._seen):
                d.pop(m, None)
        return len(gone)

    def refresh(self, targets) -> None:
        min_conf = _env("LIP_FV_MIN_CONF", 0.6)
        max_spread = _env("LIP_FV_MAX_PM_SPREAD", 0.06)
        min_liq = _env("LIP_FV_MIN_PM_LIQ", 1000)
        targets = sorted(set(targets))
        self._prune_meta(targets, time.time())
        pm_error = None
        try:
            pm = self._pm_rows()
        except Exception as exc:
            # Keep pricing the model families; earlier Polymarket rows keep
            # their own ts and age out in get() (LIP_FV_MAX_AGE_S).
            pm, pm_error = [], exc
        index: dict[str, list] = {}
        for i, m in enumerate(pm):
            for w in tokens(m["question"])[0]:
                index.setdefault(w, []).append(i)
        now = time.time()
        out = {}
        for market in targets:
            try:
                title = self._kalshi_title(market)
            except Exception:
                title = None
            if not title:
                continue
            row, conf = best_match(title, pm, index, min_conf)
            if row is None:
                continue
            fv = pm_yes_cents(row, max_spread, min_liq)
            if fv is None:
                continue
            out[market] = {"fv_cents": fv, "conf": conf, "pm_question": row["question"], "source": "polymarket",
                           "pm_slug": row.get("slug"), "kalshi_title": title, "ts": now}
        if pm_error is not None:
            out.update({k: v for k, v in self.values.items() if v.get("source") == "polymarket" and k in targets})
        out.update(self._high_rows([m for m in targets if fv_model_active(m.split("-", 1)[0])], now))
        if _env("LIP_FV_WEATHER", 1.0) > 0:
            for market in targets:
                if market.startswith("KXRAINWKND") and market not in out:
                    try:
                        row = self._rain_fv(market, now)
                    except Exception:
                        row = None
                    if row:
                        out[market] = row
        self.values = out
        self.stats.update(refreshes=self.stats["refreshes"] + 1, pm_markets=len(pm),
                          targets=len(targets), matched=len(out), last_refresh_ts=now,
                          matched_by_source={src: sum(1 for v in out.values() if v.get("source") == src)
                                             for src in ("polymarket", "open_meteo_ensemble",
                                                         "open_meteo_ensemble_high")})
        log.info("fair value refresh: %d targets, %d pm markets, %d matched",
                 len(targets), len(pm), len(out))
        for k, v in out.items():
            log.info("fv match %s fv=%.1fc conf=%.2f pm=%r", k, v["fv_cents"], v["conf"],
                     v["pm_question"][:80])
        if pm_error is not None:
            raise pm_error  # counted in stats["errors"] by the refresh thread

    def _high_rows(self, markets: list, now: float) -> dict:
        """Model fair value for daily-HIGH markets of the model families
        (fv_weather; FV quoting or calibration on). No LIP_FV_WX_PARAMS_FILE:
        the unfitted defaults; a set but unreadable file prices nothing."""
        from mm.unattended import fv_weather as W
        if not markets:
            return {}
        if self.wx is None:
            self.wx = W.WeatherHighModel(self.http)
        try:
            params = W.load_params(os.environ.get("LIP_FV_WX_PARAMS_FILE"))
        except Exception as exc:
            self.wx.stats["last_error"] = f"params {type(exc).__name__}: {str(exc)[:120]}"
            log.warning("weather fair value disabled: LIP_FV_WX_PARAMS_FILE unusable (%s)", type(exc).__name__)
            return {}
        out = {}
        for market in markets:
            if W.station_for(market.split("-", 1)[0]) is None:
                self.wx.stats["unsupported"] += 1
                continue
            try:
                meta = dict(self.hints.get(market) or {})
                if meta.get("strike_type") is None:
                    if market not in self.market_meta:
                        self._kalshi_title(market)
                    meta = dict(self.market_meta.get(market) or {})
                elif market in self.market_meta:
                    meta.setdefault("title", self.market_meta[market].get("title"))
                row = self.wx.fv_for(market, meta, now=now, params=params)
            except Exception as exc:
                self.wx.stats["errors"] += 1
                self.wx.stats["last_error"] = type(exc).__name__
                row = None
            if row is not None:
                row["thr"] = _env("LIP_FV_WEATHER_DISAGREE_CENTS", 20)
                out[market] = row
        return out

    def stop(self) -> None:
        ev = getattr(self, "_stop_ev", None)
        if ev is not None:
            ev.set()

    def start(self, targets_fn) -> None:
        self._stop_ev = threading.Event()

        def run():
            while not self._stop_ev.is_set():
                try:
                    targets = targets_fn()
                    if not targets:
                        self._stop_ev.wait(15)
                        continue
                    self.refresh(targets)
                except Exception as exc:  # never kill the engine
                    self.stats["errors"] += 1
                    self.stats["last_error"] = type(exc).__name__
                    log.warning("fair value refresh failed: %s", type(exc).__name__)
                self._stop_ev.wait(max(60.0, _env("LIP_FV_REFRESH_S", 300)))
        self._thread = threading.Thread(target=run, name="lip-fairvalue", daemon=True)
        self._thread.start()


def fv_drop_sides(fv_cents: float, yes_bid, no_bid, thr: float, both: bool) -> tuple:
    """Sides to withhold. fv above Kalshi mid => our NO bid is the stale side."""
    if yes_bid is None or no_bid is None:
        return ()
    mid = (yes_bid + (100 - no_bid)) / 2.0
    diff = fv_cents - mid
    if abs(diff) <= thr:
        return ()
    if both:
        return ("yes", "no")
    return ("no",) if diff > 0 else ("yes",)
