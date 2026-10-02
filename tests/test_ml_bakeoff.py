"""mm.ml.bakeoff: purged/embargoed walk-forward, metrics, pull economics."""
import random

import pytest

from mm.ml import bakeoff as B

DAY = 86400.0
T0 = 1_790_000_000.0 - (1_790_000_000.0 % DAY)


def rows(n_days=4, per_day=60, seed=0):
    rnd = random.Random(seed)
    out = []
    for d in range(n_days):
        for i in range(per_day):
            fm = rnd.random() < 0.3
            tox = 1 if (fm and rnd.random() < 0.8) or rnd.random() < 0.1 else 0
            out.append({"source": "replay", "market": f"M{i % 7}", "side": "yes", "price_c": 40.0,
                        "cutoff": T0 + d * DAY + i * 600.0, "count": 5.0, "venue": "kalshi",
                        "category": "X", "fee_type": "quadratic", "synthetic": False,
                        "fast_move_30s": 1.0 if fm else 0.0, "mid_move_30s": -3.0 if fm else 0.0,
                        "toxic": tox, "markout_c_5m": -6.0 if tox else 1.0})
    return out


def test_walk_forward_is_purged_and_embargoed():
    rs = rows()
    for _b, tr, te, start in B.walk_forward(rs, "day", embargo_s=1800.0):
        assert max(rs[i]["cutoff"] for i in tr) + B.LABEL_H_S + 1800.0 < start
        assert min(rs[i]["cutoff"] for i in te) >= start
        assert not set(tr) & set(te)


def test_walk_forward_never_trains_on_later_blocks():
    rs = rows()
    folds = list(B.walk_forward(rs, "day", embargo_s=0.0))
    assert len(folds) == 3
    for _b, tr, te, _s in folds:
        assert max(rs[i]["cutoff"] for i in tr) < min(rs[i]["cutoff"] for i in te)


def test_choose_unit_falls_back_and_says_so():
    unit, note = B.choose_unit(rows(n_days=2), "auto")
    assert unit == "block4h" and "4-hour" in note
    assert B.choose_unit(rows(n_days=3), "auto") == ("day", None)


def test_auc_logloss_brier():
    assert B.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert B.auc([0, 1], [0.5, 0.5]) == 0.5
    assert B.auc([1, 1], [0.1, 0.2]) is None
    assert B.logloss([1, 0], [0.5, 0.5]) == pytest.approx(0.693147, abs=1e-5)
    assert B.brier([1, 0], [1.0, 0.0]) == 0.0


def test_pull_value_sign():
    v = B.pull_value([-1.0, 0.5, -0.25], [True, False, True])
    assert v["pulls"] == 2 and v["value_usd"] == pytest.approx(1.25)
    assert v["breakeven_pull_cost_usd"] == pytest.approx(0.625)


def test_select_dedupes_and_separates_populations():
    rs = rows(1, 10)
    rs.append(dict(rs[0]))
    rs.append(dict(rs[1], source="trade_proxy"))
    rs.append(dict(rs[2], synthetic=True, cutoff=rs[2]["cutoff"] + 1))
    rs.append(dict(rs[3], toxic=None, cutoff=rs[3]["cutoff"] + 1))
    sel = B.select(rs, "fills")
    assert len(sel) == 10                       # duplicate, proxy, synthetic, unlabeled dropped
    assert len(B.select(rs, "trade_proxy")) == 1
    assert len(B.select(rs, "fills", include_synthetic=True)) == 11


def test_net_fill_includes_fees_and_proxy_size_cap():
    r = {"source": "trade_proxy", "count": 500.0, "markout_c_5m": -2.0, "price_c": 50.0, "venue": "pmus"}
    assert B.net_fill_usd(r) == pytest.approx(-0.2 + B.fee_adj_usd(r, 10.0))
    assert B.fee_adj_usd(r, 10.0) > 0              # PM US maker rebate


def test_run_end_to_end_baselines_without_ml_packages():
    pytest.importorskip("numpy")
    res = B.run(rows(), models=["prior", "rule_fastmove"], embargo_s=1800.0, unit="day", log=lambda *_: None)
    assert res["fold_unit"] == "day" and len(res["folds"]) == 3
    assert 0.3 < res["models"]["prior"]["auc"] < 0.7   # constant per fold (pooled != 0.5)
    rule = res["models"]["rule_fastmove"]
    assert rule["auc"] > 0.7 and rule["pull"]["flag"]["value_usd"] > 0


def test_run_logreg_and_trees_learn_signal():
    pytest.importorskip("sklearn")
    pytest.importorskip("lightgbm")
    res = B.run(rows(n_days=5, per_day=120), models=["prior", "logreg", "lgbm"], embargo_s=1800.0,
                unit="day", log=lambda *_: None)
    assert res["models"]["logreg"]["auc"] > 0.7
    assert res["models"]["lgbm"]["auc"] > 0.7
    assert res["models"]["logreg"]["logloss"] < res["models"]["prior"]["logloss"]
    assert "ensemble" in res["models"]


def test_missing_model_package_is_reported_not_fatal(monkeypatch):
    pytest.importorskip("numpy")

    def boom(name):
        raise ImportError("no such package")
    monkeypatch.setattr(B, "make_model", boom)
    res = B.run(rows(), models=["prior", "catboost"], embargo_s=0.0, unit="day", log=lambda *_: None)
    assert "catboost" in res["errors"] and "prior" in res["models"]
