"""FV-driven paper quoting for KXHIGH: knobs, edge/EV gates, selection
ranking, close-time admission, fail-closed paths, screen metadata and the
fair-value cache wiring."""
import json
import time
from dataclasses import replace

import pytest

from mm import selector as S
from mm.unattended import fairvalue as F
from mm.unattended import fv_weather as W
from mm.unattended import loop as L
from mm.unattended import screen as SC
from tests.test_patch15 import T0, _book

MKT = "KXHIGHNY-26OCT01-B72.5"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LIP_FV_QUOTE_ENABLE", "LIP_FV_QUOTE_FAMILIES", "LIP_FV_MIN_CONF", "LIP_FV_MAX_GIVEUP_CENTS",
              "LIP_FV_LONGSHOT_TILT", "LIP_FV_MIN_HOURS_TO_CLOSE", "LIP_MIN_HOURS_TO_CLOSE",
              "LIP_MIN_CLOSE_HOURS", "LIP_EVENT_WINDOW_HOURS", "LIP_SIZE_LADDER", "LIP_CROSS_GUARD",
              "LIP_DURABLE_RESERVE", "LIP_FV_PULL_BOTH", "LIP_FV_WX_PARAMS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIP_FV_ENABLE", "1")


def _on(monkeypatch, **extra):
    monkeypatch.setenv("LIP_FV_QUOTE_ENABLE", "1")
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    monkeypatch.setenv("LIP_FV_MIN_HOURS_TO_CLOSE", "24")
    for k, v in extra.items():
        monkeypatch.setenv(k, str(v))


class _FV:
    """Fair-value cache stub (the loop only reads ``get``/``summary``)."""

    def __init__(self, fv=50.0, conf=0.9, source=W.SOURCE):
        self.fv, self.conf, self.source = fv, conf, source
        self.hints = {}

    def get(self, market, now=None):
        if self.fv is None:
            return None
        return {"fv_cents": self.fv, "conf": self.conf, "source": self.source, "pm_question": "model",
                "thr": 20.0, "ts": time.time()}

    def note_market(self, market, meta):
        self.hints[market] = dict(meta)

    def summary(self):
        return {"enabled": True}


def _prog(loop, market=MKT, close_h=30.0, pool=100.0):
    loop.add_program({"market": market, "series": market.split("-")[0], "period_reward_usd": pool,
                      "period_seconds": 86400, "start_ts": T0 - 3600, "end_ts": T0 + 86400,
                      "close_ts": T0 + close_h * 3600, "target_size": 1000, "days_from_close": True,
                      "rank_score": 0.1, "exchange_index": 0, "strike_type": "between",
                      "floor_strike": 72, "cap_strike": 73})


def _loop(monkeypatch, fv=50.0, conf=0.9, mode="paper", close_h=30.0, yes=((30, 2000),), no=((60, 2000),)):
    loop = L.RunLoop(mode=mode, bankroll=5000, poster=None)
    loop.fv = _FV(fv, conf)
    _prog(loop, close_h=close_h)
    _book(loop, MKT, list(yes), list(no), T0)
    loop.now = T0
    loop.fv_quote_stats.clear()   # the first book frame already ran a selection
    return loop


# ---------------------------------------------------------------- knobs / pure math
def test_flags_default_off_and_family_match(monkeypatch):
    assert not F.fv_quote_enabled() and not F.fv_quote_active("KXHIGHNY")
    assert F.fv_quote_families() == ("KXHIGH",)
    monkeypatch.setenv("LIP_FV_QUOTE_ENABLE", "1")
    assert F.fv_quote_active("kxhighchi") and not F.fv_quote_active("KXRAINNYC")
    monkeypatch.setenv("LIP_FV_QUOTE_FAMILIES", "KXHIGHNY, KXHIGHCHI")
    assert F.fv_quote_active("KXHIGHCHI") and not F.fv_quote_active("KXHIGHMIA")
    assert F.fv_min_close_hours() is None
    monkeypatch.setenv("LIP_FV_MIN_HOURS_TO_CLOSE", "bad")
    assert F.fv_min_close_hours() is None


