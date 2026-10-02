#!/usr/bin/env python3
"""Fit the weather fair-value model's per-station bias and inflation.

Read-only on the engine: reads the settled calibration samples the engine
appends to LIP_FV_CALIB_SAMPLES_FILE (default
/var/lib/lip-maker/fv_calib_samples.jsonl; mm/unattended/fv_calib.py, one
JSON line per settled market and lead bucket: station, event, range [lo, hi],
outcome y, and the model's member-max summary ``ens`` = mean, sd, n,
observation floor) and writes a parameters file for LIP_FV_WX_PARAMS_FILE
(mm/unattended/fv_weather.load_params) only when asked (--out).

Model fitted (per station, maximum likelihood, grid search):

    CLI max ~ N(mean + bias_f, (inflation x sd)^2 + kernel_sd_f^2),
    whole-degree rounding and the observation floor exactly as
    fv_weather.bucket_prob, P(YES) clipped to [0.01, 0.99];
    log-likelihood = sum over samples of y ln p + (1 - y) ln(1 - p).

This treats the member maxes as normal with the recorded mean and sd (the
engine keeps the summary, not every member), keeps kernel_sd_f fixed
(--kernel-sd, default the engine default 1.75 F), and counts every bucket of
an event as a separate Bernoulli term (a pseudo-likelihood: buckets of one
city-day are correlated; the event count is what --min-events gates).
Samples taken after the settlement window ended (``ens.after_window``) carry
no forecast information and are skipped.

Refuses to fit a station with fewer than --min-events (30) distinct settled
events (city-days); exits 2 without writing when no station qualifies.
Prints, per station: samples, events, the fitted bias / inflation, and the
mean log loss and Brier score at the fit, at the unfitted defaults (bias 0,
inflation 1.4) and of the values the engine actually used, plus the book's
Brier on samples that had a mid. In-sample numbers: judge the fit on
fv_calibration going forward.

    python3 tools/fit_fv_weather.py                      # report only
    python3 tools/fit_fv_weather.py --out /etc/lip-maker/fv_wx_params.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mm.unattended import fv_weather as W  # noqa: E402

DEFAULT_SAMPLES = "/var/lib/lip-maker/fv_calib_samples.jsonl"
P_MIN, P_MAX = 0.01, 0.99


def _clip(p: float) -> float:
    return min(P_MAX, max(P_MIN, float(p)))


def read_samples(paths) -> tuple[list, list]:
    """Usable samples (deduplicated on market + lead bucket, last wins) and
    error strings. A sample needs station, y in {0, 1}, a 2-item range and
    ens.mean / ens.sd; after-window samples are dropped."""
    rows, errors = {}, []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"{path}: {e}")
            continue
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ens = r.get("ens") or {}
                rng = r.get("range")
                y = int(r["y"])
                if y not in (0, 1) or not isinstance(rng, list) or len(rng) != 2:
                    raise ValueError("bad y/range")
                if ens.get("after_window"):
                    continue
                s = {"station": str(r["station"]).upper(), "event": str(r.get("event") or ""),
                     "market": str(r.get("market") or ""), "lead_bucket": str(r.get("lead_bucket") or ""),
                     "y": y, "lo": None if rng[0] is None else int(rng[0]),
                     "hi": None if rng[1] is None else int(rng[1]),
                     "mean": float(ens["mean"]), "sd": max(0.0, float(ens["sd"])),
                     "floor": None if ens.get("floor_f") is None else int(ens["floor_f"]),
                     "slip": float(ens.get("slip") or 0.0),
                     "fv": None if r.get("fv") is None else float(r["fv"]),
                     "mid": None if r.get("mid") is None else float(r["mid"])}
                if not (math.isfinite(s["mean"]) and math.isfinite(s["sd"])):
                    raise ValueError("non-finite ens")
            except (KeyError, TypeError, ValueError) as e:
                errors.append(f"{path}:{i}: {type(e).__name__}: {e}")
                continue
            rows[(s["market"], s["lead_bucket"])] = s
    return list(rows.values()), errors


def prob(s: dict, bias: float, inflation: float, kernel_sd: float) -> float:
    sd = math.sqrt((inflation * s["sd"]) ** 2 + kernel_sd ** 2)
    return _clip(W.bucket_prob([s["mean"]], s["lo"], s["hi"], sd=sd, bias=bias,
                               floor_f=s["floor"], floor_slip=s["slip"]))


def loglik(samples: list, bias: float, inflation: float, kernel_sd: float) -> float:
    tot = 0.0
    for s in samples:
        p = prob(s, bias, inflation, kernel_sd)
        tot += math.log(p) if s["y"] else math.log(1.0 - p)
    return tot


def _grid(lo: float, hi: float, step: float) -> list:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 6) for i in range(n + 1)]


def fit_station(samples: list, kernel_sd: float, *, bias_range=(-5.0, 5.0),
                infl_range=(0.5, 3.0)) -> dict:
    """Grid search (coarse, then fine around the best) for max likelihood."""
    best = None
    for b in _grid(bias_range[0], bias_range[1], 0.25):
        for k in _grid(infl_range[0], infl_range[1], 0.1):
            ll = loglik(samples, b, k, kernel_sd)
            if best is None or ll > best[0]:
                best = (ll, b, k)
    _ll, b0, k0 = best
    for b in _grid(max(bias_range[0], b0 - 0.25), min(bias_range[1], b0 + 0.25), 0.05):
        for k in _grid(max(infl_range[0], k0 - 0.1), min(infl_range[1], k0 + 0.1), 0.025):
            ll = loglik(samples, b, k, kernel_sd)
            if ll > best[0]:
                best = (ll, b, k)
    ll, b, k = best
    return {"bias_f": round(b, 3), "inflation": round(k, 3), "loglik": ll,
            "at_bound": b in bias_range or k in infl_range}


def scores(samples: list, probs: list) -> dict:
    n = len(samples)
    if not n:
        return {"logloss": None, "brier": None}
    ll = -sum(math.log(p) if s["y"] else math.log(1.0 - p) for s, p in zip(samples, probs)) / n
    br = sum((p - s["y"]) ** 2 for s, p in zip(samples, probs)) / n
    return {"logloss": round(ll, 5), "brier": round(br, 5)}


def run(samples: list, *, kernel_sd: float, min_events: int) -> dict:
    by_station: dict[str, list] = {}
    for s in samples:
        by_station.setdefault(s["station"], []).append(s)
    out = {"kernel_sd_f": kernel_sd, "min_events": min_events, "stations": {}, "params": {}}
    for st, rows in sorted(by_station.items()):
        events = {s["event"] for s in rows}
        info = {"samples": len(rows), "events": len(events)}
        dflt = W.DEFAULT_PARAMS
        info["defaults"] = scores(rows, [prob(s, dflt["bias_f"], dflt["inflation"], kernel_sd) for s in rows])
        used = [s for s in rows if s["fv"] is not None]
        info["engine_values"] = dict(scores(used, [_clip(s["fv"] / 100.0) for s in used]), n=len(used))
        paired = [s for s in rows if s["mid"] is not None]
        info["book"] = dict(scores(paired, [_clip(s["mid"] / 100.0) for s in paired]), n=len(paired))
        if len(events) < min_events:
            info["status"] = f"skipped: {len(events)} settled events < {min_events}"
            out["stations"][st] = info
            continue
        fit = fit_station(rows, kernel_sd)
        info["fit"] = dict(scores(rows, [prob(s, fit["bias_f"], fit["inflation"], kernel_sd) for s in rows]),
                           bias_f=fit["bias_f"], inflation=fit["inflation"], at_grid_bound=fit["at_bound"])
        info["status"] = "fitted"
        out["stations"][st] = info
        out["params"][st] = {"bias_f": fit["bias_f"], "inflation": fit["inflation"], "kernel_sd_f": kernel_sd}
    return out


def render(rep: dict) -> str:
    lines = [f"weather FV fit (kernel sd {rep['kernel_sd_f']} F fixed; min {rep['min_events']} events/station; "
             "in-sample)"]
    for st, i in rep["stations"].items():
        lines.append(f"{st}: samples={i['samples']} events={i['events']} -> {i['status']}")
        if "fit" in i:
            f = i["fit"]
            lines.append(f"  fit      bias_f={f['bias_f']:+.2f} inflation={f['inflation']:.3f} "
                         f"logloss={f['logloss']} brier={f['brier']}"
                         + ("  (AT GRID BOUND: check the data)" if f["at_grid_bound"] else ""))
        lines.append(f"  defaults logloss={i['defaults']['logloss']} brier={i['defaults']['brier']}")
        lines.append(f"  engine   logloss={i['engine_values']['logloss']} brier={i['engine_values']['brier']} "
                     f"(n={i['engine_values']['n']})")
        lines.append(f"  book     logloss={i['book']['logloss']} brier={i['book']['brier']} (n={i['book']['n']})")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fit per-station bias/inflation of the weather FV model.")
    ap.add_argument("--samples", action="append", default=None,
                    help=f"settled samples JSONL (repeatable; default LIP_FV_CALIB_SAMPLES_FILE or {DEFAULT_SAMPLES})")
    ap.add_argument("--out", default=None, help="write the params JSON here (for LIP_FV_WX_PARAMS_FILE)")
    ap.add_argument("--min-events", type=int, default=30,
                    help="distinct settled events (city-days) a station needs to be fitted (default 30)")
    ap.add_argument("--kernel-sd", type=float, default=W.DEFAULT_KERNEL_SD_F)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.kernel_sd > 0:
        ap.error("--kernel-sd must be > 0")
    paths = a.samples or [os.environ.get("LIP_FV_CALIB_SAMPLES_FILE") or DEFAULT_SAMPLES]
    samples, errors = read_samples(paths)
    rep = run(samples, kernel_sd=a.kernel_sd, min_events=max(1, a.min_events))
    rep["inputs"] = {"files": paths, "errors": errors[:20], "errors_n": len(errors), "samples": len(samples)}
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    for e in errors[:5]:
        print(f"input: {e}", file=sys.stderr)
    if not rep["params"]:
        print("no station has enough settled events: nothing written", file=sys.stderr)
        return 2
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(rep["params"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        W.load_params(str(tmp))            # what the engine will load must load
        os.replace(tmp, out)
        print(f"wrote {out} ({', '.join(sorted(rep['params']))}); set LIP_FV_WX_PARAMS_FILE={out} and restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
