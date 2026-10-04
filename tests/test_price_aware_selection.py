"""Gaps 6 and 11: opt-in price floor, news-category exclusion, carry offset, and the
event-level markout-by-price-bucket data + report tool. Defaults unchanged."""
import json

import pytest

from mm import selector as SEL
from mm.selector import KalshiMarket
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401


def _km(yes, no, category=None, days=30.0):
    return KalshiMarket(market="KXCPI-26NOV30-T3", series="KXCPI", period_reward_usd=500,
                        period_seconds=86400, seconds_left=86400, discount_factor=0.5, target_size=100,
                        yes_bids=yes, no_bids=no, days_to_settle=days, category=category)


# ---------------------------------------------------------------- price floor
def test_price_floor_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LIP_MIN_SIDE_PRICE_CENTS", raising=False)
    assert SEL.exclusion_reason(_km([(4, 500)], [(94, 500)])) == ""


def test_price_floor_excludes_the_longshot_side_either_way(monkeypatch):
    monkeypatch.setenv("LIP_MIN_SIDE_PRICE_CENTS", "10")
    assert SEL.exclusion_reason(_km([(4, 500)], [(94, 500)])).startswith("side_price_below_10c")   # YES ~5c
    assert SEL.exclusion_reason(_km([(94, 500)], [(4, 500)])).startswith("side_price_below_10c")   # NO ~5c
    assert SEL.exclusion_reason(_km([(44, 500)], [(53, 500)])) == ""


def test_price_floor_ignores_an_unknown_book(monkeypatch):
    monkeypatch.setenv("LIP_MIN_SIDE_PRICE_CENTS", "10")
    assert SEL.exclusion_reason(_km([], [])) == ""
    assert SEL.exclusion_reason(_km([(4, 500)], [])) == ""


# ---------------------------------------------------------------- news categories
def test_news_exclusion_is_opt_in_and_uses_the_configured_category_set(monkeypatch):
    monkeypatch.delenv("LIP_EXCLUDE_NEWS_CATEGORIES", raising=False)
    assert SEL.exclusion_reason(_km([(44, 500)], [(53, 500)], category="Politics")) == ""
    monkeypatch.setenv("LIP_EXCLUDE_NEWS_CATEGORIES", "1")
    assert SEL.exclusion_reason(_km([(44, 500)], [(53, 500)], category="Politics")) == "news_category"
    assert SEL.exclusion_reason(_km([(44, 500)], [(53, 500)], category="Economics")) == ""
    assert SEL.exclusion_reason(_km([(44, 500)], [(53, 500)], category=None)) == ""
    monkeypatch.setenv("LIP_NEWS_CATEGORIES", "Economics")
    assert SEL.exclusion_reason(_km([(44, 500)], [(53, 500)], category="Economics")) == "news_category"


# ---------------------------------------------------------------- carry offset
def test_carry_apy_offset_nets_the_interest_and_never_goes_negative(monkeypatch):
    monkeypatch.setenv("LIP_CARRY_APR", "0.10")
    monkeypatch.delenv("LIP_CARRY_APY_OFFSET", raising=False)
    assert SEL.carry_apr() == pytest.approx(0.10)
    monkeypatch.setenv("LIP_CARRY_APY_OFFSET", "0.035")
    assert SEL.carry_apr() == pytest.approx(0.065)
    monkeypatch.setenv("LIP_CARRY_APY_OFFSET", "0.5")
    assert SEL.carry_apr() == 0.0
    monkeypatch.setenv("LIP_CARRY_APY_OFFSET", "oops")
    assert SEL.carry_apr() == pytest.approx(0.10)


# ---------------------------------------------------------------- engine data
def test_price_bucket_markouts_are_kept_per_event_with_a_cap(monkeypatch):
    from mm.unattended import loop as L
    lp = newloop(bankroll=1500.0)
    for i in range(L.PRICE_EVENT_KEEP + 5):
        lp._price_event_note("30-70", f"EV{i}", 2.0, 20.0, -0.4)
    assert len(lp.price_event_acc["30-70"]) == L.PRICE_EVENT_KEEP
    assert "EV0" not in lp.price_event_acc["30-70"] and f"EV{L.PRICE_EVENT_KEEP + 4}" in lp.price_event_acc["30-70"]


def test_status_price_bucket_report_has_event_level_confidence(monkeypatch):
    lp = newloop(bankroll=1500.0)
    for i, m in enumerate([-0.5, -0.4, -0.6, -0.5, -0.45, -0.55]):
        lp._price_event_note("10-30", f"EV{i}", 2.0, 20.0, m * 20.0 / 100.0)
        lp.price_acc.setdefault("10-30", [0, 0.0, 0.0])
        lp.price_acc["10-30"][0] += 2
        lp.price_acc["10-30"][1] += 20.0
        lp.price_acc["10-30"][2] += m * 20.0 / 100.0
    row = lp.series_gate_report()["go_no_go"]["markout_by_price_bucket"]["10-30"]
    assert row["events"] == 6 and row["cents_per_contract"] == pytest.approx(-0.5, abs=0.01)
    assert row["lower_90_cents"] < row["cents_per_contract"] < row["upper_90_cents"]
    empty = lp.series_gate_report()["go_no_go"]["markout_by_price_bucket"]["<10"]
    assert empty["events"] == 0 and empty["lower_90_cents"] is None


def test_price_event_data_survives_a_restart(monkeypatch, tmp_path):
    lp = newloop(bankroll=1500.0)
    lp.attach_state(str(tmp_path / "s.json"))
    lp._price_event_note("<10", "EV1", 3.0, 30.0, -0.9)
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.price_event_acc == lp.price_event_acc


# ---------------------------------------------------------------- report tool
def _state(tmp_path, buckets):
    data = {"version": 1, "price_event_acc": buckets,
            "series_acc": {"KXCPI": {"fills": 20, "fees": 0.1, "settled_fills": 10, "settled_usd": 1.0,
                                     "mk5_usd": -1.0, "mk5_contracts": 200.0, "mk5_n": 20}}}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_report_tool_recommends_a_floor_only_when_the_whole_interval_is_adverse(tmp_path):
    from tools import state_markout_report as R
    bad = {f"E{i}": [2, 20.0, -0.4 * 20.0 / 100.0 * (1 + 0.05 * (i % 3))] for i in range(40)}
    ok = {f"E{i}": [2, 20.0, 0.1 * 20.0 / 100.0 * (1 + 0.05 * (i % 3))] for i in range(40)}
    rep = R.build_report(_state(tmp_path, {"<10": bad, "30-70": ok}))
    rows = {r["bucket"]: r for r in rep["buckets"]}
    assert rows["<10"]["recommendation"].startswith("consider a floor")
    assert rows["30-70"]["recommendation"] == "no action"
    assert rows["70-90"]["recommendation"] == "insufficient data"
    assert "never applied" in rep["note"]


def test_report_tool_handles_a_state_file_without_the_new_fields(tmp_path):
    from tools import state_markout_report as R
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1}))
    rep = R.build_report(str(path))
    assert all(r["recommendation"] == "insufficient data" for r in rep["buckets"])