def test_edge_ev_giveup_and_longshot_tilt(monkeypatch):
    assert F.side_edge_cents(50, "yes", 45) == 5 and F.side_edge_cents(50, "no", 45) == 5
    assert F.side_edge_cents(30, "no", 75) == -5
    assert F.fv_side_ok(0) and not F.fv_side_ok(-0.5)
    monkeypatch.setenv("LIP_FV_MAX_GIVEUP_CENTS", "1")
    assert F.fv_side_ok(-1) and not F.fv_side_ok(-1.5)
    assert F.side_edge_cents(12, "yes", 10) == 2
    monkeypatch.setenv("LIP_FV_LONGSHOT_TILT", "3")
    assert F.side_edge_cents(12, "yes", 10) == -1          # YES bid under 15c shaded
    assert F.side_edge_cents(40, "yes", 20) == 20          # 20c bid: no tilt
    assert F.side_edge_cents(88, "no", 10) == 2            # NO side never tilted
    # EV = fills x edge + reward - fee x fills
    assert F.side_ev_usd_day(2.0, 4.0, 0.5, 0.01) == pytest.approx(0.08 + 0.5 - 0.04)


def _km(fv=None, yes=((30, 2000.0),), no=((60, 2000.0),), days=1.25, **kw):
    return S.KalshiMarket(market=MKT, series="KXHIGHNY", period_reward_usd=100.0, period_seconds=86400,
                          seconds_left=86400, discount_factor=0.5, target_size=1000, yes_bids=list(yes),
                          no_bids=list(no), days_to_settle=days, fv_cents=fv, **kw)


def test_quote_economics_adds_capture_vs_fv():
    base = S.quote_economics(_km(), 100)
    # rungs 30 / 60; weather fills 0.04/day x 100 = 4 per side.
    both = S.quote_economics(_km(fv=40.0), 100)           # edges +10 and 0: both rest
    assert both[0] - base[0] == pytest.approx(4 * (10 + 0) / 100.0)
    assert both[1] == base[1]                               # two-sided capital
    # with both sides resting the capture is the spread, whatever the FV
    assert S.quote_economics(_km(fv=35.0), 100)[0] == pytest.approx(both[0])
    # FV 50: NO at 60 pays 10c over fair -> priced as a one-sided YES quote
    one = S.quote_economics(_km(fv=50.0), 100)
    assert one[1] == pytest.approx(0.30 * 100)
    explicit = S.quote_economics(_km(), 100, sides=("yes",))
    assert one[0] == pytest.approx(explicit[0] + 4 * 20 / 100.0)
    assert S.fv_capture_per_day(_km(fv=50.0), 100, 30, 60, sides=("no",)) == pytest.approx(-0.4)
    assert S.fv_capture_per_day(_km(), 100, 30, 60) == 0.0
    # both sides pay up: nothing to price
    assert S.quote_economics(_km(fv=29.5, no=((71, 2000.0),)), 100)[:2] == (0.0, 0.0)


def test_one_sided_pricing_matches_legacy_size_curve_conversion():
    km = _km()
    net2, _cap2, share2, yc, nc = S.quote_economics(km, 300)
    cost2 = S.reward_per_day(share2, km) - net2
    legacy = S.reward_per_day(S.kalshi_one_sided_share(km, "no", nc, 300.0), km) - cost2 / 2.0
    net1, cap1, _s, _y, _n = S.quote_economics(km, 300, sides=("no",))
    assert net1 == pytest.approx(legacy) and cap1 == pytest.approx(nc / 100.0 * 300)


def test_ranking_uses_model_edge():
    plain = replace(_km(), market="KXHIGHNY-26OCT01-B70.5")
    edge = replace(_km(fv=40.0), market="KXHIGHNY-26OCT01-B72.5")
    payup = replace(_km(fv=29.5, no=((71, 2000.0),)), market="KXHIGHNY-26OCT01-B74.5")
    sel = S.fast_allocate([plain, edge, payup], per_market_usd=1000, chunk=100)
    nets = {t.market: t.net_per_day for t in sel.taken}
    assert nets["KXHIGHNY-26OCT01-B72.5"] > nets["KXHIGHNY-26OCT01-B70.5"]
    assert "KXHIGHNY-26OCT01-B74.5" not in nets            # both sides pay up vs FV


