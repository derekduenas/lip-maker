"""ARGUS paper-mode scanner — multi-brain (Music + Weather as of 2026-05-12).

Why multi-brain: Music settles monthly → first paper reconciliation ~June 1
(too slow to validate model). Weather settles DAILY → first reconciliations
within 24-48h, so the paper track record builds 10× faster. Same paper
machinery (table, sizer, reconciler, stats) — brains plug in via the
DomainBrain interface.

Pipeline (paper-only — no real orders):
  STEP 0 (reconcile FIRST): for every status='open' position in
         argus_paper_positions, ask Kalshi whether the market settled.
         If yes → score paper_pnl, mark resolved.
  STEP 1: Load trained MusicModel; instantiate WeatherBrain (NWS-driven,
          no separate trained-model file).
  STEP 2: Pull all active KXRANKLISTSONGSPOT{GLOBAL,USA}-* (music) AND
          KX{HIGH,LOW,HIGHT,LOWT}{CITY}-* (weather) markets from Kalshi.
  STEP 3: For each market, route to its brain via _scan_one_brain helper.
  STEP 4-7: edge_pp, actionable gate, persist row, emit paper position
            (quarter-Kelly via argus.execution.sizer) — same as before.
  STEP 8: Print combined per-brain summary + paper-track stats by
          (model_version, chart_region). chart_region values:
          GLOBAL/USA (music) and WEATHER (weather).

Cron: deploy/argus-scan.{timer,service} → daily 23:00 UTC.

USAGE:
  python -m tools.argus_scan
  python -m tools.argus_scan --json
  python -m tools.argus_scan --min-edge 5      # custom edge gate
  python -m tools.argus_scan --no-paper        # skip new paper-position writes
  python -m tools.argus_scan --reconcile-only  # just close resolved, skip scan
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.kalshi_auth import KalshiClient
from argus.brains.music import (
    MusicBrain, MusicModel, MARKET_PREFIXES as MUSIC_PREFIXES,
    MODEL_PATH, parse_ticker as parse_music_ticker,
)
from argus.brains.weather import (
    WeatherBrain, ALL_PREFIXES as WEATHER_PREFIXES, parse_market as parse_weather_market,
)
from argus.config import (
    DATA_DIR, MIN_EDGE_PP, MIN_CONFIDENCE, ensure_dirs,
)
from argus.data.spotify_charts import SpotifyChartsClient
from argus.data.nws import NWSClient
from argus.execution.sizer import size_prediction

_log = logging.getLogger(__name__)

CANDIDATES_DB = lambda: Path(DATA_DIR) / "argus_candidates.db"

# ARGUS_PAPER_BANKROLL takes precedence over ARGUS_BANKROLL for paper sizing
# so the paper book can be capitalized independently of any future live cap.
PAPER_BANKROLL = float(
    os.getenv("ARGUS_PAPER_BANKROLL")
    or os.getenv("ARGUS_BANKROLL", "1000")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS argus_candidates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,             -- ISO UTC of scan
    brain_id           TEXT NOT NULL,
    model_version      TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    artist             TEXT,
    resolution_month   TEXT,                      -- YYYY-MM
    chart_region       TEXT,                      -- GLOBAL | USA (2026-05-10)
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

CREATE TABLE IF NOT EXISTS argus_paper_positions (
    position_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    side           TEXT NOT NULL,            -- 'yes' | 'no'
    entry_price    REAL NOT NULL,            -- market mid at entry, $0..1
    size_usd       REAL NOT NULL,            -- quarter-Kelly recommended
    entry_ts       TEXT NOT NULL,            -- ISO UTC
    model_p        REAL NOT NULL,            -- our_p at entry
    market_p       REAL NOT NULL,            -- mid at entry
    edge_pp        REAL NOT NULL,            -- signed edge at entry
    confidence     REAL NOT NULL,
    brain_id       TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    chart_region   TEXT,                     -- GLOBAL | USA (NULL for non-music)
    status         TEXT NOT NULL,            -- 'open' | 'resolved' | 'aborted'
    settled_at     TEXT,                     -- ISO UTC of settle (resolved only)
    outcome        TEXT,                     -- 'yes' | 'no' | NULL
    paper_pnl      REAL,                     -- realized $ if resolved
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_argus_paper_status ON argus_paper_positions(status);
CREATE INDEX IF NOT EXISTS idx_argus_paper_ticker ON argus_paper_positions(ticker);
"""


