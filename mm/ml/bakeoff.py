"""Fill-toxicity model bake-off: purged, embargoed walk-forward (offline, paper).

    /opt/lip-ml-venv/bin/python -m mm.ml.bakeoff [--out-dir /var/lib/lip-maker/ml]
        [--population fills|trade_proxy|all|both] [--models prior,rule_fastmove,logreg,lgbm,catboost[,tabpfn]]
        [--embargo-s 1800] [--fold-unit auto|day|block4h] [--json PATH]

Populations (never mixed):
  fills        live + replay paper fills (non-synthetic; --include-synthetic adds
               PM US inferred fills with a synthetic flag feature)
  trade_proxy  recorded public prints seen from the maker side: NOT our fills,
               a proxy to exercise the harness while fills are scarce.

Walk-forward: rows are grouped by UTC day (``auto`` falls back to 4-hour
blocks when there are < 3 days, and says so). For every test block k the
models train on blocks < k, minus a purge/embargo: a train row is dropped
unless cutoff + label horizon (5 m) + embargo < start of the test block, so no
training label overlaps the test period. Metrics come from the pooled
out-of-sample predictions: AUC, log-loss, Brier, and the economic value of a
pull policy.

Economic value (per fill, our size = fill count; trade_proxy: min(print, 10)):
  net_fill_usd = size x 5 m markout (side mid at t+5m - price) / 100
                 - Kalshi maker fee (series fee_type) + PM US maker rebate
  pull policy: skip the fill when the model's score >= tau. tau = the train
  fold's in-sample score quantile for a pull rate of 10 % / 20 % (no test
  labels or test scores are used). value = -sum(net_fill_usd of skipped
  fills); breakeven_pull_cost_usd = value / pulls is the reward we may give up
  per pulled quote before the policy stops paying. ``rule_fastmove`` pulls on
  the engine's own fast-move flag (opposite best >= 5c toward us since the
  quote / over 30 s; the live engine already pulls on it).
  Widening (re-quote 1-2c back) needs a queue-aware replay and is NOT modelled
  here (see docs); pulling is the bound.

Ensemble (mean probability of the single learners) is reported as the winner
only if it beats the best single model out of sample on both log-loss and
value; that comparison reuses the same OOS data (optimistic), and says so.
All results are labelled UNDERPOWERED unless the readiness gate is READY.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

from mm.ml import dataset as D

LABEL_H_S = 300.0
PULL_RATES = (0.10, 0.20)
PROXY_SIZE = 10.0
TOP_CATS = 15
EXCLUDE_NUM = ("count",)            # the taker's size is not known before the fill
SINGLE = ("logreg", "lgbm", "catboost", "tabpfn")
# TabPFN (2.0.9, open v2 weights; tabpfn>=2.5 needs a Prior Labs licence token)
# measured on APEX: 1000 train x 2000 test rows, 1 CPU thread > 13 min without
# finishing. Opt-in only (--models ...,tabpfn); not in the nightly default.
TABPFN_MAX_TRAIN = 500
TABPFN_MAX_TEST = 1000


# ---------------------------------------------------------------- data
def select(rows, population, include_synthetic=False):
    seen, out = set(), []
    for r in rows:
        if r.get("toxic") is None or r.get("markout_c_5m") is None:
            continue
        src = r.get("source")
        if population == "fills" and src not in ("live", "replay"):
            continue
        if population == "trade_proxy" and src != "trade_proxy":
            continue
        if population == "all" and src == "quote_bg":
            continue
        if r.get("synthetic") and not include_synthetic:
            continue
        key = (src, r["market"], r["side"], round(float(r["cutoff"]), 3), r.get("price_c"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=lambda r: float(r["cutoff"]))
    return out


def fee_adj_usd(r, size):
    """Maker fee (negative) or PM US rebate (positive) for ``size`` contracts."""
    try:
        from mm.accounting import kalshi_fee_usd, pm_us_maker_rebate_usd
        px = int(round(float(r["price_c"])))
        if r.get("venue") == "pmus":
            return float(pm_us_maker_rebate_usd(px, Decimal(str(size))))
        ft = r.get("fee_type") or "quadratic_with_maker_fees"
        try:
            return -float(kalshi_fee_usd(px, Decimal(str(size)), fee_type=ft, is_taker=False))
        except Exception:
            return -float(kalshi_fee_usd(px, Decimal(str(size)), fee_type="quadratic_with_maker_fees",
                                         is_taker=False))
    except Exception:
        return 0.0


def net_fill_usd(r):
    size = float(r.get("count") or 0.0)
    if r.get("source") == "trade_proxy":
        size = min(size, PROXY_SIZE)
    return size * float(r["markout_c_5m"]) / 100.0 + fee_adj_usd(r, size)


def fast_flag(r):
    v = r.get("fast_move_quote")
    if v is None:
        v = r.get("fast_move_30s")
    return 1.0 if v else 0.0


class Encoder:
    """Numeric features + one-hots (venue, side, synthetic, top categories)."""

    def fit(self, rows):
        self.num = [f for f in D.FEATURES_NUM if f not in EXCLUDE_NUM]
        cats = Counter(str(r.get("category")) for r in rows)
        self.cats = [c for c, _n in cats.most_common(TOP_CATS)]
        self.names = self.num + ["venue_pmus", "side_yes", "synthetic"] + [f"cat={c}" for c in self.cats]
        return self

    def transform(self, rows):
        import numpy as np
        X = np.full((len(rows), len(self.names)), np.nan, dtype=float)
        n = len(self.num)
        for i, r in enumerate(rows):
            for j, f in enumerate(self.num):
                v = r.get(f)
                if v is not None:
                    X[i, j] = float(v)
            X[i, n] = 1.0 if r.get("venue") == "pmus" else 0.0
            X[i, n + 1] = 1.0 if r.get("side") == "yes" else 0.0
            X[i, n + 2] = 1.0 if r.get("synthetic") else 0.0
            c = str(r.get("category"))
            for k, cc in enumerate(self.cats):
                X[i, n + 3 + k] = 1.0 if c == cc else 0.0
        return X


# ---------------------------------------------------------------- folds
def block_of(ts, unit):
    if unit == "day":
        return int(ts // 86400)
    if unit == "block4h":
        return int(ts // 14400)
    raise ValueError(unit)


def walk_forward(rows, unit, embargo_s, label_h_s=LABEL_H_S, min_train=30):
    """Yield (block, train_idx, test_idx, test_start). Train rows must have
    cutoff + label horizon + embargo < test_start (purge + embargo)."""
    blocks = sorted({block_of(float(r["cutoff"]), unit) for r in rows})
    size = 86400 if unit == "day" else 14400
    for b in blocks[1:]:
        start = b * size
        test = [i for i, r in enumerate(rows) if block_of(float(r["cutoff"]), unit) == b]
        train = [i for i, r in enumerate(rows)
                 if float(r["cutoff"]) + label_h_s + embargo_s < start]
        if len(train) >= min_train and test:
            yield b, train, test, start


def choose_unit(rows, unit):
    if unit != "auto":
        return unit, None
    days = {block_of(float(r["cutoff"]), "day") for r in rows}
    if len(days) >= 3:
        return "day", None
    return "block4h", f"only {len(days)} UTC day(s) of data: folds are 4-hour blocks, not days"


# ---------------------------------------------------------------- models
def make_model(name):
    if name == "logreg":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
                             StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
    if name == "lgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=20,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.8, n_jobs=1, verbose=-1)
    if name == "catboost":
        from catboost import CatBoostClassifier
        return CatBoostClassifier(iterations=300, depth=4, learning_rate=0.05, thread_count=1, verbose=0,
                                  allow_writing_files=False)
    if name == "tabpfn":
        from tabpfn import TabPFNClassifier
        try:
            return TabPFNClassifier(device="cpu", n_estimators=1)
        except TypeError:
            return TabPFNClassifier(device="cpu")
    raise ValueError(name)


def fit_predict(name, Xtr, ytr, Xte, rows_tr, rows_te, seed=0):
    """Returns (p_train, p_test) for one fold."""
    import numpy as np
    if name == "prior":
        p = float(np.mean(ytr))
        return np.full(len(ytr), p), np.full(len(Xte), p)
    if name == "rule_fastmove":
        ftr = np.array([fast_flag(r) for r in rows_tr])
        fte = np.array([fast_flag(r) for r in rows_te])
        base = float(np.mean(ytr))
        p1 = float(np.mean(ytr[ftr == 1])) if (ftr == 1).sum() >= 5 else max(base, 0.99)
        p0 = float(np.mean(ytr[ftr == 0])) if (ftr == 0).sum() else base
        # the rule's score is the flag; probabilities are the train rates per bin
        return np.where(ftr == 1, p1, p0), np.where(fte == 1, p1, p0)
    if name == "tabpfn":
        rng = np.random.default_rng(seed)
        idx = np.arange(len(ytr))
        if len(idx) > TABPFN_MAX_TRAIN:
            idx = np.sort(rng.choice(idx, TABPFN_MAX_TRAIN, replace=False))
        m = make_model(name)
        m.fit(Xtr[idx], ytr[idx])
        pte = np.concatenate([m.predict_proba(Xte[i:i + TABPFN_MAX_TEST])[:, 1]
                              for i in range(0, len(Xte), TABPFN_MAX_TEST)])
        ptr = np.concatenate([m.predict_proba(Xtr[i:i + TABPFN_MAX_TEST])[:, 1]
                              for i in range(0, len(Xtr), TABPFN_MAX_TEST)])
        return ptr, pte
    m = make_model(name)
    m.fit(Xtr, ytr)
    return m.predict_proba(Xtr)[:, 1], m.predict_proba(Xte)[:, 1]


# ---------------------------------------------------------------- metrics
def auc(y, p):
    pos = [pp for yy, pp in zip(y, p) if yy == 1]
    neg = [pp for yy, pp in zip(y, p) if yy == 0]
    if not pos or not neg:
        return None
    # rank-based (ties averaged)
    allp = sorted((pp, i) for i, pp in enumerate(list(pos) + list(neg)))
    ranks = [0.0] * len(allp)
    i = 0
    while i < len(allp):
        j = i
        while j + 1 < len(allp) and allp[j + 1][0] == allp[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[allp[k][1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    rp = sum(ranks[:len(pos)])
    return (rp - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))


def logloss(y, p):
    eps = 1e-6
    return -sum(yy * math.log(min(max(pp, eps), 1 - eps)) + (1 - yy) * math.log(1 - min(max(pp, eps), 1 - eps))
                for yy, pp in zip(y, p)) / max(1, len(y))


def brier(y, p):
    return sum((pp - yy) ** 2 for yy, pp in zip(y, p)) / max(1, len(y))


def pull_value(net, pulled):
    """$ change from skipping the pulled fills (positive = policy helps)."""
    n = sum(1 for x in pulled if x)
    v = -sum(x for x, k in zip(net, pulled) if k)
    return {"pulls": n, "value_usd": round(v, 4),
            "breakeven_pull_cost_usd": round(v / n, 5) if n else None}


# ---------------------------------------------------------------- runner
def run(rows, *, models, embargo_s=1800.0, unit="auto", seed=0, log=print) -> dict:
    import numpy as np
    unit, unit_note = choose_unit(rows, unit)
    y_all = np.array([int(r["toxic"]) for r in rows])
    net_all = [net_fill_usd(r) for r in rows]
    folds = []
    oos = {m: {} for m in list(models) + ["ensemble"]}
    tau_pulls = {m: {q: {} for q in PULL_RATES} for m in list(models) + ["ensemble"]}
    skipped = Counter()
    errors = {}
    timing = Counter()
    for b, tr, te, start in walk_forward(rows, unit, embargo_s):
        ytr = y_all[tr]
        if ytr.sum() < 5 or (len(ytr) - ytr.sum()) < 5:
            skipped["train_one_class_or_<5_pos"] += 1
            continue
        enc = Encoder().fit([rows[i] for i in tr])
        Xtr = enc.transform([rows[i] for i in tr])
        Xte = enc.transform([rows[i] for i in te])
        preds_tr, preds_te = {}, {}
        for m in models:
            if m in errors:
                continue
            t0 = time.time()
            try:
                ptr, pte = fit_predict(m, Xtr, ytr, Xte, [rows[i] for i in tr], [rows[i] for i in te], seed)
                preds_tr[m], preds_te[m] = ptr, pte
            except Exception as e:     # missing package / weights / CPU budget
                errors[m] = f"{type(e).__name__}: {str(e)[:300]}"
                log(f"model {m} disabled: {errors[m]}")
            timing[m] += time.time() - t0
        singles = [m for m in SINGLE if m in preds_te]
        if len(singles) >= 2:
            preds_tr["ensemble"] = np.mean([preds_tr[m] for m in singles], axis=0)
            preds_te["ensemble"] = np.mean([preds_te[m] for m in singles], axis=0)
        for m, pte in preds_te.items():
            for i, p in zip(te, pte):
                oos[m][i] = float(p)
            for q in PULL_RATES:
                if m == "rule_fastmove":
                    pulled = [fast_flag(rows[i]) == 1.0 for i in te]
                else:
                    tau = float(np.quantile(preds_tr[m], 1.0 - q))
                    pulled = [p >= tau and p > float(np.min(preds_tr[m])) for p in pte]
                for i, k in zip(te, pulled):
                    tau_pulls[m][q][i] = bool(k)
        folds.append({"block": int(b), "test_start_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)),
                      "train_n": len(tr), "test_n": len(te), "test_toxic": int(y_all[te].sum())})
    res = {}
    for m, d in oos.items():
        if not d:
            continue
        idx = sorted(d)
        y = [int(y_all[i]) for i in idx]
        p = [d[i] for i in idx]
        net = [net_all[i] for i in idx]
        out = {"oos_n": len(idx), "oos_toxic": sum(y), "auc": _r(auc(y, p)), "logloss": _r(logloss(y, p)),
               "brier": _r(brier(y, p)), "baseline_net_usd": round(sum(net), 4), "pull": {}}
        for q in PULL_RATES:
            pulls = tau_pulls[m][q]
            out["pull"][f"{int(q * 100)}pct"] = pull_value(net, [pulls.get(i, False) for i in idx])
            if m == "rule_fastmove":
                out["pull"] = {"flag": out["pull"][f"{int(q * 100)}pct"]}
                break
        res[m] = out
    winner = None
    singles_done = {m: res[m] for m in res if m in SINGLE}
    if singles_done:
        best = min(singles_done, key=lambda m: res[m]["logloss"])
        winner = best
        if "ensemble" in res:
            e, s = res["ensemble"], res[best]
            ev = e["pull"].get("10pct", {}).get("value_usd") or 0
            sv = s["pull"].get("10pct", {}).get("value_usd") or 0
            if e["logloss"] < s["logloss"] and ev > sv:
                winner = "ensemble"
    return {"fold_unit": unit, "fold_note": unit_note, "embargo_s": embargo_s, "label_horizon_s": LABEL_H_S,
            "n_rows": len(rows), "n_toxic": int(y_all.sum()) if len(rows) else 0, "folds": folds,
            "folds_skipped": dict(skipped), "models": res, "errors": errors,
            "fit_seconds": {k: round(v, 1) for k, v in timing.items()},
            "winner_by_oos_logloss": winner,
            "winner_note": "ensemble only wins if it beats the best single model on OOS log-loss AND 10% pull "
                           "value; same OOS data used for the choice (optimistic)"}


def _r(x):
    return None if x is None else round(float(x), 5)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mm.ml.bakeoff")
    ap.add_argument("--out-dir", default=D.OUT_DIR)
    ap.add_argument("--population", default="both", choices=("fills", "trade_proxy", "all", "both"))
    ap.add_argument("--models", default="prior,rule_fastmove,logreg,lgbm,catboost",
                    help="add ',tabpfn' to benchmark TabPFN (slow on CPU)")
    ap.add_argument("--embargo-s", type=float, default=1800.0)
    ap.add_argument("--fold-unit", default="auto", choices=("auto", "day", "block4h"))
    ap.add_argument("--include-synthetic", action="store_true")
    ap.add_argument("--max-rows", type=int, default=60000, help="newest N rows per population (CPU bound)")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    rows = D.load_samples(a.out_dir)
    lj = D.read_jsonl(Path(a.out_dir) / "fills_live.jsonl")
    ready = D.readiness(rows, live_journal={"n": len(lj)})
    pops = ("fills", "trade_proxy") if a.population == "both" else (a.population,)
    report = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "paper_only": True,
              "readiness": ready["status"], "readiness_reason": ready["reason"], "populations": {}}
    for pop in pops:
        sel = select(rows, pop, a.include_synthetic)[-a.max_rows:]
        power = "PROXY (public prints, not our fills)" if pop == "trade_proxy" else \
            ("OK" if ready["status"] == "READY" else "UNDERPOWERED")
        if len(sel) < 60:
            report["populations"][pop] = {"power": power, "n_rows": len(sel),
                                          "result": "insufficient rows for a walk-forward (< 60 labeled)"}
            continue
        r = run(sel, models=[m for m in a.models.split(",") if m], embargo_s=a.embargo_s, unit=a.fold_unit)
        r["power"] = power
        report["populations"][pop] = r
    txt = json.dumps(report, indent=1)
    path = a.json or str(Path(a.out_dir) / "bakeoff.json")
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(txt)
    os.replace(tmp, path)
    for pop, r in report["populations"].items():
        print(f"== {pop}: power={r['power']} rows={r.get('n_rows')} toxic={r.get('n_toxic')} "
              f"unit={r.get('fold_unit')} folds={len(r.get('folds') or [])} {r.get('fold_note') or ''}")
        if "models" not in r:
            print("   ", r.get("result"))
            continue
        for m, v in r["models"].items():
            pulls = " ".join(f"{k}:{x['pulls']}p ${x['value_usd']:+.2f}" for k, x in v["pull"].items())
            print(f"   {m:14s} n={v['oos_n']:6d} auc={v['auc']} ll={v['logloss']} brier={v['brier']} "
                  f"base_net=${v['baseline_net_usd']:+.2f} {pulls}")
        if r.get("errors"):
            print("   disabled:", r["errors"])
        print("   winner (OOS log-loss):", r.get("winner_by_oos_logloss"))
    print(f"readiness: {report['readiness']} -> results are "
          f"{'usable' if report['readiness'] == 'READY' else 'UNDERPOWERED / proxy only'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
