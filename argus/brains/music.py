"""MusicBrain — predict per-artist-per-month #1 on Spotify Daily Top Songs Global.

Market structure (verified 2026-05-10 against 20 settled markets):
    Series:  KXRANKLISTSONGSPOTGLOBAL
    Question: "If {ARTIST} has a #1 song on Any Spotify Daily Top Songs
              Global chart in {MONTH YEAR}, then resolves to Yes."
    Trigger:  ANY day during the month — early-close once first hit
    Credit:   FEATURED artists count (Bieber+Nicki both YES on Beauty And
              A Beat)
    Source:   Spotify Daily Top Songs Global → kworb's
              /spotify/country/global_daily.html mirror
    Base rate: ~5-10% YES per market (heavy NO skew: 17/20 settled = NO)

Ticker format:
    KXRANKLISTSONGSPOTGLOBAL-{YY}{MMM}{DD}-{ARTIST_CODE}
        YY MMM DD = settle/expiration date (1st of month following resolution)
        ARTIST_CODE = 3-4 letters (NIC, JUS, OLII, OLI, MIC, etc.)
    Resolution month = the month BEFORE the settle date.
    Use market dict's `yes_sub_title` for canonical artist name (the 3-letter
    code is collision-prone — OLI vs OLII).

Features (6, replacing the original spec's 7):
    1. current_top_track_rank          — best rank of any song crediting
                                         artist on today's chart (200 if off)
    2. current_top_track_streams_velocity — daily delta on that top track
    3. days_remaining_in_month         — opportunity window for #1 hit
    4. historical_no1_rate_12mo        — fraction of past 12 months with #1
    5. nearest_climber_rank_change     — 7-day rank change of artist's top
    6. peer_density_at_top             — gap between #1 and #2 streams (small
                                         gap = leader vulnerable)

Model: logistic regression with intercept = logit(base_rate). Phase 3 ships
HEURISTIC weights (sensible priors); Phase 4 backtest tunes via gradient.

Confidence = 1 - 4 * p * (1 - p)  (high at extremes, 0 at p=0.5)

Early-close handling: not relevant for backtest reconstruction (we score
against the FINAL settled outcome regardless of when settle fired). For
the live scanner (Phase 5), poll daily — once an artist hits #1 the market
resolves and disappears from active set on next poll.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from argus.brains.base import DomainBrain, Prediction, confidence_from_extremes
from argus.config import DATA_DIR, ensure_dirs
from argus.data.spotify_charts import SpotifyChartsClient, ChartEntry

_log = logging.getLogger(__name__)


MARKET_PREFIX = "KXRANKLISTSONGSPOTGLOBAL"   # legacy alias (Phase 3)
# Series suffix → kworb chart slug. Add new region: append here only.
CHART_REGIONS = {
    "GLOBAL": "global",
    "USA":    "us",
}
MARKET_PREFIXES = [f"KXRANKLISTSONGSPOT{s}" for s in CHART_REGIONS]
MODEL_PATH    = lambda: Path(DATA_DIR) / "music_model_v1.json"

MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Heuristic priors — replaced by Phase 4 backtest training.
# Intercept = logit(0.10) ≈ -2.20 → bare-prior ~10% (matches observed base rate).
# Features are PRE-NORMALIZED in extract_features (see comments below),
# so weights operate on roughly comparable [0, 10] / [-1, 1] scales.
DEFAULT_WEIGHTS = {
    "intercept":                       -2.20,
    "top_rank_lift":                    +0.45,   # max(0, 11-rank): #1=+4.95, off-chart=0
    "streams_velocity_norm_today":      +0.20,   # streams_today_delta / 1e6
    "days_factor":                      +1.00,   # days_remaining / 31, [0,1]
    "historical_no1_rate_12mo":         +3.00,   # [0,1] artist past hits
    "streams_7d_velocity_sign":         +0.50,   # -1/0/+1 momentum direction
    "peer_gap_norm":                    -0.30,   # (s1-s2)/1e6: large gap = locked leader
}


# ── Ticker parsing ────────────────────────────────────────────────────────
_RX_TICKER = re.compile(
    r"^KXRANKLISTSONGSPOT(?P<region>" + "|".join(CHART_REGIONS) + r")-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-"
    r"(?P<artist_code>[A-Z]+)$"
)


@dataclass
class MarketMeta:
    """Parsed metadata for a Music market."""
    ticker:           str
    settle_date:      dt.date       # 1st of month following resolution
    resolution_year:  int
    resolution_month: int           # 1-12
    artist_code:      str           # 3-4 letter abbreviation
    chart_region:     str = "GLOBAL"          # GLOBAL | USA (kworb chart key)
    artist_name:      Optional[str] = None    # from yes_sub_title (canonical)


def parse_ticker(ticker: str, yes_sub_title: Optional[str] = None) -> Optional[MarketMeta]:
    """Parse a Music ticker. Returns None if not our format."""
    m = _RX_TICKER.match(ticker)
    if not m:
        return None
    yy   = int(m.group("yy"))
    mon  = MONTH_ABBR.get(m.group("mon"))
    if not mon:
        return None
    dd = int(m.group("dd"))
    try:
        settle_date = dt.date(2000 + yy, mon, dd)
    except ValueError:
        return None
    # Resolution month = month BEFORE settle date.
    if mon == 1:
        res_year, res_month = settle_date.year - 1, 12
    else:
        res_year, res_month = settle_date.year, mon - 1
    return MarketMeta(
        ticker=ticker, settle_date=settle_date,
        resolution_year=res_year, resolution_month=res_month,
        artist_code=m.group("artist_code"),
        chart_region=m.group("region"),
        artist_name=(yes_sub_title or "").strip() or None,
    )


# ── Featured-artist matcher ───────────────────────────────────────────────
_FEAT_INDICATORS = ["feat.", "featuring", "ft.", "(w/", "with ", "& ", ", "]


def chart_rows_crediting(artist_name: str, chart: list[ChartEntry]) -> list[ChartEntry]:
    """Return chart entries where artist is the primary OR a featured credit.

    Matching strategy (case-insensitive):
      - primary: chart row's `artist` field equals target
      - featured: target name appears verbatim in chart row's `track_name`
                  (catches "(w/ Nicki Minaj)", "feat. Nicki Minaj", etc.)

    False-positive risk: short artist names like "John" can match other
    Johns. Mitigated by requiring the FULL canonical name match, not first-
    name only. For ambiguous artists, the operator should refine the canon.
    """
    target = (artist_name or "").strip().lower()
    if not target:
        return []
    out: list[ChartEntry] = []
    for e in chart:
        if (e.artist or "").strip().lower() == target:
            out.append(e)
            continue
        if target in (e.track_name or "").lower():
            out.append(e)
    return out


# ── Feature extraction ────────────────────────────────────────────────────
@dataclass
class MusicFeatures:
    """Pre-normalized features fed to the logistic model.

    Raw measurements (rank, streams, gap) are kept too for explainability /
    debugging in key_features dict; only the *_norm / *_lift / *_sign
    versions enter the logistic z.
    """
    # raw (audit only)
    current_top_track_rank:        int      # 1-200 or 999 if off-chart
    current_top_track_streams:     int      # streams_today on top crediting track
    streams_today_delta:           int
    streams_7d_delta:              int
    peer_streams_gap:              int      # #1 streams - #2 streams

    # normalized (feed to model)
    top_rank_lift:                 float    # max(0, 11 - rank): #1 → 10
    streams_velocity_norm_today:   float    # delta / 1e6
    days_factor:                   float    # days_remaining / 31, [0, 1]
    historical_no1_rate_12mo:      float    # [0, 1] — Phase 4 backfills
    streams_7d_velocity_sign:      int      # -1 / 0 / +1
    peer_gap_norm:                 float    # gap / 1e6
    days_remaining_in_month:       int      # raw 0-31 (audit)

    def model_vec(self) -> dict:
        """The 6 normalized features that enter the logistic z-score."""
        return {
            "top_rank_lift":              self.top_rank_lift,
            "streams_velocity_norm_today": self.streams_velocity_norm_today,
            "days_factor":                self.days_factor,
            "historical_no1_rate_12mo":   self.historical_no1_rate_12mo,
            "streams_7d_velocity_sign":   self.streams_7d_velocity_sign,
            "peer_gap_norm":              self.peer_gap_norm,
        }

    def audit_dict(self) -> dict:
        """Full breakdown for explainability / Prediction.key_features."""
        return {
            **self.model_vec(),
            "current_top_track_rank":     self.current_top_track_rank,
            "current_top_track_streams":  self.current_top_track_streams,
            "streams_today_delta":        self.streams_today_delta,
            "streams_7d_delta":           self.streams_7d_delta,
            "peer_streams_gap":           self.peer_streams_gap,
            "days_remaining_in_month":    self.days_remaining_in_month,
        }


def days_remaining(year: int, month: int, today: dt.date) -> int:
    """Days remaining in (year, month) AS OF today. 0 if month is over."""
    last_day = calendar.monthrange(year, month)[1]
    end = dt.date(year, month, last_day)
    if today > end:
        return 0
    if today.year == year and today.month == month:
        return last_day - today.day
    # Future month — full month + months in between
    if (year, month) > (today.year, today.month):
        # naive: days till end of resolution month
        return (end - today).days
    return 0


def extract_features(
    artist_name:       str,
    resolution_year:   int,
    resolution_month:  int,
    chart:             list[ChartEntry],
    today:             dt.date,
    client:            Optional[SpotifyChartsClient] = None,
) -> MusicFeatures:
    """Compute features for one (artist, month) pair. Raw + normalized.

    `client` (optional) enables historical_no1_rate_12mo lookup. Pass the
    same SpotifyChartsClient the brain uses so the kworb cache is shared
    between chart fetches and per-track history fetches inside the helper.
    Without a client (e.g. unit tests with synthetic chart) the feature
    falls back to 0.0 — the prior bug, but explicit and test-only.
    """
    rows = chart_rows_crediting(artist_name, chart)
    if rows:
        top = min(rows, key=lambda e: e.rank)
        cur_rank   = top.rank
        cur_streams = top.streams_today
        d_today    = top.streams_today_delta
        d_7d       = top.streams_7d_delta
    else:
        cur_rank, cur_streams, d_today, d_7d = 999, 0, 0, 0

    # Peer density: streams gap between #1 and #2 (small gap = leader vulnerable)
    if len(chart) >= 2:
        peer_gap = max(0, chart[0].streams_today - chart[1].streams_today)
    else:
        peer_gap = 0

    days_left = days_remaining(resolution_year, resolution_month, today)

    # 2026-05-13: wire historical_no1_rate_12mo via the same authoritative
    # function that built the training set (argus/backtest/historical_no1).
    # No-leakage anchor = first day of resolution month (lookback = 365 days
    # ending the day BEFORE that). Per-(artist, anchor) JSON-cached so this
    # is one HTTP burst on first use, free thereafter. Fallback 0.0 only
    # when client is None (unit-test path) or lookup fails.
    hist_rate_12mo = 0.0
    if client is not None and artist_name:
        try:
            from argus.backtest.historical_no1 import compute_historical_no1_rate
            anchor = dt.date(resolution_year, resolution_month, 1)
            hist = compute_historical_no1_rate(artist_name, anchor, client)
            hist_rate_12mo = float(hist.rate)
        except Exception as e:
            _log.warning(
                f"historical_no1 lookup failed for {artist_name!r} "
                f"@ {resolution_year}-{resolution_month:02d}: {e}"
            )

    return MusicFeatures(
        # raw
        current_top_track_rank=cur_rank,
        current_top_track_streams=cur_streams,
        streams_today_delta=d_today,
        streams_7d_delta=d_7d,
        peer_streams_gap=peer_gap,
        days_remaining_in_month=days_left,
        # normalized
        top_rank_lift=max(0.0, 11.0 - cur_rank) if cur_rank <= 200 else 0.0,
        streams_velocity_norm_today=d_today / 1_000_000.0,
        days_factor=days_left / 31.0,
        historical_no1_rate_12mo=hist_rate_12mo,
        streams_7d_velocity_sign=(1 if d_7d > 0 else (-1 if d_7d < 0 else 0)),
        peer_gap_norm=peer_gap / 1_000_000.0,
    )


# ── Model ─────────────────────────────────────────────────────────────────
def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass
class MusicModel:
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    n_train: int = 0
    trained_at: Optional[str] = None
    notes:   str = "untrained — Phase 3 heuristic priors"

    def predict_p(self, feats: MusicFeatures) -> float:
        v = feats.model_vec()
        z = self.weights.get("intercept", 0.0)
        for k, val in v.items():
            z += self.weights.get(k, 0.0) * val
        # Clamp z to keep sigmoid finite
        z = max(-30.0, min(30.0, z))
        return _sigmoid(z)

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or MODEL_PATH()
        ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "weights":    self.weights,
                "n_train":    self.n_train,
                "trained_at": self.trained_at,
                "notes":      self.notes,
            }, f, indent=2)
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "MusicModel":
        """Load weights. Supports two on-disk shapes:
          legacy: {"weights": {...}, "n_train": int, "trained_at": str, "notes": str}
          v1:     {"version": "v1", "weights": {...}, "n_training_examples": int,
                   "trained_at": iso, "test_metrics": {...}, ...}
        """
        path = path or MODEL_PATH()
        if not path.exists():
            return cls()
        with open(path) as f:
            d = json.load(f)
        weights = d.get("weights", dict(DEFAULT_WEIGHTS))
        n_train = d.get("n_train") or d.get("n_training_examples", 0)
        trained_at = d.get("trained_at")
        version = d.get("version", "")
        tm = d.get("test_metrics", {}) or {}
        notes = d.get("notes") or (
            f"version={version}  bss={tm.get('bss')}  brier={tm.get('brier')}  "
            f"n_test={tm.get('n_test')}"
            if version else ""
        )
        return cls(
            weights=weights,
            n_train=n_train,
            trained_at=trained_at,
            notes=notes,
        )


# ── Brain ─────────────────────────────────────────────────────────────────
class MusicBrain(DomainBrain):
    brain_id       = "music"
    market_pattern = "KXRANKLISTSONGSPOT*-*"   # GLOBAL + USA

    def __init__(self, *, client: Optional[SpotifyChartsClient] = None,
                 model: Optional[MusicModel] = None) -> None:
        self.client = client or SpotifyChartsClient()
        self.model  = model  or MusicModel.load()

    def can_evaluate(self, market_ticker: str) -> bool:
        return any(market_ticker.startswith(p + "-") for p in MARKET_PREFIXES)

    def predict(self, market: dict) -> Optional[Prediction]:
        ticker = market.get("ticker", "")
        meta = parse_ticker(ticker, market.get("yes_sub_title"))
        if not meta or not meta.artist_name:
            _log.debug(f"unparsable or no canonical artist: {ticker}")
            return None
        chart_slug = CHART_REGIONS.get(meta.chart_region, "global")
        try:
            chart = self.client.get_chart(chart_slug)
        except Exception as e:
            _log.warning(f"chart fetch failed ({chart_slug}): {e}")
            return None
        if not chart:
            return None
        today = dt.date.today()
        feats = extract_features(
            artist_name=meta.artist_name,
            resolution_year=meta.resolution_year,
            resolution_month=meta.resolution_month,
            chart=chart, today=today, client=self.client,
        )
        # If month is over and artist never hit #1, p_yes ≈ 0.
        # If month is over and artist DID hit #1, market would have resolved
        # YES already; we still emit a low-confidence prediction.
        if feats.days_remaining_in_month == 0 and feats.current_top_track_rank > 1:
            p_yes = 0.01
        else:
            p_yes = self.model.predict_p(feats)

        p_yes = max(0.001, min(0.999, p_yes))
        return Prediction(
            p_yes=p_yes,
            confidence=confidence_from_extremes(p_yes),
            key_features={
                **feats.audit_dict(),
                "artist_name":     meta.artist_name,
                "artist_code":     meta.artist_code,
                "chart_region":    meta.chart_region,
                "resolution_month": f"{meta.resolution_year}-{meta.resolution_month:02d}",
                "settle_date":     meta.settle_date.isoformat(),
                "model_n_train":   self.model.n_train,
            },
            brain_id=self.brain_id,
            market_ticker=ticker,
        )


# ── Self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Parse 5 real settled tickers (verified in Phase 3 data-truth probe)
    samples = [
        ("KXRANKLISTSONGSPOTGLOBAL-26JUN01-NIC",  "Nicki Minaj"),
        ("KXRANKLISTSONGSPOTGLOBAL-26JUN01-JUS",  "Justin Bieber"),
        ("KXRANKLISTSONGSPOTGLOBAL-26MAY01-OLII", "Olivia Rodrigo"),
        ("KXRANKLISTSONGSPOTGLOBAL-26MAY01-OLI",  "Olivia Dean"),
        ("KXRANKLISTSONGSPOTGLOBAL-26MAY01-TAY",  "Taylor Swift"),
    ]
    print("=== Parse 5 real tickers ===")
    for tk, sub in samples:
        m = parse_ticker(tk, sub)
        assert m is not None, tk
        print(f"  {tk}  → res={m.resolution_year}-{m.resolution_month:02d}  "
              f"settle={m.settle_date}  artist={m.artist_name} ({m.artist_code})")

    # Save + reload model
    print("\n=== Model save/load roundtrip ===")
    m1 = MusicModel()
    p = m1.save()
    m2 = MusicModel.load()
    print(f"  saved to {p}, reloaded; weights match: {m1.weights == m2.weights}")
    assert m1.weights == m2.weights

    # Live spot-check: predict on Bieber JUN01 market (we know it resolved YES May 3)
    print("\n=== Live predict spot-check (Bieber, JUN01 market) ===")
    brain = MusicBrain()
    market = {
        "ticker": "KXRANKLISTSONGSPOTGLOBAL-26JUN01-JUS",
        "yes_sub_title": "Justin Bieber",
    }
    pred = brain.predict(market)
    if pred is None:
        print("  no prediction (chart fetch failed or parse miss)")
    else:
        print(f"  p_yes      = {pred.p_yes:.4f}")
        print(f"  confidence = {pred.confidence:.4f}")
        print(f"  features:")
        for k, v in pred.key_features.items():
            print(f"    {k:<40} = {v}")

    # Featured-artist sanity check
    print("\n=== Nicki Minaj (featured-artist) spot-check ===")
    market_n = {
        "ticker": "KXRANKLISTSONGSPOTGLOBAL-26JUN01-NIC",
        "yes_sub_title": "Nicki Minaj",
    }
    pred_n = brain.predict(market_n)
    if pred_n:
        rows = chart_rows_crediting("Nicki Minaj", brain.client.get_chart("global"))
        print(f"  rows crediting Nicki Minaj on today's chart: {len(rows)}")
        if rows:
            top = min(rows, key=lambda r: r.rank)
            print(f"  top entry: #{top.rank} {top.artist} - {top.track_name}")
        print(f"  p_yes={pred_n.p_yes:.4f}  conf={pred_n.confidence:.4f}")

    print("\nOK music brain")
