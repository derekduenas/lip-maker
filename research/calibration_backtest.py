"""Sovereign calibration backtest — Task #196 MVP.

Methodology validation BEFORE bulk-ingesting more transcripts.
For each (speaker, event_type) with ≥4 transcripts:
  - Leave-one-out cross-validation
  - Forecast: P(phrase appears ≥N times) using Poisson with Laplace-smoothed rate
  - Score: Brier on binary predictions

Output: per-ticker Brier + overall + reliability bins.
If overall Brier <0.20 → green light to bulk-ingest 8-12 high-coverage tickers.
If >0.25 → fix methodology before adding data.
"""
from __future__ import annotations
import sqlite3, math, re, json
from collections import Counter, defaultdict
from statistics import mean

DB = "/root/sovereign/data/sovereign.db"

# Generic earnings phrases + speaker-specific tells
PHRASES = {
    "common": ["ai", "guidance", "headwind", "tailwind", "macro", "uncertain",
               "growth", "margin", "demand", "inflation"],
    "musk": ["robotaxi", "fsd", "energy", "autonomy", "supercharger", "optimus"],
    "subramaniam": ["network", "yields", "ground", "express", "dimensional"],
    "mcd": ["comparable sales", "drive-thru", "value", "loyalty", "delivery"],
    "lyft": ["rides", "drivers", "active riders", "bookings", "ev"],
    "brown": ["auto", "consumer", "deposits", "credit"],
    "ortberg": ["737", "787", "defense", "supply chain", "production"],
    "rogers": ["loans", "deposits", "credit", "wealth"],
    "lip_bu": ["foundry", "process", "ai", "data center"],
    "powell": ["inflation", "unemployment", "data dependent", "patient", "transitory"],
}


def tokenize(text: str) -> str:
    return text.lower()


def count_phrase(text: str, phrase: str) -> int:
    """Count exact phrase occurrences (case-insensitive, word boundaries)."""
    p = re.escape(phrase.lower())
    return len(re.findall(rf"\b{p}\b", text.lower()))


def poisson_pmf_at_least(k: int, lam: float) -> float:
    """P(X >= k) for Poisson(lam). Numerical-safe for our scale."""
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    # P(X >= k) = 1 - P(X < k) = 1 - sum_{i=0..k-1} e^-lam * lam^i / i!
    if k <= 0:
        return 1.0
    lo = math.exp(-lam)
    cum = lo
    term = lo
    for i in range(1, k):
        term *= lam / i
        cum += term
    return max(0.0, min(1.0, 1.0 - cum))


def brier(predictions: list[tuple[float, int]]) -> float:
    """Mean squared error on binary outcomes given probabilities."""
    if not predictions:
        return float("nan")
    return mean((p - y) ** 2 for p, y in predictions)


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT speaker, event_type, event_date, raw_text
        FROM transcripts
        WHERE event_type LIKE '%earnings%'
        ORDER BY event_date
    """).fetchall()

    # Group by (speaker, event_type)
    groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for sp, et, dt, text in rows:
        groups[(sp or "", et)].append((dt, text))

    all_predictions = []  # (p, y) across all backtests
    per_group_brier = {}
    print(f"\n{'='*80}\nSovereign Calibration Backtest — methodology validation")
    print(f"{'='*80}\n")
    print(f"{'group':<35} {'N':>3} {'preds':>6} {'brier':>7} {'naive':>7}")
    print("-" * 65)

    for (speaker, event_type), calls in groups.items():
        if len(calls) < 4:
            continue
        # Phrase set for this speaker
        phrases = list(set(PHRASES["common"] + PHRASES.get(speaker, [])))

        group_preds = []
        for held_out_idx in range(len(calls)):
            train = [c for i, c in enumerate(calls) if i != held_out_idx]
            test_dt, test_text = calls[held_out_idx]

            for phrase in phrases:
                # Baseline rate from training set (Laplace smoothed: +1/+0)
                train_counts = [count_phrase(t, phrase) for _, t in train]
                lam = (sum(train_counts) + 1) / (len(train_counts) + 1)
                actual = count_phrase(test_text, phrase)

                # Generate 3 binary predictions per phrase: ≥1, ≥median(train), ≥mean+1
                median_train = sorted(train_counts)[len(train_counts)//2] if train_counts else 0
                thresholds = [1, max(1, median_train), max(2, int(round(lam + 1)))]
                for thr in set(thresholds):
                    p = poisson_pmf_at_least(thr, lam)
                    y = 1 if actual >= thr else 0
                    group_preds.append((p, y))

        if not group_preds:
            continue
        b = brier(group_preds)
        # Naive baseline: predict 0.5 for everything
        naive = mean((0.5 - y) ** 2 for _, y in group_preds)
        per_group_brier[(speaker, event_type)] = (b, len(group_preds), naive)
        all_predictions.extend(group_preds)
        label = f"{speaker or '_'}|{event_type}"
        print(f"{label:<35} {len(calls):>3} {len(group_preds):>6} {b:>7.4f} {naive:>7.4f}")

    print("-" * 65)
    overall = brier(all_predictions)
    naive_overall = mean((0.5 - y) ** 2 for _, y in all_predictions)
    print(f"{'OVERALL':<35} {'':<3} {len(all_predictions):>6} {overall:>7.4f} {naive_overall:>7.4f}")
    print(f"\nBrier interpretation: 0.0=perfect, 0.25=random (50/50), 1.0=always wrong")
    print(f"Edge over naive: {(naive_overall - overall):+.4f}")

    # Reliability bins
    print(f"\n{'='*80}\nReliability bins (calibration check):")
    print(f"{'predicted':<15} {'actual':<10} {'n':>5}")
    bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
    for lo, hi in bins:
        bin_preds = [(p, y) for p, y in all_predictions if lo <= p < hi]
        if not bin_preds:
            continue
        avg_pred = mean(p for p, _ in bin_preds)
        avg_actual = mean(y for _, y in bin_preds)
        print(f"{lo:.1f}-{hi:.1f}        {avg_actual:>5.3f}      {len(bin_preds):>5}")

    # Save to JSON for memory
    out = {
        "overall_brier": overall,
        "overall_naive_brier": naive_overall,
        "edge_over_naive": naive_overall - overall,
        "n_predictions": len(all_predictions),
        "per_group": {f"{s}|{e}": {"brier": b, "n": n, "naive": nv}
                      for (s, e), (b, n, nv) in per_group_brier.items()}
    }
    with open("/tmp/calibration_backtest.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved /tmp/calibration_backtest.json")

    print(f"\n{'='*80}\nVERDICT")
    print(f"{'='*80}")
    if overall < 0.20:
        print(f"GREEN — Brier {overall:.3f} < 0.20. Methodology calibrates. Ship bulk ingest.")
    elif overall < 0.25:
        print(f"YELLOW — Brier {overall:.3f} in 0.20-0.25 range. Marginal. Consider improvements before scaling.")
    else:
        print(f"RED — Brier {overall:.3f} >= 0.25. Methodology not calibrated yet. Fix before adding data.")


if __name__ == "__main__":
    main()
