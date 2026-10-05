"""The Oct 10 evidence must say which fills came from the sampling group (built to COLLECT fills, quoted at
best bid in busy markets) and which from reward quoting (what would actually run live)."""
import pytest

from tests.test_audit_2026_10_04 import M, T0, YES, NO, _campaign, newloop, program, snap  # noqa: F401
from tests.test_review_loop_pnl import _env  # noqa: F401


def _manual_mark(lp, market, sample, count=10.0, price=40.0, ts=None):
    ts = T0 if ts is None else ts
    lp.fill_marks.append({"market": market, "side": "yes", "price_cents": price, "count": count, "ts": ts, "mid0": 41.0,
                          "venue": "kalshi", "bucket": None, "markout_60s": None, "markout_300s": None,
                          "markout_1800s": None, "synthetic": False, "sample": sample})


def test_group_tables_split_sampling_from_reward_fills(monkeypatch, tmp_path):
    lp = _campaign(monkeypatch, tmp_path / "s.json")                   # one sampling-group fill, 5-min markout measured
    ev = lp._event_of(M)
    assert ev in lp.group_event_acc["sample"] and not lp.group_event_acc.get("reward")
    _manual_mark(lp, M, sample=False, ts=lp.now - 400)
    lp._update_markouts(lp.now)
    assert ev in lp.group_event_acc["reward"]
    assert lp.event_acc[ev][0] == 2                                    # the pooled table still holds both


def test_the_report_shows_a_verdict_per_group_and_says_which_one_the_headline_pools(monkeypatch, tmp_path):
    lp = _campaign(monkeypatch, tmp_path / "s.json")
    gn = lp.series_gate_report()["go_no_go"]
    assert set(gn["by_group"]) == {"sample", "reward"}
    assert gn["by_group"]["sample"]["statistics"]["events"] == 1
    assert gn["by_group"]["reward"]["statistics"]["events"] == 0
    assert "pools" in gn["headline_note"]
    assert gn["by_group"]["sample"]["verdict"]["reward_cents_per_contract_after_haircut"] == 0.0   # no reward credited to the group built to collect fills


def test_group_tables_survive_a_restart(monkeypatch, tmp_path):
    lp = _campaign(monkeypatch, tmp_path / "s.json")
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.kill is None and lp2.group_event_acc == lp.group_event_acc


def test_corrupt_group_tables_do_not_latch_the_kill(monkeypatch, tmp_path):
    import json
    lp = _campaign(monkeypatch, tmp_path / "s.json")
    lp.save_state(force=True)
    d = json.loads((tmp_path / "s.json").read_text())
    d["group_event_acc"] = {"sample": {"E": ["x", 1, 2]}, "reward": "junk"}
    (tmp_path / "s.json").write_text(json.dumps(d))
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.kill is None and lp2.state_error is None
