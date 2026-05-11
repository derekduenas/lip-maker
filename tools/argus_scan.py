"""ARGUS paper-mode scanner — surface tradeable Music brain candidates.

Pipeline (paper-only — no orders):
  1. Load trained MusicModel from data/argus/music_model_v1.json
     (raises if file missing — operator must run argus_backtest --save-model
     first; never silently use heuristic priors in scan mode)
  2. Pull all currently-active KXRANKLISTSONGSPOTGLOBAL-* markets from Kalshi
  3. For each, run MusicBrain.predict() (live chart, true daily delta)
  4. Compute edge_pp = |our_p - market_p| × 100
     (market_p from yes mid: (yes_bid + yes_ask) / 2)
  5. Filter: edge_pp >= MIN_EDGE_PP AND confidence >= MIN_CONFIDENCE
  6. Persist EVERY prediction (not just candidates) to argus_candidates.db
     — needed for the live-vs-backtest velocity-feature monitor
  7. Print top 10 candidates with full feature breakdown

Velocity feature monitor:
  Backtest weight on streams_velocity_norm_today is NEGATIVE (week-over-week
  proxy artifact suspected). Live data uses true daily delta. Each prediction
  logs the velocity value so a future analysis cron can correlate live
  velocity → outcome and confirm/refute the artifact hypothesis.

Cron: deploy/argus-scan.{timer,service} → daily 23:00 UTC.

USAGE:
  python -m tools.argus_scan
  python -m tools.argus_scan --json
  python -m tools.argus_scan --min-edge 5      # custom edge gate
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.kalshi_auth import KalshiClient
from argus.brains.music import MusicBrain, MusicModel, MARKET_PREFIX, MODEL_PATH
from argus.config import (
    DATA_DIR, MIN_EDGE_PP, MIN_CONFIDENCE, ensure_dirs,
)
from argus.data.spotify_charts import SpotifyChartsClient

_log = logging.getLogger(__name__)

CANDIDATES_DB = lambda: Path(DATA_DIR) / "argus_candidates.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS argus_candidates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,             -- ISO UTC of scan
    brain_id           TEXT NOT NULL,
    model_version      TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    artist             TEXT,
    resolution_month   TEXT,                      -- YYYY-MM
    our_p              REAL NOT NULL,
    market_p           REAL,                      -- yes mid
    edge_pp            REAL,                      -- (our_p - market_p) × 100
    confidence         REAL NOT NULL,
    actionable         INTEGER NOT NULL,          -- 1 if passed gate, 0 = logged for monitor
    yes_bid            REAL,
    yes_ask            REAL,
    feature_audit_json TEXT NOT NULL              -- full feature breakdown
);
CREATE INDEX IF NOT EXISTS idx_argus_cand_ts      ON argus_candidates(ts);
CREATE INDEX IF NOT EXISTS idx_argus_cand_ticker  ON argus_candidates(ticker);
CREATE INDEX IF NOT EXISTS idx_argus_cand_actionable ON argus_candidates(actionable);
"""


def _get_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(CANDIDATES_DB()), timeout=10.0)
    conn.executescript(SCHEMA)
    return conn


def _market_mid(market: dict) -> Optional[float]:
    """Compute yes mid from bid/ask. None if no quotes."""
    try:
        bid = float(market.get("yes_bid_dollars") or 0)
        ask = float(market.get("yes_ask_dollars") or 0)
    except (ValueError, TypeError):
        return None
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if bid > 0:
        return bid
    if ask > 0:
        return ask
    # Fall back to last_price if available
    try:
        last = float(market.get("last_price_dollars") or 0)
        return last if last > 0 else None
    except (ValueError, TypeError):
        return None