def test_close_time_admission_needs_fv_and_knob(monkeypatch):
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    assert S.exclusion_reason(_km(fv=50.0)) == "closes_within_48h"          # knob unset: no relaxation
    monkeypatch.setenv("LIP_FV_MIN_HOURS_TO_CLOSE", "24")
    assert S.exclusion_reason(_km()) == "closes_within_48h"                 # no fair value: excluded
    assert S.exclusion_reason(_km(fv=50.0)) == ""
    assert S.exclusion_reason(_km(fv_candidate=True)) == ""
    assert S.exclusion_reason(_km(fv=50.0, days=0.5)) == "closes_within_24h"


# ---------------------------------------------------------------- loop: quoting decisions
def test_flag_off_keeps_defensive_guard(monkeypatch):
    loop = _loop(monkeypatch, fv=50.0)
    assert loop._fv_quote_row(MKT) is None
    assert loop._quote(MKT, 30, 60, 100, T0)
    q = loop.resting[MKT]
    # FV 50 vs mid 35 is 15c (< the row's 20c weather threshold): both sides rest as before
    assert q["yes"] > 0 and q["no"] > 0


def test_negative_edge_side_withheld(monkeypatch):
    _on(monkeypatch)
    loop = _loop(monkeypatch, fv=50.0)
    assert loop._quote(MKT, 30, 60, 100, T0)
    q = loop.resting[MKT]
    assert q["yes"] > 0 and q["no"] == 0          # NO at 60 vs fair 50: -10c
    assert loop.fv_quote_stats.get("edge_withheld_no") == 1
    assert loop.fv_blocks == {}                    # the guard did not run


def test_giveup_allows_small_negative_edge(monkeypatch):
    _on(monkeypatch, LIP_FV_MAX_GIVEUP_CENTS=10)
    loop = _loop(monkeypatch, fv=40.5)
    assert loop._quote(MKT, 30, 60, 100, T0)
    q = loop.resting[MKT]
    assert q["yes"] > 0 and q["no"] > 0            # NO edge = 59.5 - 60 = -0.5c, allowed


def test_ev_gate_requires_positive_ev(monkeypatch):
    _on(monkeypatch, LIP_FV_MAX_GIVEUP_CENTS=10)
    # no LIP pool: EV = fills x edge - fees; NO edge -0.5c -> EV < 0 -> withheld
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _FV(40.5)
    _prog(loop, pool=0.0)
    _book(loop, MKT, [(30, 2000)], [(60, 2000)], T0)
    loop.now = T0
    assert loop._quote(MKT, 30, 60, 100, T0)
    q = loop.resting[MKT]
    assert q["yes"] > 0 and q["no"] == 0
    assert loop.fv_quote_stats.get("ev_withheld_no") == 1
    # both sides negative: nothing rests
    loop.fv = _FV(99.0)
    assert not loop._quote(MKT, 99, 60, 100, T0) or MKT not in loop.resting


def test_low_confidence_or_other_source_or_demo_falls_back(monkeypatch):
    _on(monkeypatch)
    assert _loop(monkeypatch, fv=50.0, conf=0.5)._fv_quote_row(MKT) is None
    loop = _loop(monkeypatch, fv=50.0)
    loop.fv = _FV(50.0, source="polymarket")
    assert loop._fv_quote_row(MKT) is None
    demo = L.RunLoop(mode="paper", bankroll=5000)
    demo.mode = "demo"  # FV quoting is paper only
    demo.fv = _FV(50.0)
    _prog(demo)
    assert demo._fv_quote_row(MKT) is None


def test_program_registers_fv_target_and_strike_hint(monkeypatch):
    _on(monkeypatch)
    loop = _loop(monkeypatch)
    assert MKT in loop._fv_wanted
    assert loop.fv.hints[MKT] == {"strike_type": "between", "floor_strike": 72, "cap_strike": 73}
    loop.end_program(MKT)
    assert MKT not in loop._fv_wanted and MKT not in loop._fv_hints
    _prog(loop, market="KXHIGHLAX-26OCT01-B72.5")       # unsupported station: not a target
    assert "KXHIGHLAX-26OCT01-B72.5" not in loop._fv_wanted