def _get_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(CANDIDATES_DB()), timeout=10.0)
    conn.executescript(SCHEMA)
    # Best-effort migration: add chart_region to old argus_candidates rows.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(argus_candidates)")]
        if "chart_region" not in cols:
            conn.execute("ALTER TABLE argus_candidates ADD COLUMN chart_region TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
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
    try:
        last = float(market.get("last_price_dollars") or 0)
        return last if last > 0 else None
    except (ValueError, TypeError):
        return None


def pull_active_markets(series_ticker: str) -> list[dict]:
    c = KalshiClient()
    out: list[dict] = []
    cursor = None
    pages = 0
    # Some Kalshi accounts use "active", others use "open" — try both
    for status in ("active", "open"):
        cursor = None
        page_n = 0
        while page_n < 20:
            params = {"series_ticker": series_ticker, "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                r = c.get_unauth("/markets", params=params)
            except Exception as e:
                _log.warning(f"pull series={series_ticker} status={status} failed: {e}")
                break
            ms = r.get("markets", [])
            out.extend(ms)
            page_n += 1
            pages += 1
            cursor = r.get("cursor")
            if not cursor or not ms:
                break
        if out:
            break
    seen = set(); uniq: list[dict] = []
    for m in out:
        tk = m.get("ticker")
        if tk and tk not in seen:
            seen.add(tk); uniq.append(m)
    return uniq


def pull_active_markets_all_prefixes(prefixes: list[str]) -> list[dict]:
    """Pull markets for each EXACT series_ticker and combine."""
    combined: list[dict] = []
    seen: set[str] = set()
    for p in prefixes:
        markets = pull_active_markets(p)
        _log.info(f"pulled {len(markets)} active for {p}")
        for m in markets:
            tk = m.get("ticker")
            if tk and tk not in seen:
                seen.add(tk); combined.append(m)
    return combined


def discover_series_starting_with(prefix: str, *,
                                  drop_keywords: tuple[str, ...] = ()) -> list[str]:
    """Enumerate all open Kalshi series whose ticker startswith prefix.

    Used for prefix families (KXHIGHT*, KXLOWT*) where the underlying series
    are city-specific (KXHIGHTNY, KXHIGHTPHX, ...). Kalshi's
    /markets?series_ticker filter wants exact matches, so we have to expand
    the prefix to a list first via /series.
    """
    c = KalshiClient()
    series_out: list[str] = []
    for status in ("active", "open"):
        cursor = None
        pages = 0
        while pages < 30:
            params = {"status": status, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                r = c.get_unauth("/series", params=params)
            except Exception as e:
                _log.warning(f"series enum failed (status={status}): {e}")
                break
            ss = r.get("series", [])
            for s in ss:
                t = s.get("ticker", "")
                if not t.startswith(prefix):
                    continue
                if any(k in t.upper() for k in drop_keywords):
                    continue
                if t not in series_out:
                    series_out.append(t)
            cursor = r.get("cursor")
            pages += 1
            if not cursor or not ss:
                break
        if series_out:
            break
    return series_out


def pull_active_markets_for_weather(prefix_families: list[str]) -> list[dict]:
    """Resolve each weather prefix family to concrete city series, then pull
    markets per series. Filters out non-temperature siblings whose tickers
    happen to share the prefix (KXHIGHINFLATION, KXLOWESTRATE, etc.).
    """
    DROP = ("INFLATION", "RATE", "MOV", "MOVDJT", "MOVKH", "YIELD", "VIX",
            "UMICH", "DXY")
    all_series: list[str] = []
    for fam in prefix_families:
        seen_in_family = discover_series_starting_with(fam, drop_keywords=DROP)
        for s in seen_in_family:
            if s not in all_series:
                all_series.append(s)
    _log.info(f"weather: resolved {len(prefix_families)} prefix families to "
              f"{len(all_series)} series")
    combined: list[dict] = []
    seen: set[str] = set()
    for s in all_series:
        markets = pull_active_markets(s)
        for m in markets:
            tk = m.get("ticker")
            if tk and tk not in seen:
                seen.add(tk); combined.append(m)
    return combined


# ── Reconciler ──────────────────────────────────────────────────────────────
def _market_outcome(client: KalshiClient, ticker: str) -> Optional[dict]:
    """Return {'status', 'result', 'close_time'} for a market, or None on err.

    Kalshi /markets/{ticker} returns:
      market.status: 'open' | 'closed' | 'settled' | 'finalized' | 'unopened'
      market.result: 'yes' | 'no' | '' (only populated once settled)
      market.close_time: ISO timestamp of close
    """
    try:
        r = client.get_unauth(f"/markets/{ticker}")
    except Exception as e:
        _log.debug(f"market lookup {ticker} failed: {e}")
        return None
    m = r.get("market") or {}
    return {
        "status":     m.get("status", ""),
        "result":     (m.get("result") or "").lower(),
        "close_time": m.get("close_time"),
    }


def _score_paper_pnl(*, side: str, size_usd: float, entry_price: float,
                     outcome: str) -> float:
    """Compute realized paper PnL for a binary position.

    contracts = size_usd / entry_price   (entry_price in $0..1)
    YES side: win pays (1 - entry_price) per contract; lose costs entry_price
    NO  side: win pays entry_price       per contract; lose costs (1 - entry_price)

    Net dollars (regardless of side):
      win  → +contracts * (1 - cost)   where cost = entry_price (yes) or 1-entry_price (no)
      lose → -contracts * cost
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    cost = entry_price if side == "yes" else (1.0 - entry_price)
    if cost <= 0:
        return 0.0
    contracts = size_usd / cost
    won = (outcome == "yes" and side == "yes") or (outcome == "no" and side == "no")
    if won:
        return round(contracts * (1.0 - cost), 4)
    return round(-contracts * cost, 4)


def reconcile_open_positions(conn: sqlite3.Connection) -> dict:
    """Close resolved paper positions. Returns summary dict."""
    rows = conn.execute(
        """SELECT position_id, ticker, side, entry_price, size_usd
           FROM argus_paper_positions WHERE status='open'"""
    ).fetchall()
    if not rows:
        return {"checked": 0, "resolved": 0, "still_open": 0, "errors": 0,
                "pnl_resolved_usd": 0.0}

    client = KalshiClient()
    n_resolved = n_open = n_err = 0
    pnl_total = 0.0
    for pid, ticker, side, entry_price, size_usd in rows:
        info = _market_outcome(client, ticker)
        if info is None:
            n_err += 1
            continue
        result = info["result"]
        status = info["status"]
        if result not in ("yes", "no"):
            # Not yet settled — leave open
            n_open += 1
            continue
        pnl = _score_paper_pnl(
            side=side, size_usd=float(size_usd),
            entry_price=float(entry_price), outcome=result,
        )
        conn.execute(
            """UPDATE argus_paper_positions
               SET status='resolved', outcome=?, paper_pnl=?, settled_at=?,
                   notes=COALESCE(notes,'') || ?
               WHERE position_id=?""",
            (result, pnl,
             dt.datetime.now(dt.timezone.utc).isoformat(),
             f" reconciled status={status}", pid),
        )
        n_resolved += 1
        pnl_total += pnl
    conn.commit()
    return {
        "checked":    len(rows),
        "resolved":   n_resolved,
        "still_open": n_open,
        "errors":     n_err,
        "pnl_resolved_usd": round(pnl_total, 2),
    }


# ── Paper-position writer ───────────────────────────────────────────────────
def _has_open_position(conn: sqlite3.Connection, ticker: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM argus_paper_positions WHERE ticker=? AND status='open' LIMIT 1",
        (ticker,),
    ).fetchone()
    return r is not None


def _emit_paper_position(
    conn: sqlite3.Connection, *,
    pred,                    # Prediction
    market: dict, mid: float, edge_pp: float,
    bankroll: float, deployed_usd: float,
    chart_region: Optional[str], model_version: str,
) -> Optional[dict]:
    """Size + insert one paper position. Returns row dict or None if rejected."""
    sd = size_prediction(pred, market_p=mid,
                         bankroll=bankroll, deployed_usd=deployed_usd)
    if sd.rejected or sd.final_usd <= 0:
        return None
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO argus_paper_positions (
              ticker, side, entry_price, size_usd, entry_ts,
              model_p, market_p, edge_pp, confidence,
              brain_id, model_version, chart_region, status, notes
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
        (
            pred.market_ticker, sd.side, mid, sd.final_usd, ts,
            pred.p_yes, mid, edge_pp, pred.confidence,
            pred.brain_id, model_version, chart_region,
            "; ".join(sd.rationale)[:500],
        ),
    )
    conn.commit()
    return {
        "ticker":      pred.market_ticker,
        "side":        sd.side,
        "size_usd":    sd.final_usd,
        "entry_price": mid,
    }


def _open_deployed_total(conn: sqlite3.Connection) -> float:
    r = conn.execute(
        "SELECT COALESCE(SUM(size_usd), 0) FROM argus_paper_positions WHERE status='open'"
    ).fetchone()
    return float(r[0] or 0)


# ── Per-brain scan helper ────────────────────────────────────────────────
def _chart_region_for(brain_id: str, market: dict) -> Optional[str]:
    """Resolve chart_region per brain — used for separate Brier tracking."""
    if brain_id == "music":
        meta = parse_music_ticker(market.get("ticker", ""), market.get("yes_sub_title"))
        return meta.chart_region if meta else None
    if brain_id == "weather":
        meta = parse_weather_market(market)
        return meta.chart_region if meta else None
    return None


def _scan_one_brain(
    *, conn: sqlite3.Connection, brain, brain_id: str, model_version: str,
    markets: list[dict], min_edge: float, min_conf: float, no_paper: bool,
    ts_now: str, deployed_now: float, paper_bankroll: float,
) -> dict:
    """Run one brain over its market universe. Mutates DB. Returns summary."""
    rows: list[tuple] = []
    candidates: list[dict] = []
    paper_emitted: list[dict] = []
    n_no_pred = n_no_mid = 0

    for m in markets:
        tk = m.get("ticker", "")
        chart_region = _chart_region_for(brain_id, m)
        try:
            pred = brain.predict(m)
        except Exception as e:
            _log.warning(f"{brain_id}.predict({tk}) failed: {e}")
            n_no_pred += 1
            continue
        if pred is None:
            n_no_pred += 1
            continue
        mid = _market_mid(m)
        if mid is None:
            n_no_mid += 1
            edge_pp = None
        else:
            edge_pp = (pred.p_yes - mid) * 100.0

        actionable = (
            edge_pp is not None
            and abs(edge_pp) >= min_edge
            and pred.confidence >= min_conf
        )

        feat = pred.key_features
        # Pull the brain-agnostic surrogate fields the candidates table
        # expects (artist + resolution_month). For weather, use city +
        # settle_date so the row is still self-describing.
        surrogate_artist = feat.get("artist_name") or feat.get("city")
        surrogate_window = feat.get("resolution_month") or feat.get("settle_date")
        rows.append((
            ts_now, brain_id, model_version, tk,
            surrogate_artist, surrogate_window, chart_region,
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
                "brain_id":        brain_id,
                "chart_region":    chart_region,
                "label":           surrogate_artist,
                "window":          surrogate_window,
                "our_p":           round(pred.p_yes, 4),
                "market_p":        round(mid, 4) if mid else None,
                "edge_pp":         round(edge_pp, 4) if edge_pp is not None else None,
                "confidence":      round(pred.confidence, 4),
                "side":            "YES" if (edge_pp or 0) > 0 else "NO",
                "feature_audit":   feat,
            })
            if (not no_paper) and (not _has_open_position(conn, tk)):
                emitted = _emit_paper_position(
                    conn, pred=pred, market=m, mid=mid, edge_pp=edge_pp,
                    bankroll=paper_bankroll, deployed_usd=deployed_now,
                    chart_region=chart_region, model_version=model_version,
                )
                if emitted:
                    paper_emitted.append(emitted)
                    deployed_now += emitted["size_usd"]

    return {
        "rows":          rows,
        "candidates":    candidates,
        "paper_emitted": paper_emitted,
        "n_no_pred":     n_no_pred,
        "n_no_mid":      n_no_mid,
        "deployed_now":  deployed_now,
    }


# ── Stats ───────────────────────────────────────────────────────────────────
def paper_stats(conn: sqlite3.Connection) -> list[dict]:
    """One row per (model_version, chart_region) bucket."""
    rows = conn.execute(
        """SELECT model_version, COALESCE(chart_region, 'GLOBAL'),
                  status, COUNT(*), COALESCE(SUM(size_usd), 0),
                  COALESCE(SUM(paper_pnl), 0)
           FROM argus_paper_positions
           GROUP BY model_version, chart_region, status"""
    ).fetchall()
    bucket: dict[tuple, dict] = {}
    for mv, cr, status, n, size, pnl in rows:
        key = (mv, cr)
        b = bucket.setdefault(key, {
            "model_version": mv, "chart_region": cr,
            "open": 0, "resolved": 0, "wins": 0, "losses": 0,
            "size_open_usd": 0.0, "pnl_resolved_usd": 0.0,
        })
        if status == "open":
            b["open"] += n
            b["size_open_usd"] += float(size)
        elif status == "resolved":
            b["resolved"] += n
            b["pnl_resolved_usd"] += float(pnl or 0)
    # Wins/losses lookup
    wl = conn.execute(
        """SELECT model_version, COALESCE(chart_region, 'GLOBAL'),
                  outcome, side, COUNT(*)
           FROM argus_paper_positions
           WHERE status='resolved'
           GROUP BY model_version, chart_region, outcome, side"""
    ).fetchall()
    for mv, cr, outcome, side, n in wl:
        b = bucket.get((mv, cr))
        if not b:
            continue
        won = (outcome == "yes" and side == "yes") or (outcome == "no" and side == "no")
        if won: b["wins"] += n
        else:   b["losses"] += n
    return list(bucket.values())


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE_PP,
                    help=f"min edge_pp to flag actionable (default {MIN_EDGE_PP})")
    ap.add_argument("--min-conf", type=float, default=MIN_CONFIDENCE,
                    help=f"min confidence (default {MIN_CONFIDENCE})")
    ap.add_argument("--no-paper", action="store_true",
                    help="do not write new paper positions (just log candidates)")
    ap.add_argument("--reconcile-only", action="store_true",
                    help="only score resolved paper positions, then exit")
    a = ap.parse_args()

    conn = _get_db()

    # ── STEP 0: reconcile open positions FIRST ──
    rec = reconcile_open_positions(conn)
    if rec["resolved"]:
        _log.info(
            f"reconciled {rec['resolved']}/{rec['checked']} paper positions "
            f"(still open: {rec['still_open']}, errs: {rec['errors']}, "
            f"realized $: {rec['pnl_resolved_usd']:+.2f})"
        )
    elif rec["checked"]:
        _log.info(
            f"reconciler: {rec['checked']} open, none resolved yet "
            f"(errs: {rec['errors']})"
        )
    if a.reconcile_only:
        if a.json:
            print(json.dumps({"reconcile": rec}, indent=2, default=str))
        else:
            print(f"\n━━━ ARGUS reconcile (only) ━━━")
            for k, v in rec.items():
                print(f"  {k}: {v}")
        conn.close()
        return 0

    # Insist on a trained model
    mp = MODEL_PATH()
    if not mp.exists():
        print(f"ERROR: trained model not found at {mp}", file=sys.stderr)
        print("Run: python -m tools.argus_backtest --brain music --save-model",
              file=sys.stderr)
        conn.close()
        return 2
    model = MusicModel.load(mp)
    model_version = "v1"
    _log.info(f"loaded music model n_train={model.n_train} trained_at={model.trained_at}")

    music_brain   = MusicBrain(client=SpotifyChartsClient(), model=model)
    weather_brain = WeatherBrain(client=NWSClient())

    music_markets = pull_active_markets_all_prefixes(MUSIC_PREFIXES)
    _log.info(f"music: pulled {len(music_markets)} active across {MUSIC_PREFIXES}")
    weather_markets = pull_active_markets_for_weather(list(WEATHER_PREFIXES))
    _log.info(f"weather: pulled {len(weather_markets)} active markets")

    if not music_markets and not weather_markets:
        print("\nNo active markets returned by Kalshi for any series.")
        conn.close()
        return 0

    ts_now = dt.datetime.now(dt.timezone.utc).isoformat()
    deployed_now = _open_deployed_total(conn)

    music_res = _scan_one_brain(
        conn=conn, brain=music_brain, brain_id="music", model_version=model_version,
        markets=music_markets, min_edge=a.min_edge, min_conf=a.min_conf,
        no_paper=a.no_paper, ts_now=ts_now, deployed_now=deployed_now,
        paper_bankroll=PAPER_BANKROLL,
    )
    deployed_now = music_res["deployed_now"]

    weather_res = _scan_one_brain(
        conn=conn, brain=weather_brain, brain_id="weather", model_version="v1",
        markets=weather_markets, min_edge=a.min_edge, min_conf=a.min_conf,
        no_paper=a.no_paper, ts_now=ts_now, deployed_now=deployed_now,
        paper_bankroll=PAPER_BANKROLL,
    )
    deployed_now = weather_res["deployed_now"]

    all_rows = music_res["rows"] + weather_res["rows"]
    all_candidates = music_res["candidates"] + weather_res["candidates"]
    all_paper_emitted = music_res["paper_emitted"] + weather_res["paper_emitted"]

    # Persist candidates
    try:
        conn.executemany(
            """INSERT INTO argus_candidates (
                ts, brain_id, model_version, ticker, artist, resolution_month,
                chart_region, our_p, market_p, edge_pp, confidence, actionable,
                yes_bid, yes_ask, feature_audit_json
              ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            all_rows,
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        _log.warning(f"insert failed ({e}); retrying without chart_region")
        compat = [(r[0], r[1], r[2], r[3], r[4], r[5],
                   r[7], r[8], r[9], r[10], r[11], r[12], r[13], r[14])
                  for r in all_rows]
        conn.executemany(
            """INSERT INTO argus_candidates (
                ts, brain_id, model_version, ticker, artist, resolution_month,
                our_p, market_p, edge_pp, confidence, actionable,
                yes_bid, yes_ask, feature_audit_json
              ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            compat,
        )
        conn.commit()

    all_candidates.sort(key=lambda c: -abs(c.get("edge_pp") or 0))
    top = all_candidates[:10]
    stats = paper_stats(conn)

    if a.json:
        def _bsum(res, key):
            return res[key] if isinstance(res[key], int) else len(res[key])
        print(json.dumps({
            "ts":             ts_now,
            "model_version":  model_version,
            "music":   {
                "n_markets":     len(music_markets),
                "n_predictions": len(music_res["rows"]),
                "n_no_pred":     music_res["n_no_pred"],
                "n_no_mid":      music_res["n_no_mid"],
                "n_actionable":  len(music_res["candidates"]),
                "paper_emitted": music_res["paper_emitted"],
            },
            "weather": {
                "n_markets":     len(weather_markets),
                "n_predictions": len(weather_res["rows"]),
                "n_no_pred":     weather_res["n_no_pred"],
                "n_no_mid":      weather_res["n_no_mid"],
                "n_actionable":  len(weather_res["candidates"]),
                "paper_emitted": weather_res["paper_emitted"],
            },
            "paper_bankroll": PAPER_BANKROLL,
            "reconcile":      rec,
            "paper_stats":    stats,
            "top10":          top,
        }, indent=2, default=str))
        conn.close()
        return 0

    print(f"\n━━━ ARGUS SCAN — multi-brain ━━━")
    print(f"  ts:           {ts_now}")
    print(f"  reconcile:    resolved={rec['resolved']}  still_open={rec['still_open']}  "
          f"realized=${rec['pnl_resolved_usd']:+.2f}")
    print(f"  paper_book:   bankroll=${PAPER_BANKROLL:.0f}  "
          f"deployed=${deployed_now:.2f}  emitted_this_scan={len(all_paper_emitted)}")
    print()
    for label, res, prefixes in (
        ("music",   music_res,   MUSIC_PREFIXES),
        ("weather", weather_res, list(WEATHER_PREFIXES)),
    ):
        n_markets = len(music_markets) if label == "music" else len(weather_markets)
        print(f"  [{label.upper():<7}] markets={n_markets:>3}  "
              f"preds={len(res['rows']):>3}  no_pred={res['n_no_pred']:>3}  "
              f"no_mid={res['n_no_mid']:>3}  actionable={len(res['candidates']):>3}  "
              f"new_paper={len(res['paper_emitted']):>2}")
        for e in res["paper_emitted"][:5]:
            print(f"    + {e['ticker'][:48]:<48} {e['side']:<3} "
                  f"${e['size_usd']:>6.2f} @ {e['entry_price']:.3f}")
    print()

    if stats:
        print(f"  PAPER TRACK RECORD by (version, region):")
        for s in sorted(stats, key=lambda x: (x["model_version"], x["chart_region"] or "")):
            wins = s["wins"]; loss = s["losses"]; n_rez = wins + loss
            wr = (wins / n_rez * 100.0) if n_rez > 0 else 0.0
            print(f"    {s['model_version']}/{(s['chart_region'] or '?'):<8s} "
                  f"open={s['open']:>3d} (${s['size_open_usd']:>7.2f})  "
                  f"resolved={s['resolved']:>3d}  "
                  f"WR={wr:>5.1f}%  realized=${s['pnl_resolved_usd']:+.2f}")
        print()

    if not top:
        print("  (no actionable candidates)")
        conn.close()
        return 0
    print(f"  TOP {len(top)} CANDIDATES (by |edge_pp|):")
    print(f"  {'ticker':<48} {'brain':<7} {'reg':<7} {'side':<3} {'edge':>6} "
          f"{'our':>5} {'mkt':>5} {'conf':>5}")
    for c in top:
        print(f"  {c['ticker'][:48]:<48} {c['brain_id'][:7]:<7} "
              f"{(c['chart_region'] or '?')[:7]:<7} {c['side']:<3} "
              f"{c['edge_pp']:>+6.2f} {c['our_p']:>5.2f} "
              f"{c['market_p']:>5.2f} {c['confidence']:>5.2f}")
    print()
    print("  (full feature audit per candidate persisted to "
          f"{CANDIDATES_DB().name})")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
