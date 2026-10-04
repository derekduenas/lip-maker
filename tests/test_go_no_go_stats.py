"""Oct 10 go/no-go statistical bar (mm/unattended/go_no_go.py): pure functions."""
import math

import pytest

from mm.unattended import go_no_go as G


def _events(means_cents, contracts=10.0, fills=2):
    """event -> [fills, contracts, usd] with the given per-event mean cents/contract."""
    return {f"E{i}": [fills, contracts, m * contracts / 100.0] for i, m in enumerate(means_cents)}


def test_event_stats_mean_sd_se_and_lower_bound():
    st = G.event_stats(_events([1.0, 2.0, 3.0, 4.0]))
    assert st["events"] == 4 and st["fills"] == 8
    assert st["mean_cents"] == pytest.approx(2.5)
    assert st["sd_cents"] == pytest.approx(math.sqrt(5 / 3))
    assert st["se_cents"] == pytest.approx(st["sd_cents"] / 2)
    assert st["lower_90_cents"] == pytest.approx(2.5 - 1.638 * st["se_cents"])   # t(3) one-sided 90%


def test_many_fills_in_one_event_are_one_observation():
    """Ten fills in one event must not look like ten independent samples."""
    one_event = G.event_stats({"E": [10, 100.0, -5.0]})
    assert one_event["events"] == 1 and one_event["fills"] == 10
    assert one_event["se_cents"] is None and one_event["lower_90_cents"] is None


def test_events_without_contracts_are_ignored_and_empty_is_safe():
    assert G.event_stats({})["events"] == 0
    assert G.event_stats({"E": [0, 0.0, 0.0]})["events"] == 0


def test_events_needed_matches_the_planning_example():
    # SD 4c, detect +0.5c at 80% power, one-sided alpha 5%: (2.485*4/0.5)^2 = 395.2, rounded up
    assert G.events_needed(4.0, 0.5) == 396
    assert G.events_needed(None, 0.5) is None and G.events_needed(4.0, 0.0) is None


def test_t_critical_values_are_monotone_and_converge():
    vals = [G.t_crit_90(d) for d in (1, 2, 5, 10, 30, 60, 120, 1000)]
    assert vals == sorted(vals, reverse=True) and vals[-1] == pytest.approx(1.282)
    assert G.t_crit_90(0) == float("inf")


CFG = {"min_events": 5, "target_edge_cents": 0.5, "reward_haircut": 0.5, "min_frozen_days": 3.0}


def test_go_needs_the_lower_bound_above_zero_on_enough_events_and_a_frozen_config():
    good = G.event_stats(_events([0.4, 0.6, 0.5, 0.7, 0.3, 0.5]))
    v = G.verdict(good, reward_cents_per_contract=0.0, frozen_days=5.0, cfg=CFG)
    assert v["verdict"] == "GO" and v["edge_lower_90_cents"] > 0
    v = G.verdict(good, reward_cents_per_contract=0.0, frozen_days=1.0, cfg=CFG)
    assert (v["verdict"], v["why"]) == ("INSUFFICIENT", "parameters_not_frozen")
    few = G.event_stats(_events([0.4, 0.6, 0.5]))
    assert G.verdict(few, reward_cents_per_contract=0.0, frozen_days=9.0, cfg=CFG)["why"] == "events"


def test_reward_counts_only_after_the_haircut():
    st = G.event_stats(_events([-1.0, -1.1, -0.9, -1.0, -1.05, -0.95]))
    no_reward = G.verdict(st, reward_cents_per_contract=0.0, frozen_days=9.0, cfg=CFG)
    assert no_reward["verdict"] == "NO_GO"
    # 4c/contract of estimated reward, 50% haircut -> +2c: the same markout now clears it
    with_reward = G.verdict(st, reward_cents_per_contract=4.0, frozen_days=9.0, cfg=CFG)
    assert with_reward["reward_cents_per_contract_after_haircut"] == 2.0
    assert with_reward["verdict"] == "GO"


def test_noisy_interval_that_spans_zero_is_insufficient_not_go_or_no_go():
    st = G.event_stats(_events([3.0, -3.0, 2.5, -2.0, 1.0, -1.5]))
    v = G.verdict(st, reward_cents_per_contract=0.0, frozen_days=9.0, cfg=CFG)
    assert (v["verdict"], v["why"]) == ("INSUFFICIENT", "interval_spans_zero")
    assert v["events_needed_for_target"] > 6


def test_no_data_is_insufficient():
    v = G.verdict(G.event_stats({}), reward_cents_per_contract=1.0, frozen_days=9.0, cfg=CFG)
    assert (v["verdict"], v["why"]) == ("INSUFFICIENT", "too_few_events")


# ---------------------------------------------------------------- fingerprint
def test_fingerprint_is_stable_ignores_order_and_excludes_secrets():
    a = {"LIP_SAMPLE_N": "8", "LIP_SKEW_MAX_TICKS": "2", "HOME": "/x"}
    b = {"HOME": "/y", "LIP_SKEW_MAX_TICKS": "2", "LIP_SAMPLE_N": "8"}
    assert G.fingerprint(a)[0] == G.fingerprint(b)[0]
    fp, params = G.fingerprint(dict(a, LIP_ALERT_WEBHOOK="https://secret", LIP_WD_NTFY_TOPIC="t",
                                    LIP_WD_KALSHI_KEY_ID="k"))
    assert fp == G.fingerprint(a)[0]
    assert set(params) == {"LIP_SAMPLE_N", "LIP_SKEW_MAX_TICKS"}
    assert G.fingerprint(dict(a, LIP_SAMPLE_N="9"))[0] != G.fingerprint(a)[0]


def test_param_history_counts_trials_and_restarts_the_frozen_window():
    h = []
    assert G.note_params(h, 1000.0, {"LIP_A": "1"}) is True
    assert G.note_params(h, 2000.0, {"LIP_A": "1"}) is False
    assert G.note_params(h, 3000.0, {"LIP_A": "2"}) is True and len(h) == 2
    assert G.frozen_days(h, 3000.0 + 86400.0 * 4) == pytest.approx(4.0)
    assert G.frozen_days([], 5.0) == 0.0