# ---------------------------------------------------------------- loop: selection
def test_select_admits_short_dated_kxhigh_only_with_fair_value(monkeypatch):
    _on(monkeypatch)
    loop = _loop(monkeypatch, fv=50.0, close_h=30.0)
    loop._select(T0)
    q = loop.resting.get(MKT)
    assert q is not None and q["yes"] > 0 and q["no"] == 0
    assert MKT in loop._fv_admitted
    plan = loop.last_plan[MKT]
    # ranked as the one-sided YES quote it is, with its capture vs FV
    assert plan["capital_usd"] == pytest.approx(0.30 * plan["net_size"])
    km = replace(loop._km(MKT), fv_cents=50.0)
    assert plan["net_per_day"] == pytest.approx(S.quote_economics(km, plan["net_size"])[0])
    # no fair value: the 48 h gate applies again and nothing rests
    loop2 = _loop(monkeypatch, fv=None, close_h=30.0)
    loop2._select(T0)
    assert MKT not in loop2.resting
    assert any(m == MKT and why.startswith("closes_within_48") for m, why in loop2.excluded)


def test_select_rests_only_the_positive_edge_side_or_nothing(monkeypatch):
    _on(monkeypatch)
    # FV 45: YES at 50 pays 5c over fair (withheld), NO at 52 vs 55 earns 3c
    loop = _loop(monkeypatch, fv=45.0, yes=((50, 2000),), no=((48, 2000),))
    loop._select(T0)
    q = loop.resting[MKT]
    assert q["yes"] == 0 and q["no"] > 0
    # FV 49: YES 50 (-1c) and NO 52 (51 - 52 = -1c) both pay up: not selected
    loop3 = _loop(monkeypatch, fv=49.0, yes=((50, 2000),), no=((52, 2000),))
    loop3._select(T0)
    assert MKT not in loop3.resting
    assert "net_per_day" not in loop3.last_plan.get(MKT, {})


def test_fail_closed_when_fair_value_disappears(monkeypatch):
    _on(monkeypatch)
    loop = _loop(monkeypatch, fv=50.0, close_h=30.0)
    loop._select(T0)
    assert MKT in loop.resting
    loop.fv.fv = None   # API failure / stale value
    loop._guard_resting(T0 + 1)
    assert MKT not in loop.resting
    assert loop.pulls.get("fv_unavailable") == 1
    assert not loop._quote(MKT, 30, 60, 100, T0 + 2)


def test_guard_pulls_side_when_fair_value_moves(monkeypatch):
    _on(monkeypatch)
    loop = _loop(monkeypatch, fv=50.0, close_h=100.0)   # long enough: not admitted on FV
    monkeypatch.setenv("LIP_FV_MAX_GIVEUP_CENTS", "15")
    assert loop._quote(MKT, 30, 60, 100, T0)
    assert loop.resting[MKT]["yes"] > 0 and loop.resting[MKT]["no"] > 0
    monkeypatch.setenv("LIP_FV_MAX_GIVEUP_CENTS", "0")
    loop._guard_resting(T0 + 1)
    q = loop.resting[MKT]
    assert q["yes"] > 0 and q["no"] == 0
    assert loop.pulls.get("fv_negative_edge") == 1
    loop.fv.fv = 20.0   # now YES pays up too
    loop._guard_resting(T0 + 2)
    assert MKT not in loop.resting


def test_status_reports_fv_quote(monkeypatch):
    _on(monkeypatch)
    loop = _loop(monkeypatch, fv=50.0)
    rep = loop.fv_quote_report()
    assert rep["enabled"] and rep["families"] == ["KXHIGH"] and rep["driving"] == [MKT]
    snap = loop.live_snapshot()
    assert snap["fair_value"]["fv_quote"]["driving_n"] == 1


# ---------------------------------------------------------------- screen
def _meta_row(ticker, close):
    return {"ticker": ticker, "close_time": close, "event_ticker": ticker.rsplit("-", 1)[0], "status": "active",
            "exchange_index": 0, "strike_type": "between", "floor_strike": 72, "cap_strike": 73,
            "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.40"}


