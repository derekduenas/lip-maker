"""Capital efficiency: the paper engine locked the full cost of YES+NO pairs until settlement, so a few
sampling-group fills starved the whole budget (92% of market-seconds idle, ~$3 of rewards). Opt-in
LIP_PAIR_RELEASE models Kalshi netting (a pair is worth exactly $1 and frees its cost now), and the
engine now says out loud when it is capital-starved."""
import json
from decimal import Decimal

import pytest

from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401


def _pair_position(lp, yes=40.0, no=41.0, yes_cost=27.6, no_cost=13.0):
    lp.position[M] = {"yes": yes, "no": no, "yes_cost": yes_cost, "no_cost": no_cost, "fees": 0.0, "venue": "kalshi"}
    lp._sync_inventory(M)


def test_default_locks_the_full_cost_of_a_pair(monkeypatch):
    monkeypatch.delenv("LIP_PAIR_RELEASE", raising=False)
    lp = newloop(bankroll=1500.0)
    _pair_position(lp)
    assert lp.locked_usd()["kalshi"] == pytest.approx(40.6)


def test_pair_release_frees_the_paired_part_and_keeps_the_unpaired_cost(monkeypatch):
    monkeypatch.setenv("LIP_PAIR_RELEASE", "1")
    lp = newloop(bankroll=1500.0)
    _pair_position(lp)                                   # 40 paired, 1 extra NO at 13.0/41 each
    assert lp.locked_usd()["kalshi"] == pytest.approx(1 * 13.0 / 41.0)
    assert lp.venue_budgets()["kalshi"] > 400.0           # the cap is available again


def test_pair_release_does_not_change_the_marked_pnl(monkeypatch):
    monkeypatch.delenv("LIP_PAIR_RELEASE", raising=False)
    a = newloop(bankroll=1500.0)
    a.on_frame(program(M)); a.on_frame(snap(M, T0, [(40, 2000)], [(55, 2000)]))
    _pair_position(a)
    base = a.pnl_parts()["markout_usd"]
    monkeypatch.setenv("LIP_PAIR_RELEASE", "1")
    b = newloop(bankroll=1500.0)
    b.on_frame(program(M)); b.on_frame(snap(M, T0, [(40, 2000)], [(55, 2000)]))
    _pair_position(b)
    assert b.pnl_parts()["markout_usd"] == pytest.approx(base)     # a pair is worth $1 either way


def test_the_assumption_is_disclosed_in_the_go_no_go_report(monkeypatch):
    monkeypatch.setenv("LIP_PAIR_RELEASE", "1")
    lp = newloop(bankroll=1500.0)
    gn = lp.series_gate_report()["go_no_go"]
    assert gn["assumptions"]["pair_release"] is True
    monkeypatch.delenv("LIP_PAIR_RELEASE")
    assert newloop(bankroll=1500.0).series_gate_report()["go_no_go"]["assumptions"]["pair_release"] is False


def test_capital_report_flags_starvation_and_splits_paired_from_unpaired(monkeypatch):
    monkeypatch.delenv("LIP_PAIR_RELEASE", raising=False)
    lp = newloop(bankroll=1500.0)
    rep = lp.capital_report()
    assert rep["starved"] is False
    lp.position[M] = {"yes": 900.0, "no": 900.0, "yes_cost": 213.0, "no_cost": 213.0, "fees": 0.0, "venue": "kalshi"}
    lp._sync_inventory(M)
    rep = lp.capital_report()
    assert rep["kalshi"]["locked_usd"] == pytest.approx(426.0)
    assert rep["kalshi"]["locked_paired_usd"] == pytest.approx(426.0) and rep["kalshi"]["locked_unpaired_usd"] == 0.0
    assert rep["starved"] is True and rep["pair_release"] is False
    assert "pair release" in rep["hint"].lower()
    assert "capital" in lp.live_snapshot()


def test_starvation_alerts_once_after_it_has_lasted(monkeypatch):
    monkeypatch.delenv("LIP_PAIR_RELEASE", raising=False)
    monkeypatch.setenv("LIP_STARVED_ALERT_S", "600")
    lp = newloop(bankroll=1500.0)
    lp.position[M] = {"yes": 900.0, "no": 900.0, "yes_cost": 213.0, "no_cost": 213.0, "fees": 0.0, "venue": "kalshi"}
    lp._sync_inventory(M)
    lp._note_starvation(T0)
    assert lp.alerts == []                               # just started
    lp._note_starvation(T0 + 601)
    assert [a["level"] for a in lp.alerts] == ["WARNING"] and "capital" in lp.alerts[0]["message"].lower()
    lp._note_starvation(T0 + 700)
    assert len(lp.alerts) == 1                           # once per episode
    lp.position.clear(); lp.inv_committed.clear(); lp.risk.venue_usd.clear()
    lp._note_starvation(T0 + 800)
    assert lp._starved_since is None                     # episode ends when capital frees up


def test_perf_summary_renders_a_status_dict_and_names_the_capital_problem():
    from tools import perf_summary
    s = {"mode": "paper", "live_armed": False, "data_source": "production-books", "pnl_usd": 2.97,
         "capital": {"starved": True, "kalshi": {"budget_usd": 1.9, "locked_usd": 425.6, "cap_usd": 427.5,
                                                   "locked_paired_usd": 400.0, "locked_unpaired_usd": 25.6},
                     "pair_release": False, "hint": "enable LIP_PAIR_RELEASE"},
         "accrual_seconds": {"known": 144368, "idle": 1727083}}
    out = perf_summary.render(s)
    assert "STARVED" in out and "idle" in out.lower() and "92" in out
    assert perf_summary.render({}) and "STARVED" not in perf_summary.render({"mode": "paper"})


def test_perf_summary_shows_the_group_split():
    from tools import perf_summary
    s = {"series_gate": {"go_no_go": {"verdict": {"verdict": "INSUFFICIENT", "why": "events"}, "statistics": {},
                                      "by_group": {"sample": {"statistics": {"events": 9, "fills": 40, "mean_cents": 0.1, "pooled_cents": 0.2},
                                                              "verdict": {"verdict": "INSUFFICIENT", "why": "events"}},
                                                   "reward": {"statistics": {"events": 0, "fills": 0}, "verdict": {"verdict": "INSUFFICIENT", "why": "too_few_events"}}}}}}
    out = perf_summary.render(s)
    assert "group sample" in out and "group reward" in out and "9 events" in out
