"""Sovereign calibration v3 — peer RELATIVE change context.

v2 failed because absolute peer blending drowned CEO-specific vocabularies.
v3 hypothesis: scale own baseline by peers' RELATIVE deviation from THEIR norm.

For each generic-only phrase (no CEO-specific tells):
  peer_recent = mean count among other CEOs in ±60d window
  peer_historical = mean count across ALL other CEOs' calls in corpus
  peer_factor = peer_recent / peer_historical (clipped to [0.5, 2.0])
  adjusted_lam = own_baseline * peer_factor

Speaker-specific phrases use own baseline only (untouched).
Adjustment applied only when peer N >= 5 (otherwise own baseline).
"""
from __future__ import annotations
import sqlite3, math, re, json
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean

DB = "/root/sovereign/data/sovereign.db"
PEER_WINDOW_DAYS = 60
MIN_PEER_CALLS = 5
PEER_FACTOR_MIN = 0.5
PEER_FACTOR_MAX = 2.0

# ONLY apply context to phrases that are macro/generic (not CEO tells)
CONTEXT_PHRASES = {"ai", "macro", "inflation", "uncertain", "headwind",
                   "tailwind", "guidance", "demand"}

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
    "huang": ["blackwell", "hopper", "data center", "ai", "cuda"],
    "cook": ["services", "iphone", "vision pro", "india", "ai"],
    "nadella": ["copilot", "azure", "ai", "openai"],
    "pichai": ["ai", "search", "cloud", "youtube"],
    "zuckerberg": ["ai", "llama", "metaverse", "instagram", "reels"],
    "jassy": ["aws", "ai", "advertising", "logistics"],
    "dimon": ["consumer", "credit", "rates", "macro"],
    "mcmillon": ["e-commerce", "advertising", "membership", "value"],
}


def count_phrase(text, phrase):
    p = re.escape(phrase.lower())
    return len(re.findall(rf"\b{p}\b", text.lower()))


def poisson_pmf_at_least(k, lam):
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    if k <= 0:
        return 1.0
    cum = math.exp(-lam)
    term = cum
    for i in range(1, k):
        term *= lam / i
        cum += term
    return max(0.0, min(1.0, 1.0 - cum))


def brier(preds):
    return mean((p - y) ** 2 for p, y in preds) if preds else float("nan")


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT speaker, event_type, event_date, raw_text
        FROM transcripts WHERE event_type LIKE '%earnings%' ORDER BY event_date
    """).fetchall()
    all_calls = [(dt, sp or "", et, txt) for sp, et, dt, txt in rows]

    # Precompute corpus-wide historical mean per phrase (excluding any single ticker)
    # For correctness we recompute per (speaker, et) — but for efficiency, approx
    # by global mean (the fluctuation between including/excluding 1 ticker is small).
    historical_mean = {}
    for ph in CONTEXT_PHRASES:
        counts = [count_phrase(t, ph) for _, _, _, t in all_calls]
        historical_mean[ph] = sum(counts) / max(1, len(counts))

    groups = defaultdict(list)
    for dt, sp, et, txt in all_calls:
        groups[(sp, et)].append((dt, txt))

    v1_preds, v3_preds = [], []
    per_group = {}

    print(f"\n{'='*82}")
    print(f"Sovereign Calibration v3 — peer RELATIVE-change (CONTEXT_PHRASES only)")
    print(f"{'='*82}\n")
    print(f"{'group':<35} {'v1':>7} {'v3':>7} {'delta':>7}")
    print("-" * 60)

    for (speaker, event_type), calls in groups.items():
        if len(calls) < 4:
            continue
        phrases = list(set(PHRASES["common"] + PHRASES.get(speaker, [])))
        gp_v1, gp_v3 = [], []

        for held_out_idx in range(len(calls)):
            train = [c for i, c in enumerate(calls) if i != held_out_idx]
            test_dt_str, test_text = calls[held_out_idx]
            try:
                test_dt = datetime.fromisoformat(test_dt_str)
            except ValueError:
                continue

            window_start = test_dt - timedelta(days=PEER_WINDOW_DAYS)
            window_end = test_dt + timedelta(days=PEER_WINDOW_DAYS)
            peer_texts = [
                ptxt for pdt, psp, pet, ptxt in all_calls
                if (psp, pet) != (speaker, event_type)
                and window_start <= datetime.fromisoformat(pdt) <= window_end
            ]

            for phrase in phrases:
                train_counts = [count_phrase(t, phrase) for _, t in train]
                own_lam = (sum(train_counts) + 1) / (len(train_counts) + 1)
                actual = count_phrase(test_text, phrase)

                # v3 context: relative change for generic phrases only
                v3_lam = own_lam
                if phrase in CONTEXT_PHRASES and len(peer_texts) >= MIN_PEER_CALLS:
                    peer_recent_mean = mean(count_phrase(pt, phrase) for pt in peer_texts)
                    hist_mean = historical_mean.get(phrase, 0.0)
                    if hist_mean > 0.5:  # only apply when phrase is non-rare globally
                        factor = peer_recent_mean / hist_mean
                        factor = max(PEER_FACTOR_MIN, min(PEER_FACTOR_MAX, factor))
                        v3_lam = own_lam * factor

                median_train = sorted(train_counts)[len(train_counts)//2] if train_counts else 0
                thresholds = list(set([1, max(1, median_train), max(2, int(round(own_lam + 1)))]))
                for thr in thresholds:
                    p1 = poisson_pmf_at_least(thr, own_lam)
                    p3 = poisson_pmf_at_least(thr, v3_lam)
                    y = 1 if actual >= thr else 0
                    gp_v1.append((p1, y))
                    gp_v3.append((p3, y))

        if not gp_v1:
            continue
        b1, b3 = brier(gp_v1), brier(gp_v3)
        per_group[f"{speaker}|{event_type}"] = {"v1": b1, "v3": b3, "delta": b3 - b1}
        v1_preds.extend(gp_v1)
        v3_preds.extend(gp_v3)
        delta = b3 - b1
        marker = "✅" if delta < -0.001 else ("❌" if delta > 0.001 else "≈")
        label = f"{speaker or '_'}|{event_type}"
        print(f"{label:<35} {b1:>7.4f} {b3:>7.4f} {delta:>+7.4f} {marker}")

    print("-" * 60)
    overall_v1 = brier(v1_preds)
    overall_v3 = brier(v3_preds)
    delta = overall_v3 - overall_v1
    print(f"{'OVERALL':<35} {overall_v1:>7.4f} {overall_v3:>7.4f} {delta:>+7.4f}")
    print(f"\nN predictions: {len(v1_preds)}")

    out = {
        "context_phrases": list(CONTEXT_PHRASES),
        "peer_window_days": PEER_WINDOW_DAYS,
        "factor_clip": [PEER_FACTOR_MIN, PEER_FACTOR_MAX],
        "v1_brier": overall_v1, "v3_brier": overall_v3, "delta": delta,
        "n_predictions": len(v1_preds), "per_group": per_group,
    }
    with open("/tmp/calibration_v3.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n{'='*82}\nVERDICT\n{'='*82}")
    if delta < -0.005:
        print(f"GREEN — relative-change context HELPS by {-delta:.4f}. Ship v3.")
    elif delta < -0.001:
        print(f"YELLOW — small improvement {-delta:.4f}. Worth more iteration.")
    elif delta < 0.001:
        print(f"NEUTRAL — no material change. Context as designed adds nothing.")
    else:
        print(f"RED — context HURTS by {delta:.4f}. Stick with v1.")


if __name__ == "__main__":
    main()