def test_screen_keeps_strike_fields_and_feeds_kxhigh_under_fv_knob(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    now = T0
    close = datetime.fromtimestamp(now + 30 * 3600, timezone.utc).isoformat()
    meta = SC.market_meta(_meta_row(MKT, close), now)
    assert (meta["strike_type"], meta["floor_strike"], meta["cap_strike"]) == ("between", 72.0, 73.0)
    cache = SC.MetaCache(str(tmp_path / "c.json"))
    cache.markets[MKT] = meta
    cache.series["KXHIGHNY"] = {"category": "Climate and Weather", "fee_type": "quadratic_with_maker_fees"}
    frame = {"market": MKT, "series": "KXHIGHNY", "period_reward_usd": 50, "period_seconds": 86400,
             "end_ts": now + 86400, "target_size": 1000}
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    out, stats = SC.screen([frame], cache, now=now)
    assert out == [] and stats["reasons"].get("closes_within_48h") == 1
    _on(monkeypatch)
    out, _ = SC.screen([frame], cache, now=now)
    assert [f["market"] for f in out] == [MKT]
    assert out[0]["strike_type"] == "between" and out[0]["cap_strike"] == 73.0
    monkeypatch.setenv("LIP_FV_QUOTE_ENABLE", "0")
    assert SC.screen([frame], cache, now=now)[0] == []
    # a KXHIGH series the model cannot price is not fed early either
    monkeypatch.setenv("LIP_FV_QUOTE_ENABLE", "1")
    lax = "KXHIGHLAX-26OCT01-B72.5"
    cache.markets[lax] = SC.market_meta(_meta_row(lax, close), now)
    cache.series["KXHIGHLAX"] = cache.series["KXHIGHNY"]
    out, stats = SC.screen([dict(frame, market=lax, series="KXHIGHLAX")], cache, now=now)
    assert out == [] and stats["reasons"].get("closes_within_48h") == 1


# ---------------------------------------------------------------- fair-value cache wiring
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    def __init__(self, ens):
        self.ens = ens
        self.urls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        if url == W.ENSEMBLE_URL:
            return _Resp(200, self.ens)
        if "gamma-api" in url:
            return _Resp(503, {})
        if "/markets/" in url:
            return _Resp(200, {"market": {"title": "Highest temperature in NYC on Oct 1, 2026?",
                                          "strike_type": "greater", "floor_strike": 73}})
        return _Resp(404, {})


def _ens_payload():
    from datetime import datetime, timezone
    start = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    times = [datetime.fromtimestamp(start + 3600 * i, timezone.utc).strftime("%Y-%m-%dT%H:%M") for i in range(48)]
    hourly = {"time": times}
    for i in range(30):
        hourly[f"temperature_2m_member{i:02d}"] = [72.5] * 48
    return {"utc_offset_seconds": 0, "hourly": hourly}


def test_cache_prices_kxhigh_even_when_polymarket_fails(monkeypatch):
    _on(monkeypatch)
    http = _Http(_ens_payload())
    cache = F.FairValueCache(session=http, sleep=lambda s: None)
    cache.note_market(MKT, {"strike_type": "between", "floor_strike": 72, "cap_strike": 73})
    # freeze "now" before the window so the model uses the ensemble only
    from datetime import datetime, timezone
    monkeypatch.setattr(F.time, "time", lambda: datetime(2026, 9, 30, 15, tzinfo=timezone.utc).timestamp())
    with pytest.raises(RuntimeError):
        cache.refresh([MKT, "KXHIGHNY-26OCT01-T73", "KXHIGHLAX-26OCT01-B72.5"])
    row = cache.values[MKT]
    assert row["source"] == W.SOURCE and 60 < row["fv_cents"] < 75
    # no hint for the threshold market: strike fields came from the public market fetch
    assert cache.values["KXHIGHNY-26OCT01-T73"]["fv_cents"] < 50
    assert "KXHIGHLAX-26OCT01-B72.5" not in cache.values
    assert cache.summary()["weather_high"]["unsupported"] >= 1


def test_cache_bad_params_file_prices_nothing(monkeypatch, tmp_path):
    _on(monkeypatch)
    bad = tmp_path / "p.json"
    bad.write_text("{oops")
    monkeypatch.setenv("LIP_FV_WX_PARAMS_FILE", str(bad))
    cache = F.FairValueCache(session=_Http(_ens_payload()), sleep=lambda s: None)
    assert cache._high_rows([MKT], time.time()) == {}
    assert "params" in cache.wx.stats["last_error"]


def test_cache_flag_off_does_not_price(monkeypatch):
    http = _Http(_ens_payload())
    cache = F.FairValueCache(session=http, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        cache.refresh([MKT])
    assert MKT not in cache.values and W.ENSEMBLE_URL not in http.urls
