from tools import perf_log as P


def _row(t, rew, fills=0, resting=10, plan=20.0, starved=False, known=0, idle=0):
    return {"ts": t, "rewards_est": rew, "fills": fills, "quotes": fills * 5, "resting": resting, "plan_day": plan,
            "starved": starved, "known_s": known, "idle_s": idle}


def test_snapshot_extracts_the_few_numbers_that_matter():
    s = {"pnl_attribution": {"est_rewards_kalshi_usd": 3.85}, "fills_n": 119, "resting_n": 0, "selected_n": 0, "quotes_n": 1505,
         "venues": {"kalshi": {"plan_net_usd_per_day": 12.5}}, "capital": {"starved": True, "kalshi": {"budget_usd": 1.9, "locked_usd": 425.6}},
         "accrual_seconds": {"known": 100, "idle": 900}, "pulls": {"fast_move": 50, "trade_through": 23},
         "checkpoint": {"kalshi_fills": {"from_sampling_group": 116}, "markout_5m": {"fills": 119, "cents_per_contract": 0.09}}}
    r = P.snapshot(s, now=1000.0)
    assert r["rewards_est"] == 3.85 and r["pulls"] == 73 and r["plan_day"] == 12.5 and r["starved"] is True and r["fills_sample"] == 116


def test_report_turns_growth_into_a_per_day_rate_and_compares_with_the_plan():
    rows = [_row(0, 10.0), _row(3600, 10.5), _row(7200, 11.0)]          # +$0.5/h = $12/day vs plan $20
    rep = P.report(rows, now=7200)
    w = rep["windows"]["6h"]
    assert w["rewards_per_day"] == 12.0 and w["realized_vs_plan"] == 0.6 and w["quoting_share"] == 1.0


def test_report_flags_starvation_idle_and_a_plan_that_overstates():
    starved = [_row(i * 600, 5.0, resting=0, plan=0.0, starved=True) for i in range(12)]
    assert any("starved" in f for f in P.report(starved)["flags"])
    free_but_silent = [_row(i * 600, 5.0, resting=0, plan=10.0) for i in range(12)]
    assert any("nothing resting" in f for f in P.report(free_but_silent)["flags"])
    overstated = [_row(i * 3600, 5.0 + i * 0.05, plan=30.0) for i in range(8)]       # ~$1.2/day vs $30
    assert any("of the engine's own plan" in f for f in P.report(overstated)["flags"])
    healthy = [_row(i * 3600, 5.0 + i * 1.0, plan=20.0) for i in range(8)]           # $24/day vs $20
    assert P.report(healthy)["flags"] == ["no structural problem seen in the logged window"]


def test_append_keeps_a_week_and_survives_garbage(tmp_path):
    f = str(tmp_path / "p.jsonl")
    with open(f, "w") as fh:
        fh.write("not json\n")
    P.append(f, _row(0.0, 1.0))
    P.append(f, _row(10 * 86400.0, 2.0))
    assert [r["rewards_est"] for r in P.load(f)] == [2.0]               # the 10-day-old row aged out
    assert "samples: 1" in P.render(P.report(P.load(f)))
