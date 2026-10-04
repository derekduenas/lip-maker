"""Gap 12: paid vs estimated LIP rewards reconciled in the running engine.

Provenance rules: only credits whose source is an independent record count as paid;
estimates are never written back as actual; the go/no-go haircut follows the measured
paid/estimated ratio only with enough matched periods and paid days."""
import json
from decimal import Decimal

import pytest

from mm.unattended import reward_recon as RR
from tests.test_review_loop_pnl import M, T0, _env, newloop, program, snap  # noqa: F401


def _est(market, pid, est, series="KXCPI", start="2026-10-01"):
    return {"market": market, "program_id": pid, "series": series, "period_start": start,
            "estimated_usd": str(est)}


def _credit(market, pid, amount, source="kalshi_statement", start="2026-10-01", **kw):
    return dict({"kind": "liquidity_reward", "source": source, "market": market, "program_id": pid,
                 "amount_usd": amount, "period_start": start}, **kw)


# ---------------------------------------------------------------- pure
def test_only_independent_sources_count_as_paid_and_the_rest_is_counted_not_hidden():
    out = RR.reconcile_periods(
        [_est("A", "p1", "2.00")],
        [_credit("A", "p1", "1.50"),
         _credit("A", "p1", "9.99", source="our_model"),            # estimate masquerading as paid
         {"kind": "balance_delta", "amount_usd": "3", "source": "kalshi_api", "market": "A", "program_id": "p1"}])
    assert out["paid_usd"] == "1.50" and out["estimated_usd"] == "2.00"
    assert out["rejected"] == {"untagged_source": 1, "not_a_liquidity_reward": 1}
    assert out["ratio"] == "0.75" and out["matched"] == 1


def test_unmatched_estimates_and_credits_are_reported_separately():
    out = RR.reconcile_periods([_est("A", "p1", "2.00"), _est("B", "p2", "1.00")],
                               [_credit("A", "p1", "2.00"), _credit("Z", "pz", "5.00")])
    assert out["unmatched_estimates"] == 1 and out["unmatched_credits"] == 1
    assert out["paid_usd"] == "2.00" and out["unmatched_credit_usd"] == "5.00"


def test_per_series_ratio_and_paid_days():
    ests = [_est("A", "p1", "2.00", "KXCPI", "2026-10-01"), _est("B", "p2", "4.00", "KXNFP", "2026-10-02")]
    cr = [_credit("A", "p1", "1.00", start="2026-10-01"), _credit("B", "p2", "4.00", start="2026-10-02")]
    out = RR.reconcile_periods(ests, cr)
    assert out["by_series"]["KXCPI"]["ratio"] == "0.5" and out["by_series"]["KXNFP"]["ratio"] == "1"
    assert out["paid_days"] == 2


def test_haircut_recommendation_needs_enough_matches_and_days_and_has_a_floor():
    ok = {"matched": 12, "paid_days": 4, "ratio": "0.60"}
    assert RR.haircut_recommendation(ok) == pytest.approx(0.4)
    assert RR.haircut_recommendation(dict(ok, matched=9)) is None
    assert RR.haircut_recommendation(dict(ok, paid_days=2)) is None
    assert RR.haircut_recommendation(dict(ok, ratio=None)) is None
    assert RR.haircut_recommendation(dict(ok, ratio="1.30")) == pytest.approx(0.2)    # floor: never trust > 80%
    assert RR.haircut_recommendation(dict(ok, ratio="0.00")) == pytest.approx(1.0)


def test_estimates_with_zero_value_have_no_ratio_and_never_divide_by_zero():
    out = RR.reconcile_periods([_est("A", "p1", "0")], [_credit("A", "p1", "1.00")])
    assert out["ratio"] is None


# ---------------------------------------------------------------- file loader
def test_credit_file_is_loaded_deduplicated_and_validated(tmp_path):
    path = tmp_path / "credits.jsonl"
    rows = [_credit("A", "p1", "1.50"), _credit("A", "p1", "1.50"),
            _credit("A", "p2", "1.00", source="our_model")]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    ledger = []
    res = RR.load_credit_file(str(path), ledger)
    assert res["added"] == 2 and res["duplicates"] == 1 and res["bad_lines"] == 1
    assert len(ledger) == 2 and all(e["entry_id"] for e in ledger)
    again = RR.load_credit_file(str(path), ledger)
    assert again["added"] == 0 and again["duplicates"] == 3 and len(ledger) == 2


