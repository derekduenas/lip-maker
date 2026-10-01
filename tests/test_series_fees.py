"""Per-series Kalshi fee resolution (engine/series_fees.py)."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine import fees
from engine.series_fees import SeriesFeeResolver, schedule_from_series


def test_quadratic_series_has_no_maker_fee_but_keeps_taker():
    s = schedule_from_series({"series": {"ticker": "KXRT", "fee_type": "quadratic",
                                         "fee_multiplier": 1}})
    assert s.fee_usd(50, 100, is_taker=False) == 0
    # 0.07 × 100 × 0.5 × 0.5 = $1.75
    assert s.fee_usd(50, 100, is_taker=True) == Decimal("1.75")


def test_maker_fee_series_charges_quarter_rate_times_multiplier():
    s = schedule_from_series({"ticker": "KXMLBGAME", "fee_type": "quadratic_with_maker_fees",
                              "fee_multiplier": 0.5})
    # 0.0175 × 0.5 × 100 × 0.25 = $0.21875
    assert s.fee_usd(50, 100, is_taker=False) == Decimal("0.21875")
    assert s.round_trip_usd(50, 50, 100) == Decimal("0.21875") + Decimal("0.875")
    assert s.verified is False


def test_combo_maker_fee_is_half_the_taker_coefficient():
    s = schedule_from_series({"ticker": "KXCOMBO", "fee_type": "quadratic_with_combo_maker_fees",
                              "fee_multiplier": 1})
    # 0.035 × 100 × 0.25 = $0.875
    assert s.fee_usd(50, 100, is_taker=False) == Decimal("0.875")
    assert s.fee_usd(50, 100, is_taker=True) == Decimal("1.75")
    assert schedule_from_series({"fee_type": "flat_new_thing"}) is None
    assert schedule_from_series({"fee_type": "quadratic", "fee_multiplier": "x"}) is None


def test_resolver_caches_and_falls_back():
    calls = []
    def fetch(series):
        calls.append(series)
        if series == "BAD":
            raise RuntimeError("down")
        return {"series": {"ticker": series, "fee_type": "quadratic", "fee_multiplier": 1}}
    fb = fees.KALSHI_UNVERIFIED
    r = SeriesFeeResolver(fetch, fb)
    a = r.for_ticker("KXRT-VER-35")
    b = r.for_ticker("KXRT-SENS-70")
    assert a is b and calls == ["KXRT"]
    assert r.for_ticker("BAD-1") is fb
    assert r.for_ticker("") is fb


def test_runner_uses_global_schedule_unless_enabled(monkeypatch):
    import run_paper as rp
    monkeypatch.setattr(settings, "SERIES_FEES_ENABLED", False)
    assert rp.PaperRunner._fee_schedule("KXRT-VER-35") is fees.active_schedule()
    monkeypatch.setattr(settings, "SERIES_FEES_ENABLED", True)
    monkeypatch.setattr(rp, "_SERIES_FEES", SeriesFeeResolver(
        lambda s: {"series": {"ticker": s, "fee_type": "quadratic", "fee_multiplier": 1}},
        fees.active_schedule()))
    sched = rp.PaperRunner._fee_schedule("KXRT-VER-35")
    assert sched.fee_usd(50, 10, is_taker=False) == 0