def pull_active_markets(series_ticker: str = MARKET_PREFIX) -> list[dict]:
    c = KalshiClient()
    out: list[dict] = []
    cursor = None
    pages = 0
    # Some Kalshi accounts use "active", others use "open" — try both
    for status in ("active", "open"):
        cursor = None
        while pages < 20:
            params = {"series_ticker": series_ticker, "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                r = c.get_unauth("/markets", params=params)
            except Exception as e:
                _log.warning(f"pull status={status} failed: {e}")
                break
            ms = r.get("markets", [])
            out.extend(ms)
            pages += 1
            cursor = r.get("cursor")
            if not cursor or not ms:
                break
        if out:
            break
    # Dedupe by ticker
    seen = set(); uniq: list[dict] = []
    for m in out:
        tk = m.get("ticker")
        if tk and tk not in seen:
            seen.add(tk); uniq.append(m)
    return uniq


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE_PP,
                    help=f"min edge_pp to flag actionable (default {MIN_EDGE_PP})")
    ap.add_argument("--min-conf", type=float, default=MIN_CONFIDENCE,
                    help=f"min confidence (default {MIN_CONFIDENCE})")
    a = ap.parse_args()

    # Insist on a trained model (no silent heuristic fallback)
    mp = MODEL_PATH()
    if not mp.exists():
        print(f"ERROR: trained model not found at {mp}", file=sys.stderr)
        print("Run: python -m tools.argus_backtest --brain music --save-model",
              file=sys.stderr)
        return 2
    model = MusicModel.load(mp)
    model_version = "v1"
    _log.info(f"loaded model n_train={model.n_train} trained_at={model.trained_at}")

    brain = MusicBrain(client=SpotifyChartsClient(), model=model)

    markets = pull_active_markets()
    _log.info(f"pulled {len(markets)} active KXRANKLISTSONGSPOT* markets")
    if not markets:
        print("\nNo active markets returned by Kalshi for this series.")
        return 0

    ts_now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows_to_persist: list[tuple] = []
    candidates: list[dict] = []
    n_no_pred = n_no_mid = 0

    for m in markets:
        tk = m.get("ticker", "")
        pred = brain.predict(m)
        if pred is None:
            n_no_pred += 1
            continue
        mid = _market_mid(m)
        if mid is None:
            n_no_mid += 1
            edge_pp = None
        else:
            edge_pp = (pred.p_yes - mid) * 100.0    # signed

        actionable = (
            edge_pp is not None
            and abs(edge_pp) >= a.min_edge
            and pred.confidence >= a.min_conf
        )

        feat = pred.key_features
        rows_to_persist.append((
            ts_now, "music", model_version, tk,
            feat.get("artist_name"), feat.get("resolution_month"),
            round(pred.p_yes, 4), round(mid, 4) if mid else None,
            round(edge_pp, 4) if edge_pp is not None else None,
            round(pred.confidence, 4), 1 if actionable else 0,
            float(m.get("yes_bid_dollars") or 0) or None,
            float(m.get("yes_ask_dollars") or 0) or None,
            json.dumps(feat, default=str),
        ))

        if actionable:
            candidates.append({
                "ticker":          tk,
                "artist":          feat.get("artist_name"),
                "month":           feat.get("resolution_month"),
                "our_p":           round(pred.p_yes, 4),
                "market_p":        round(mid, 4) if mid else None,
                "edge_pp":         round(edge_pp, 4) if edge_pp is not None else None,
                "confidence":      round(pred.confidence, 4),
                "side":            "YES" if (edge_pp or 0) > 0 else "NO",
                "top_rank_lift":   feat.get("top_rank_lift"),
                "days_factor":     feat.get("days_factor"),
                "historical_no1_rate_12mo": feat.get("historical_no1_rate_12mo"),
                "streams_velocity_norm_today": feat.get("streams_velocity_norm_today"),
                "current_top_track_rank":      feat.get("current_top_track_rank"),
            })

    # Persist all (even non-actionable — needed for velocity-feature monitor)
    conn = _get_db()
    try:
        conn.executemany(
            """INSERT INTO argus_candidates (
                ts, brain_id, model_version, ticker, artist, resolution_month,
                our_p, market_p, edge_pp, confidence, actionable,
                yes_bid, yes_ask, feature_audit_json
              ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows_to_persist,
        )
        conn.commit()
    finally:
        conn.close()

    # Sort actionables by absolute edge
    candidates.sort(key=lambda c: -abs(c.get("edge_pp") or 0))
    top = candidates[:10]

    if a.json:
        print(json.dumps({
            "ts":            ts_now,
            "model_version": model_version,
            "n_markets":     len(markets),
            "n_predictions": len(rows_to_persist),
            "n_no_pred":     n_no_pred,
            "n_no_mid":      n_no_mid,
            "n_actionable":  len(candidates),
            "top10":         top,
        }, indent=2, default=str))
        return 0

    print(f"\n━━━ ARGUS SCAN — music ━━━")
    print(f"  ts:            {ts_now}")
    print(f"  model:         v1  (n_train={model.n_train})")
    print(f"  markets:       {len(markets)}  preds={len(rows_to_persist)}  "
          f"no_pred={n_no_pred}  no_mid={n_no_mid}")
    print(f"  actionable:    {len(candidates)}  (edge≥{a.min_edge}pp, conf≥{a.min_conf})")
    print()
    if not top:
        print("  (no actionable candidates)")
        return 0
    print(f"  TOP {len(top)} CANDIDATES:")
    print(f"  {'ticker':<48} {'side':<3} {'edge':>6} {'our':>5} {'mkt':>5} {'conf':>5} "
          f"{'rank':>4} {'days':>5} {'hist':>5}")
    for c in top:
        print(f"  {c['ticker'][:48]:<48} {c['side']:<3} "
              f"{c['edge_pp']:>+6.2f} {c['our_p']:>5.2f} "
              f"{c['market_p']:>5.2f} {c['confidence']:>5.2f} "
              f"{int(c['current_top_track_rank']):>4} "
              f"{c['days_factor']:>5.2f} {c['historical_no1_rate_12mo']:>5.2f}")
    print()
    print("  (full feature audit per candidate persisted to "
          f"{CANDIDATES_DB().name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
