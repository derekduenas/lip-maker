"""Argus backtest harness — replay settled markets through a brain.

For each settled market:
  1. Reconstruct features at T-1 day before settlement (no lookahead).
     For Music: pull the artist's per-track weekly history; pick the
     latest week with week_start <= settle_date - 1 day.
  2. Backfill historical_no1_rate_12mo via Step 0 (anchor = first day of
     resolution month; window strictly excludes resolution month).
  3. Train logistic SGD on N-1 markets; predict the held-out 1.
     LOO when N < 100; 80/20 chronological split when N >= 100.
  4. Score: Brier + BSS + reliability bins + per-feature importance.

Score gate (per Phase 3):
    BSS >= BSS_LIVE_GATE (0.15)  AND  n_test >= MIN_BACKTEST_N (20)

NOT YET: Platt calibration overlay (defer to v2 if BSS marginal).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from argus.brains.base import DomainBrain
from argus.brains.music import (
    MusicBrain, MusicFeatures, MusicModel, DEFAULT_WEIGHTS,
    parse_ticker, chart_rows_crediting, days_remaining, _sigmoid,
)
from argus.data.spotify_charts import SpotifyChartsClient, ChartEntry
from argus.scoring.brier import (
    brier_score, reliability_bins, naive_baseline_brier, brier_skill_score,
    BrierResult,
)
from argus.backtest.historical_no1 import compute_historical_no1_rate

_log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────
@dataclass
class TrainExample:
    ticker:         str
    artist:         str
    settle_date:    dt.date
    resolution_year:  int
    resolution_month: int
    outcome:        int                 # 1 if YES, 0 if NO
    features:       MusicFeatures
    feature_vec:    dict                # snapshot of model_vec at reconstruction time


@dataclass
class BacktestResult:
    brain_id:       str
    n_total:        int
    n_train:        int                 # for LOO this equals N-1 per fold; reported as N-1
    n_test:         int                 # equals n_total under LOO
    base_rate:      float
    train_brier:    Optional[BrierResult]
    test_brier:     Optional[BrierResult]
    test_bss:       float
    naive_brier:    float
    calibration:    list[dict] = field(default_factory=list)
    feature_importance: dict[str, float] = field(default_factory=dict)
    notes:          list[str] = field(default_factory=list)
    method:         str = ""
    full_weights:   dict[str, float] = field(default_factory=dict)    # full-corpus retrain

    def passes_gate(self, bss_gate: float, min_n: int) -> bool:
        return self.test_bss >= bss_gate and self.n_test >= min_n

    def explain(self) -> dict:
        return {
            "brain":               self.brain_id,
            "n_total":             self.n_total,
            "n_train_per_fold":    self.n_train,
            "n_test":              self.n_test,
            "method":              self.method,
            "base_rate":           round(self.base_rate, 4),
            "naive_brier":         round(self.naive_brier, 4),
            "test_brier":          round(self.test_brier.brier, 4) if self.test_brier else None,
            "test_bss":            round(self.test_bss, 4),
            "passes_gate":         self.passes_gate(0.15, 20),
            "feature_importance":  self.feature_importance,
            "calibration":         self.calibration,
            "notes":               self.notes,
        }


# ── Feature reconstruction at T-1 ─────────────────────────────────────────
def reconstruct_features_at_settle(
    artist_name:      str,
    resolution_year:  int,
    resolution_month: int,
    settle_date:      dt.date,
    historical_no1_rate: float,
    client:           SpotifyChartsClient,
) -> Optional[MusicFeatures]:
    """Reconstruct features as of settle_date - 1 day, no lookahead.

    For Music backtest we use the artist's per-track weekly history (not
    the live daily chart, which only has 'today'). Picks the latest week
    with week_start <= cutoff and uses its rank + streams.

    Limitations vs live prediction:
      - streams_today_delta: not available historically → use weekly delta / 7
      - peer_gap_norm:       not available historically → set to 0
    """
    cutoff = settle_date - dt.timedelta(days=1)

    # Find any track for this artist that has #1-or-near history; iterate
    # the artist's top tracks and pick the BEST rank attained in any week
    # at-or-before cutoff. (Mirrors live extract_features semantics.)
    from argus.backtest.historical_no1 import find_artist_id, get_artist_top_tracks
    artist_id = find_artist_id(artist_name)
    if not artist_id:
        return None

    tracks = get_artist_top_tracks(artist_id)
    best_rank = 999
    best_streams = 0
    best_streams_7d_delta = 0
    for tid, _name in tracks:
        try:
            th = client.get_track_history(tid)
        except Exception:
            continue
        # Weeks at or before cutoff, Global only
        gw = sorted(
            [w for w in th.weeks
             if w.country == "GLOBAL" and w.week_start <= cutoff],
            key=lambda w: w.week_start,
        )
        if not gw:
            continue
        latest = gw[-1]
        if latest.rank < best_rank:
            best_rank = latest.rank
            best_streams = latest.streams
            # week-over-week delta (proxy for momentum)
            prev_streams = gw[-2].streams if len(gw) >= 2 else latest.streams
            best_streams_7d_delta = latest.streams - prev_streams

    days_left = days_remaining(resolution_year, resolution_month, cutoff)

    return MusicFeatures(
        # raw
        current_top_track_rank=best_rank,
        current_top_track_streams=best_streams,
        streams_today_delta=best_streams_7d_delta // 7,    # weekly→daily proxy
        streams_7d_delta=best_streams_7d_delta,
        peer_streams_gap=0,                                # unknown historically
        days_remaining_in_month=days_left,
        # normalized
        top_rank_lift=max(0.0, 11.0 - best_rank) if best_rank <= 200 else 0.0,
        streams_velocity_norm_today=(best_streams_7d_delta / 7.0) / 1_000_000.0,
        days_factor=days_left / 31.0,
        historical_no1_rate_12mo=historical_no1_rate,
        streams_7d_velocity_sign=(1 if best_streams_7d_delta > 0
                                  else (-1 if best_streams_7d_delta < 0 else 0)),
        peer_gap_norm=0.0,
    )


# ── Build training set from settled markets ───────────────────────────────
FEATURE_KEYS = [
    "top_rank_lift",
    "streams_velocity_norm_today",
    "days_factor",
    "historical_no1_rate_12mo",
    "streams_7d_velocity_sign",
    "peer_gap_norm",
]


def build_examples(
    settled_markets:  list[dict],
    client:           SpotifyChartsClient,
    base_rate_for_fallback: float,
) -> list[TrainExample]:
    """Reconstruct features + outcome for each settled market."""
    out: list[TrainExample] = []
    n = len(settled_markets)
    for i, m in enumerate(settled_markets):
        ticker = m.get("ticker", "")
        sub    = m.get("yes_sub_title", "")
        meta = parse_ticker(ticker, sub)
        if not meta or not meta.artist_name:
            continue
        # Settlement timestamp → date
        settle_ts = m.get("settlement_ts") or m.get("close_time", "")
        try:
            settle_dt = dt.datetime.fromisoformat(settle_ts.replace("Z", "+00:00"))
            settle_date = settle_dt.date()
        except Exception:
            settle_date = meta.settle_date

        # Step 0: backfill historical_no1
        anchor = dt.date(meta.resolution_year, meta.resolution_month, 1)
        h = compute_historical_no1_rate(
            meta.artist_name, anchor, client,
            base_rate_fallback=base_rate_for_fallback,
        )

        feats = reconstruct_features_at_settle(
            meta.artist_name, meta.resolution_year, meta.resolution_month,
            settle_date, h.rate, client,
        )
        if feats is None:
            _log.debug(f"skip {ticker}: feature reconstruction failed")
            continue

        outcome = 1 if (m.get("result") or "").lower() == "yes" else 0
        out.append(TrainExample(
            ticker=ticker, artist=meta.artist_name,
            settle_date=settle_date,
            resolution_year=meta.resolution_year,
            resolution_month=meta.resolution_month,
            outcome=outcome, features=feats,
            feature_vec=feats.model_vec(),
        ))
        if i % 10 == 0:
            _log.info(f"built {i+1}/{n} examples")
    return out


# ── Logistic SGD ──────────────────────────────────────────────────────────
def _train_logistic(
    examples:    list[TrainExample],
    epochs:      int = 400,
    lr:          float = 0.05,
    l2:          float = 0.01,
) -> dict[str, float]:
    """Tiny pure-Python logistic regression with L2 reg."""
    weights = dict(DEFAULT_WEIGHTS)
    n = len(examples)
    if n == 0:
        return weights

    keys = ["intercept"] + FEATURE_KEYS
    for ep in range(epochs):
        grads = {k: 0.0 for k in keys}
        for ex in examples:
            v = ex.feature_vec
            z = weights["intercept"] + sum(weights[k] * v[k] for k in FEATURE_KEYS)
            z = max(-30.0, min(30.0, z))
            p = _sigmoid(z)
            err = p - ex.outcome
            grads["intercept"] += err
            for k in FEATURE_KEYS:
                grads[k] += err * v[k]
        # L2: shrink each weight (NOT intercept)
        for k in FEATURE_KEYS:
            grads[k] += l2 * weights[k]
        for k in keys:
            weights[k] -= lr * grads[k] / n
    return weights


def _predict(weights: dict, ex: TrainExample) -> float:
    v = ex.feature_vec
    z = weights["intercept"] + sum(weights[k] * v[k] for k in FEATURE_KEYS)
    z = max(-30.0, min(30.0, z))
    return _sigmoid(z)


# ── Backtest entry ────────────────────────────────────────────────────────
def run_backtest(
    settled_markets: list[dict],
    *,
    client:    Optional[SpotifyChartsClient] = None,
    bss_gate:  float = 0.15,
    min_n:     int = 20,
) -> BacktestResult:
    """End-to-end. Pulls features → backfills → LOO trains → scores."""
    client = client or SpotifyChartsClient()

    # Compute observed base rate FIRST (for fallback in Step 0)
    raw_yes = sum(1 for m in settled_markets
                  if (m.get("result") or "").lower() == "yes")
    base_rate = raw_yes / max(1, len(settled_markets))

    examples = build_examples(settled_markets, client, base_rate_for_fallback=base_rate)
    n = len(examples)
    if n == 0:
        return BacktestResult(
            brain_id="music", n_total=0, n_train=0, n_test=0, base_rate=0.0,
            train_brier=None, test_brier=None, test_bss=0.0, naive_brier=0.0,
            method="empty",
        )

    outcomes_all = [ex.outcome for ex in examples]
    base_rate_examples = sum(outcomes_all) / n

    # LOO when N<100 per Phase 4 spec
    method = "LOO" if n < 100 else "80/20-chrono"
    test_preds: list[float] = []
    test_outs:  list[int]   = []

    # Train ONE model on all to report per-feature importance + train Brier
    full_weights = _train_logistic(examples)
    train_preds = [_predict(full_weights, ex) for ex in examples]
    train_brier = brier_score(train_preds, outcomes_all)

    if method == "LOO":
        for i, held in enumerate(examples):
            train = examples[:i] + examples[i+1:]
            w = _train_logistic(train)
            p = _predict(w, held)
            test_preds.append(p)
            test_outs.append(held.outcome)
            if i % 10 == 0:
                _log.info(f"LOO {i+1}/{n}")
    else:
        examples_sorted = sorted(examples, key=lambda e: e.settle_date)
        cut = int(n * 0.8)
        train, test = examples_sorted[:cut], examples_sorted[cut:]
        w = _train_logistic(train)
        for ex in test:
            test_preds.append(_predict(w, ex))
            test_outs.append(ex.outcome)

    test_brier = brier_score(test_preds, test_outs)
    nb = naive_baseline_brier(test_outs)
    bss = brier_skill_score(test_brier.brier, test_outs)

    cal = reliability_bins(test_preds, test_outs, n_bins=10)

    # Feature importance = abs(weight) sorted; weights are interpretable
    # because features were pre-normalized to comparable scales.
    importance = {k: round(full_weights.get(k, 0.0), 4)
                  for k in ["intercept"] + FEATURE_KEYS}

    return BacktestResult(
        brain_id="music",
        n_total=n,
        n_train=(n - 1) if method == "LOO" else int(n * 0.8),
        n_test=n if method == "LOO" else (n - int(n * 0.8)),
        base_rate=base_rate_examples,
        train_brier=train_brier,
        test_brier=test_brier,
        test_bss=bss,
        naive_brier=nb,
        calibration=cal,
        feature_importance=importance,
        notes=[
            f"corpus base rate (kalshi feed)  = {base_rate:.4f}",
            f"corpus base rate (rebuilt examples) = {base_rate_examples:.4f}",
        ],
        method=method,
        full_weights=full_weights,
    )
