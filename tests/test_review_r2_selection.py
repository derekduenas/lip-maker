"""Selection credit for a model fair value is the edge versus the book mid
(direction-aware per resting side), only once the calibration verdict
passes; negative-edge sides are still withheld. The per-side EV gate prices
a lone resting side with its one-sided snapshot share."""
from dataclasses import replace

import pytest

from mm import selector as S
from mm.unattended import loop as L
from tests.test_fv_quote import MKT, _FV, _km, _on, _prog, _env  # noqa: F401
from tests.test_patch15 import T0, _book

FILLS = 4.0   # weather: 0.04/day x 100 contracts per side; book 30 / 60 -> mid 35


def test_no_credit_before_calibration_passes():
    base_yes = S.quote_economics(_km(), 100, sides=("yes",))
    one = S.quote_economics(_km(fv=50.0), 100)                  # NO pays up: YES only
    assert one[0] == pytest.approx(base_yes[0])
    assert S.fv_capture_per_day(_km(fv=50.0), 100, 30, 60, sides=("yes",)) == 0.0


def test_both_sides_resting_earn_no_fv_credit():
    # The old credit was the raw spread (FV-independent, linear in size).
    base = S.quote_economics(_km(), 100)
    for fv in (31.0, 35.0, 40.0):          # both sides rest (edge >= 0 on each)
        km = _km(fv=fv, fv_calibrated=True)
        assert S.fv_capture_per_day(km, 100, 30, 60) == pytest.approx(0.0)
        assert S.quote_economics(km, 100)[0] == pytest.approx(base[0])


def test_calibrated_one_sided_credit_is_edge_vs_mid():
    base_yes = S.quote_economics(_km(), 100, sides=("yes",))
    km = _km(fv=50.0, fv_calibrated=True)
    assert S.fv_capture_per_day(km, 100, 30, 60, sides=("yes",)) == pytest.approx(FILLS * (50 - 35) / 100)
    assert S.fv_capture_per_day(km, 100, 30, 60, sides=("no",)) == pytest.approx(FILLS * (35 - 50) / 100)
    assert S.quote_economics(km, 100)[0] == pytest.approx(base_yes[0] + FILLS * 0.15)
    # no two-sided book: no mid, no credit
    assert S.fv_capture_per_day(_km(fv=50.0, fv_calibrated=True, no=()), 100, 30, 60, sides=("yes",)) == 0.0
    # negative-edge sides are still withheld, calibrated or not
    assert S.quote_economics(_km(fv=29.5, no=((71, 2000.0),), fv_calibrated=True), 100)[:2] == (0.0, 0.0)


def test_loop_marks_fv_calibrated_from_the_verdict(monkeypatch):
    _on(monkeypatch)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _FV(50.0)
    _prog(loop)
    _book(loop, MKT, [(30, 2000)], [(60, 2000)], T0)
    assert loop._km(MKT).fv_cents == 50.0 and loop._km(MKT).fv_calibrated is False
    monkeypatch.setattr(loop.fv_calib, "passed", lambda: True)
    assert loop._km(MKT).fv_calibrated is True
    loop.fv = _FV(50.0, conf=0.1)                           # no usable row: nothing to credit
    assert loop._km(MKT).fv_calibrated is False


def test_ev_gate_prices_a_lone_side_with_its_one_sided_share(monkeypatch):
    _on(monkeypatch)
    loop = L.RunLoop(mode="paper", bankroll=5000)
    loop.fv = _FV(30.0)
    _prog(loop)
    # NO book 950 < target 1000: with our NO it counts, without it Kalshi
    # excludes the snapshot, so a YES-only quote earns no reward.
    _book(loop, MKT, [(30, 2000)], [(60, 950)], T0)
    loop.now = T0
    km = loop._km(MKT)
    assert S.reward_per_day(S.kalshi_share(km, 30, 60, 100.0), km) / 2 > 1.0
    assert S.reward_per_day(S.kalshi_one_sided_share(km, "yes", 30, 100.0), km) == 0.0
    row = loop._fv_quote_row(MKT)
    loop.fv_quote_stats.clear()
    # YES edge 0: EV = 0 x fills + 0 reward - fees < 0
    assert loop._fv_gate_sides(MKT, row, 30, 60, ("yes",), 100.0) == ()
    assert loop.fv_quote_stats.get("ev_withheld_yes") == 1
    # with both sides resting the two-sided share applies
    _book(loop, MKT, [(30, 2000)], [(60, 950)], T0 + 1)
    loop.fv = _FV(35.0)
    assert loop._fv_gate_sides(MKT, loop._fv_quote_row(MKT), 30, 60, ("yes", "no"), 100.0) == ("yes", "no")