def test_missing_credit_file_is_not_an_error(tmp_path):
    assert RR.load_credit_file(str(tmp_path / "nope"), [])["error"] == "unreadable"


# ---------------------------------------------------------------- engine
def _period(lp, market=M, pid="p-" + M):
    """Run a short quoted period and archive it; returns the loop."""
    lp.on_frame(program(market))
    lp.on_frame(snap(market, T0, [(40, 2000), (39, 2000)], [(55, 2000), (54, 2000)]))
    for i in range(1, 40):
        lp.on_frame({"type": "clock", "ts": T0 + i})
    return lp


def test_archived_periods_record_an_estimate_with_the_real_program_id(tmp_path):
    lp = newloop(bankroll=1500.0)
    _period(lp)
    lp.end_program(M, "program_end")
    assert len(lp.period_estimates) == 1
    row = lp.period_estimates[0]
    assert row["market"] == M and row["program_id"] == "p-" + M and row["series"] == "KXCPI"
    assert Decimal(row["estimated_usd"]) >= 0 and row["period_start"]


def test_engine_reports_paid_vs_estimated_and_never_counts_estimates_as_paid(tmp_path):
    lp = newloop(bankroll=1500.0)
    _period(lp)
    lp.end_program(M, "program_end")
    est = lp.period_estimates[0]
    path = tmp_path / "credits.jsonl"
    path.write_text(json.dumps(_credit(M, "p-" + M, "0.50", start=est["period_start"])) + "\n")
    lp.credits_file = str(path)
    lp.maybe_load_credits(now=1.0, force=True)
    rep = lp.rewards_reconciliation()
    assert rep["paid_usd"] == "0.50" and rep["matched"] == 1
    assert rep["label"].startswith("estimates are never paid money")
    assert rep["haircut_recommendation"] is None            # too few periods/days: keep the default


def test_ledger_and_period_estimates_survive_a_restart(tmp_path):
    lp = newloop(bankroll=1500.0)
    lp.attach_state(str(tmp_path / "s.json"))
    _period(lp)
    lp.end_program(M, "program_end")
    est = lp.period_estimates[0]
    lp.ledger.append(dict(_credit(M, "p-" + M, "0.50", start=est["period_start"]), entry_id="x1"))
    lp.save_state(force=True)
    lp2 = newloop(bankroll=1500.0)
    lp2.attach_state(str(tmp_path / "s.json"))
    assert lp2.period_estimates == lp.period_estimates and lp2.ledger == lp.ledger
    assert lp2.rewards_reconciliation()["paid_usd"] == "0.50"


def test_status_carries_the_reconciliation():
    from mm.status_page import status_payload
    lp = newloop(bankroll=1500.0)
    assert "rewards_reconciliation" in status_payload(lp.live_snapshot())


def test_go_no_go_uses_the_measured_haircut_only_when_enough_data():
    lp = newloop(bankroll=1500.0)
    assert lp.series_gate_report()["go_no_go"]["verdict"]["criteria"]["reward_haircut"] == 0.5
    for i in range(12):                       # 12 matched periods over 4 distinct days, paid 70% of estimate
        day = f"2026-10-0{1 + i % 4}"
        lp.period_estimates.append(_est(f"M{i}", f"p{i}", "1.00", start=day))
        lp.ledger.append(dict(_credit(f"M{i}", f"p{i}", "0.70", start=day), entry_id=f"e{i}"))
    gn = lp.series_gate_report()["go_no_go"]
    assert gn["verdict"]["criteria"]["reward_haircut"] == pytest.approx(0.3)
    assert gn["haircut_source"] == "measured paid/estimated ratio"
    assert lp.rewards_reconciliation()["haircut_recommendation"] == pytest.approx(0.3)


def test_finish_matches_credits_by_the_real_program_id():
    """finish() paired estimates keyed (market, market) with credits keyed by the
    real program id, so real credits never matched."""
    lp = newloop(bankroll=1500.0)
    _period(lp)
    lp.on_frame({"kind": "credit", "entry_kind": "liquidity_reward", "source": "kalshi_statement",
                 "market": M, "program_id": "p-" + M, "amount_usd": "0.10"})
    report = lp.finish()
    assert len(report["reconcile"]["matches"]) == 1 and report["reconcile"]["rejected"] == 0
