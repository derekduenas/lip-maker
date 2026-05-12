"""Rolling-window Poisson-of-Poissons model for "Will speaker say X N+ times
this month" markets (e.g. KXTRUMPSAY-{date}-{term}).

DECOMPOSITION:
    expected_speeches_remaining
      = (corpus_speeches / corpus_months) × (days_remaining_in_month / 30)
    expected_per_speech_count
      = total_mentions_of_term_in_corpus / corpus_speeches
    expected_total_mentions_remaining
      = expected_speeches_remaining × expected_per_speech_count
        + mentions_already_observed_this_month   (NOT YET WIRED — see note)
    P(total ≥ N) = 1 − PoissonCDF(N − 1, expected_total)

HARD GATE (epistemic humility):
    n_speeches_observed         ≥ MIN_CADENCE_OBS        (8)
    n_speeches_with_term_in_data ≥ MIN_TERM_OBS           (3)
    cadence_per_month_estimate  ∈ [SANITY_MIN_CADENCE, SANITY_MAX_CADENCE]
                                 ([1, 60] speeches/month — sanity rail)

If a market fails any gate → return SKIP. The 29-transcript Trump corpus
is structurally thin and the gate is expected to reject MOST markets at
deploy time. That's the point — better parked than miscalibrated.

A market that passes is tagged domain='rolling_window' so its Brier is
tracked SEPARATELY from earnings (earnings_brier) and Fed (fed_brier).
A miscalibrated rolling-window won't contaminate the other domains.

Mentions-already-this-month note: a full implementation would also count
verified mentions Trump has made this month (e.g. from his Truth Social
feed or live transcripts). Until that source is wired, we model the
prediction as "from-scratch over the full month" — which slightly
under-estimates probability near month-end if the term has been mentioned.
This is logged in key_features as `already_counted_in_month=False`.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

_log = logging.getLogger(__name__)

# ── Hard-gate thresholds ───────────────────────────────────────────────────
MIN_CADENCE_OBS         = 8        # need ≥ this many speeches to estimate cadence
MIN_TERM_OBS            = 3        # need ≥ this many speeches with the term
SANITY_MIN_CADENCE      = 1.0      # speeches/month floor
SANITY_MAX_CADENCE      = 60.0     # speeches/month ceiling


# ── Poisson math ───────────────────────────────────────────────────────────
def _poisson_cdf(k: int, lam: float) -> float:
    """P(X ≤ k) where X ~ Poisson(lam). Numerically stable for our ranges
    (typical k < 50, lam < 100)."""
    if lam <= 0:
        return 1.0 if k >= 0 else 0.0
    if k < 0:
        return 0.0
    # Iterative: pmf(0) = e^{-lam}; pmf(i+1) = pmf(i) * lam / (i+1)
    log_pmf_0 = -lam
    pmf = math.exp(log_pmf_0)
    cdf = pmf
    for i in range(k):
        pmf = pmf * lam / (i + 1)
        cdf += pmf
    return min(1.0, cdf)


def p_at_least_n(n: int, expected_total: float) -> float:
    """P(X ≥ n) where X ~ Poisson(expected_total)."""
    if n <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - _poisson_cdf(n - 1, expected_total)))


# ── Data layer ─────────────────────────────────────────────────────────────
def speaker_cadence_per_month(conn: sqlite3.Connection, speaker: str,
                              event_type: str) -> tuple[Optional[float], int, int]:
    """Estimate speaker speeches per calendar month from the transcripts table.

    Returns: (cadence_per_month, n_speeches_observed, n_distinct_months)
             cadence_per_month is None if corpus is too thin.
    """
    rows = conn.execute(
        """SELECT event_date FROM transcripts
           WHERE speaker=? AND event_type=?""",
        (speaker, event_type),
    ).fetchall()
    n_speeches = len(rows)
    if n_speeches == 0:
        return (None, 0, 0)
    months = set()
    for (ed,) in rows:
        try:
            d = dt.date.fromisoformat(str(ed)[:10])
            months.add((d.year, d.month))
        except (ValueError, TypeError):
            continue
    n_months = len(months)
    if n_months == 0:
        return (None, n_speeches, 0)
    return (n_speeches / n_months, n_speeches, n_months)


def term_mention_stats(conn: sqlite3.Connection, speaker: str, event_type: str,
                       term: str) -> tuple[int, int, int]:
    """For (speaker, event_type, term): return
        (n_speeches_with_term, total_mention_count, total_speeches)
    Reads from mentions table (per-transcript count) joined to transcripts.
    """
    rows = conn.execute(
        """SELECT m.mentioned, COALESCE(m.mention_count, 0)
           FROM mentions m
           JOIN transcripts t ON t.id = m.transcript_id
           WHERE t.speaker=? AND t.event_type=? AND m.term=?""",
        (speaker, event_type, term),
    ).fetchall()
    n_with = sum(1 for r in rows if r[0])
    total = sum(int(r[1] or 0) for r in rows)
    n_total = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE speaker=? AND event_type=?",
        (speaker, event_type),
    ).fetchone()[0]
    return (n_with, total, n_total)


# ── Prediction core ────────────────────────────────────────────────────────
@dataclass
class RollingWindowPrediction:
    """Full decomposition + status, used by both the brain and the audit log."""
    status:                      str       # 'ok' | 'skip_thin_cadence' | 'skip_thin_term' |
                                           # 'skip_cadence_outlier' | 'skip_no_corpus'
    speaker:                     str
    term:                        str
    threshold_n:                 int
    settle_date:                 dt.date
    days_remaining:              int
    total_days:                  int
    cadence_per_month:           Optional[float]
    n_speeches_observed:         int
    n_distinct_months:           int
    n_speeches_with_term:        int
    term_count_total:            int
    per_speech_rate:             Optional[float]
    expected_speeches_remaining: Optional[float]
    expected_total:              Optional[float]
    p_yes:                       Optional[float]
    confidence:                  Optional[float]
    skip_reason:                 Optional[str] = None


def predict_rolling(conn: sqlite3.Connection, *, speaker: str, event_type: str,
                    term: str, threshold_n: int, settle_date: dt.date,
                    today: Optional[dt.date] = None) -> RollingWindowPrediction:
    """Apply the decomposition + hard gate. Returns full prediction object."""
    today = today or dt.date.today()
    total_days = calendar.monthrange(settle_date.year, settle_date.month)[1]
    days_remaining = max(0, (settle_date - today).days)

    cadence, n_speeches, n_months = speaker_cadence_per_month(
        conn, speaker, event_type)

    base = RollingWindowPrediction(
        status="ok", speaker=speaker, term=term,
        threshold_n=threshold_n, settle_date=settle_date,
        days_remaining=days_remaining, total_days=total_days,
        cadence_per_month=cadence,
        n_speeches_observed=n_speeches,
        n_distinct_months=n_months,
        n_speeches_with_term=0, term_count_total=0,
        per_speech_rate=None,
        expected_speeches_remaining=None,
        expected_total=None,
        p_yes=None, confidence=None,
    )

    if cadence is None or n_speeches == 0:
        base.status = "skip_no_corpus"
        base.skip_reason = f"no_corpus(n={n_speeches})"
        return base

    if n_speeches < MIN_CADENCE_OBS:
        base.status = "skip_thin_cadence"
        base.skip_reason = f"n_speeches={n_speeches}<{MIN_CADENCE_OBS}"
        return base

    if not (SANITY_MIN_CADENCE <= cadence <= SANITY_MAX_CADENCE):
        base.status = "skip_cadence_outlier"
        base.skip_reason = (f"cadence={cadence:.2f}/mo outside "
                            f"[{SANITY_MIN_CADENCE}, {SANITY_MAX_CADENCE}]")
        return base

    n_with, total_count, n_corpus = term_mention_stats(
        conn, speaker, event_type, term)
    base.n_speeches_with_term = n_with
    base.term_count_total = total_count

    if n_with < MIN_TERM_OBS:
        base.status = "skip_thin_term"
        base.skip_reason = f"n_speeches_with_term={n_with}<{MIN_TERM_OBS}"
        return base

    # Per-speech rate of the TERM (Poisson λ for one speech)
    per_speech_rate = total_count / max(1, n_corpus)
    base.per_speech_rate = per_speech_rate

    # Expected speeches in the remaining window
    days_fraction = days_remaining / 30.0
    expected_speeches_remaining = cadence * days_fraction
    base.expected_speeches_remaining = expected_speeches_remaining

    # Compose: expected total mentions in the remaining window
    # (mentions-already-this-month omitted — see module docstring caveat)
    expected_total = expected_speeches_remaining * per_speech_rate
    base.expected_total = expected_total

    p = p_at_least_n(threshold_n, expected_total)
    # Confidence rises with both data depth AND distance from threshold
    depth_factor = min(1.0, n_with / 10.0) * min(1.0, n_speeches / 20.0)
    # Far from 0.5 = high confidence; near 0.5 = low
    edge_factor = 1.0 - 4.0 * p * (1.0 - p)
    base.p_yes = max(0.001, min(0.999, p))
    base.confidence = max(0.001, min(0.999, depth_factor * edge_factor))
    return base


# ── CLI: scrutiny report ───────────────────────────────────────────────────
def scrutiny_report(speaker: str = "trump", event_type: str = "trump_speech",
                    today: Optional[dt.date] = None) -> dict:
    """Run the gate against ALL active KXTRUMPSAY markets and report.

    This is the calibration-scrutiny entry point. Before wiring rolling-
    window into the auto-router, the operator runs this once and inspects:
      - the cadence estimate (vs reality)
      - 3 worked examples
      - how many of the ~150 KXTRUMPSAY markets actually pass the gate
    If <10 pass → recommend parking the domain until corpus grows.
    """
    today = today or dt.date.today()
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        cadence, n_speeches, n_months = speaker_cadence_per_month(
            conn, speaker, event_type)
        out = {
            "speaker":          speaker,
            "n_speeches":       n_speeches,
            "n_distinct_months": n_months,
            "cadence_per_month": None if cadence is None else round(cadence, 2),
            "hard_gates":       {
                "MIN_CADENCE_OBS":     MIN_CADENCE_OBS,
                "MIN_TERM_OBS":        MIN_TERM_OBS,
                "SANITY_MIN_CADENCE":  SANITY_MIN_CADENCE,
                "SANITY_MAX_CADENCE":  SANITY_MAX_CADENCE,
            },
        }

        # Pull live KXTRUMPSAY markets
        from engine.scanner import KalshiClient, classify_market
        c = KalshiClient()
        all_markets = c.get_mention_markets()
        candidates: list[tuple[dict, dict]] = []
        for m in all_markets:
            tk = (m.get("event_ticker", "") or "").upper()
            if "TRUMPSAY" not in tk:
                continue
            cl = classify_market(m)
            if cl.get("event_type") != "trump_speech":
                continue
            if not cl.get("term"):
                continue
            candidates.append((m, cl))

        # Apply gate to each. Settlement date inferred from close_time.
        passed: list[RollingWindowPrediction] = []
        skipped_thin_term = skipped_thin_cadence = skipped_outlier = 0
        for m, cl in candidates:
            close = (m.get("close_time") or "")[:10]
            try:
                settle = dt.date.fromisoformat(close)
            except ValueError:
                continue
            # Threshold N from title/rules — Kalshi mention-count markets
            # typically encode "N+ times" in the title. Default 1 if not
            # parseable (treated as "at least once" which most pass easily).
            threshold = _extract_threshold_n(m.get("title", "") or "", default=1)
            pred = predict_rolling(
                conn, speaker=speaker, event_type=event_type,
                term=cl["term"], threshold_n=threshold,
                settle_date=settle, today=today,
            )
            if pred.status == "ok":
                passed.append(pred)
            elif pred.status == "skip_thin_term":
                skipped_thin_term += 1
            elif pred.status == "skip_thin_cadence":
                skipped_thin_cadence += 1
            elif pred.status == "skip_cadence_outlier":
                skipped_outlier += 1

        # Three worked examples — pick smallest, middle, largest p_yes
        passed.sort(key=lambda p: p.p_yes or 0)
        examples: list[dict] = []
        if passed:
            picks = [passed[0]]
            if len(passed) >= 3:
                picks.append(passed[len(passed) // 2])
                picks.append(passed[-1])
            elif len(passed) == 2:
                picks.append(passed[-1])
            for p in picks:
                examples.append({
                    "term":                   p.term,
                    "threshold_n":            p.threshold_n,
                    "settle_date":            p.settle_date.isoformat(),
                    "days_remaining":         p.days_remaining,
                    "cadence_per_month":      round(p.cadence_per_month, 2),
                    "expected_speeches_rem":  round(p.expected_speeches_remaining, 2),
                    "per_speech_rate":        round(p.per_speech_rate, 3),
                    "expected_total":         round(p.expected_total, 2),
                    "p_yes":                  round(p.p_yes, 4),
                    "confidence":             round(p.confidence, 3),
                })

        out.update({
            "n_kxtrumpsay_total":         len(candidates),
            "n_passed_gate":              len(passed),
            "n_skip_thin_cadence":        skipped_thin_cadence,
            "n_skip_thin_term":           skipped_thin_term,
            "n_skip_cadence_outlier":     skipped_outlier,
            "worked_examples":            examples,
        })
        return out
    finally:
        conn.close()


def _extract_threshold_n(title: str, default: int = 1) -> int:
    """Crude threshold extractor from Kalshi market titles like
       'Will Trump say "X" at least 5 times this month?' """
    import re
    m = re.search(r"at\s+least\s+(\d+)", title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\+\s+times", title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return default


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--speaker", default="trump")
    ap.add_argument("--event-type", default="trump_speech")
    a = ap.parse_args()

    res = scrutiny_report(speaker=a.speaker, event_type=a.event_type)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"\n━━━ ROLLING-WINDOW SCRUTINY — {a.speaker} ━━━")
        print(f"  corpus:        n_speeches={res['n_speeches']}  "
              f"distinct_months={res['n_distinct_months']}")
        print(f"  cadence:       {res['cadence_per_month']} speeches/month")
        print(f"  hard_gates:    n_speeches≥{MIN_CADENCE_OBS}, n_term≥{MIN_TERM_OBS}, "
              f"cadence∈[{SANITY_MIN_CADENCE},{SANITY_MAX_CADENCE}]")
        print()
        print(f"  KXTRUMPSAY universe: {res['n_kxtrumpsay_total']} markets")
        print(f"    PASSED gate:           {res['n_passed_gate']}")
        print(f"    skip thin term:        {res['n_skip_thin_term']}")
        print(f"    skip thin cadence:     {res['n_skip_thin_cadence']}")
        print(f"    skip cadence outlier:  {res['n_skip_cadence_outlier']}")
        print()
        print("  WORKED EXAMPLES:")
        for ex in res["worked_examples"]:
            print(f"    term={ex['term']!r}  N≥{ex['threshold_n']}  "
                  f"days_rem={ex['days_remaining']}")
            print(f"      cadence={ex['cadence_per_month']}/mo × "
                  f"({ex['days_remaining']}/30)d = "
                  f"{ex['expected_speeches_rem']} speeches remaining")
            print(f"      per_speech_rate={ex['per_speech_rate']}  "
                  f"→ expected_total={ex['expected_total']}")
            print(f"      P(≥{ex['threshold_n']}) = {ex['p_yes']}   conf={ex['confidence']}")
        print()
        if res["n_passed_gate"] < 10:
            print(f"  🔴 RECOMMENDATION: PARK rolling-window domain. "
                  f"Only {res['n_passed_gate']} markets pass the gate.")
            print(f"     Corpus is structurally too thin for credible "
                  f"calibration at {res['cadence_per_month']} speeches/month.")
        else:
            print(f"  ✓ {res['n_passed_gate']} markets pass — domain may be "
                  f"wired with isolated Brier tracking.")
