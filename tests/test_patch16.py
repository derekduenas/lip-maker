"""Patch 16: external fair value guard (Polymarket cross-venue), defensive only."""
import time

from mm.unattended import loop as L
from mm.unattended import fairvalue as F
from tests.test_patch15 import T0, _prog, _book


class _Stub:
    def __init__(self, fv):
        self.fv = fv

    def get(self, market, now=None):
        return {"fv_cents": self.fv, "conf": 0.9, "pm_question": "q", "ts": time.time()}

    def summary(self):
        return {"enabled": True}


def test_match_requires_numbers_and_direction():
    k = "Will Bitcoin reach $150,000 in October? $150,000 or above"
    assert F.match_score("Will Bitcoin reach $150,000 in October?", "Will Bitcoin reach $150k in October?") == 1.0
    assert F.match_score("Will Bitcoin reach $150,000 in October?", "Will Bitcoin reach $140k in October?") == 0.0
    assert F.match_score("Will Bitcoin dip below $100,000 in October?", "Will Bitcoin reach $100k in October?") == 0.0
    assert F.match_score(k, "Will the Lakers win the 2026 NBA Finals?") == 0.0


def test_times_years_days_ignored_and_aliases():
    w, n = F.tokens("Will ETH be below $2500.00 by 11:59 PM ET on Oct 31, 2026?")
    assert n == {"2500"} and "ethereum" in w


def test_best_match_ambiguity_rejected():
    rows = [{"question": "Will Bitcoin reach $150k in October?"},
            {"question": "Will Bitcoin reach $150k in October 2026?"}]
    idx = {}
    for i, r in enumerate(rows):
        for w in F.tokens(r["question"])[0]:
            idx.setdefault(w, []).append(i)
    row, _ = F.best_match("Will Bitcoin reach $150,000 in October?", rows, idx, 0.6)
    assert row is None  # two equally good candidates => no reference


def test_pm_price_filters():
    m = {"outcomes": '["Yes","No"]', "liquidity": "5000", "bestBid": 0.40, "bestAsk": 0.44}
    assert F.pm_yes_cents(m, 0.06, 1000) == 42.0
    assert F.pm_yes_cents(dict(m, bestAsk=0.60), 0.06, 1000) is None
    assert F.pm_yes_cents(dict(m, liquidity="10"), 0.06, 1000) is None
    assert F.pm_yes_cents(dict(m, outcomes='["Up","Down"]'), 0.06, 1000) is None


def test_drop_sides():
    # Kalshi mid = (30 + 100-60)/2 = 35
    assert F.fv_drop_sides(50, 30, 60, 8, False) == ("no",)
    assert F.fv_drop_sides(20, 30, 60, 8, False) == ("yes",)
    assert F.fv_drop_sides(40, 30, 60, 8, False) == ()
    assert F.fv_drop_sides(50, 30, 60, 8, True) == ("yes", "no")
    assert F.fv_drop_sides(50, None, 60, 8, False) == ()


def _setup(monkeypatch, fv, enable="1"):
    monkeypatch.setenv("LIP_FV_ENABLE", enable)
    monkeypatch.setenv("LIP_FV_DISAGREE_CENTS", "8")
    monkeypatch.delenv("LIP_CROSS_GUARD", raising=False)
    loop = L.RunLoop(mode="paper")
    _prog(loop, "KXA-1")
    _book(loop, "KXA-1", [(30, 2000)], [(60, 2000)], T0)
    loop.fv = _Stub(fv)
    return loop


def test_quote_withholds_picked_off_side(monkeypatch):
    loop = _setup(monkeypatch, 55.0)
    assert loop._quote("KXA-1", 30, 60, 100, T0)
    q = loop.resting["KXA-1"]
    assert q["yes"] > 0 and q["no"] == 0
    assert loop.fv_blocks == {"no": 1}


def test_flag_off_is_noop(monkeypatch):
    loop = _setup(monkeypatch, 55.0, enable="0")
    assert loop._quote("KXA-1", 30, 60, 100, T0)
    q = loop.resting["KXA-1"]
    assert q["yes"] > 0 and q["no"] > 0


def test_agreeing_fv_keeps_both(monkeypatch):
    loop = _setup(monkeypatch, 36.0)
    assert loop._quote("KXA-1", 30, 60, 100, T0)
    q = loop.resting["KXA-1"]
    assert q["yes"] > 0 and q["no"] > 0


def test_guard_resting_drops_side_when_fv_moves(monkeypatch):
    loop = _setup(monkeypatch, 36.0)
    assert loop._quote("KXA-1", 30, 60, 100, T0)
    loop.fv = _Stub(15.0)
    loop._guard_resting(T0 + 1)
    q = loop.resting["KXA-1"]
    assert q["yes"] == 0 and q["no"] > 0
    assert loop.pulls.get("fv_disagree") == 1


def test_guard_resting_pull_both(monkeypatch):
    loop = _setup(monkeypatch, 36.0)
    monkeypatch.setenv("LIP_FV_PULL_BOTH", "1")
    assert loop._quote("KXA-1", 30, 60, 100, T0)
    loop.fv = _Stub(80.0)
    loop._guard_resting(T0 + 1)
    assert "KXA-1" not in loop.resting or not (loop.resting["KXA-1"].get("yes") or loop.resting["KXA-1"].get("no"))
    assert loop.pulls.get("fv_disagree") == 1


def test_ensemble_rain_prob():
    times = [f"2026-10-03T{h:02d}:00" for h in range(24)] + [f"2026-10-04T{h:02d}:00" for h in range(24)]
    hourly = {"time": times}
    for i in range(10):
        vals = [0.0] * 48
        if i < 3:
            vals[30] = 0.5  # wet on day 2
        hourly[f"precipitation_member{i:02d}"] = vals
    assert F.ensemble_rain_prob(hourly, ["2026-10-03", "2026-10-04"]) == 0.3
    assert F.ensemble_rain_prob({"time": []}, ["2026-10-03"]) is None


def test_row_threshold_override(monkeypatch):
    loop = _setup(monkeypatch, 55.0)

    class _W(_Stub):
        def get(self, market, now=None):
            return dict(super().get(market), thr=25.0)
    loop.fv = _W(55.0)  # 20c off mid, under the 25c weather threshold
    assert loop._quote("KXA-1", 30, 60, 100, T0)
    assert loop.resting["KXA-1"]["no"] > 0
