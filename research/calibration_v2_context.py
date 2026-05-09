"""Sovereign calibration v2 — adds Bayesian context blend.

Adjustment: instead of pure own-baseline Poisson, blend with peer-mention
rate from OTHER CEOs' calls within 60-day window of held-out call.

Hypothesis: contemporaneous peer signal carries info about macro/zeitgeist
("AI" frequency rises everywhere when AI cycle is hot). If true, Brier drops.

Methodology:
  adjusted_rate = (1 - alpha) * own_baseline + alpha * peer_recent
  alpha = 0.30 (start conservative; sweep later if positive)

If peer signal sparse (<3 peer calls in window), fall back to own baseline.

Compare to v1 backtest. Report Brier delta per ticker + overall.
"""
from __future__ import annotations
import sqlite3, math, re, json
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean

DB = "/root/sovereign/data/sovereign.db"
PEER_WINDOW_DAYS = 60
ALPHA_PEER = 0.30  # weight on peer signal vs own baseline
MIN_PEER_CALLS = 3  # below this, fall back to own baseline

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


def count_phrase(text: str, phrase: str) -> int:
    p = re.escape(phrase.lower())
    return len(re.findall(rf"\b{p}\b", text.lower()))


def poisson_pmf_at_least(k: int, lam: float) -> float:
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


def brier(predictions):
    if not predictions:
        return float("nan")
    return mean((p - y) ** 2 for p, y in predictions)


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT speaker, event_type, event_date, raw_text
        FROM transcripts WHERE event_type LIKE '%earnings%'
        ORDER BY event_date
    """).fetchall()

    # Index all (date, speaker, event_type, text) for peer lookup
    all_calls = [(dt, sp or "", et, txt) for sp, et, dt, txt in rows]

    groups = defaultdict(list)
    for dt, sp, et, txt in all_calls:
        groups[(sp, et)].append((dt, txt))

    v1_preds, v2_preds = [], []
    per_group = {}

    print(f"\n{'='*82}")
    print(f"Sovereign Calibration v2 — peer-context blend (alpha={ALPHA_PEER})")
    print(f"{'='*82}\n")
    print(f"{'group':<35} {'v1':>7} {'v2':>7} {'delta':>7}")
    print("-" * 60)

    for (speaker, event_type), calls in groups.items():
        if len(calls) < 4:
            continue
        phrases = list(set(PHRASES["common"] + PHRASES.get(speaker, [])))
        gp_v1, gp_v2 = [], []

        for held_out_idx in range(len(calls)):
            train = [c for i, c in enumerate(calls) if i != held_out_idx]
            test_dt_str, test_text = calls[held_out_idx]
            try:
                test_dt = datetime.fromisoformat(test_dt_str)
            except ValueError:
                continue

            # Peer calls within ±60 days of test_dt, EXCLUDING this (speaker, et)
            window_start = test_dt - timedelta(days=PEER_WINDOW_DAYS)
            window_end = test_dt + timedelta(days=PEER_WINDOW_DAYS)
            peer_calls = []
            for pdt, psp, pet, ptxt in all_calls:
                if (psp, pet) == (speaker, event_type):
                    continue
                try:
                    pdt_dt = datetime.fromisoformat(pdt)
                except ValueError:
                    continue
                if window_start <= pdt_dt <= window_end:
                    peer_calls.append(ptxt)

            for phrase in phrases:
                # Own baseline
                train_counts = [count_phrase(t, phrase) for _, t in train]
                own_lam = (sum(train_counts) + 1) / (len(train_counts) + 1)
                actual = count_phrase(test_text, phrase)

                # v2 peer-blend
                if len(peer_calls) >= MIN_PEER_CALLS:
                    peer_counts = [count_phrase(pt, phrase) for pt in peer_calls]
                    peer_lam = (sum(peer_counts) + 1) / (len(peer_counts) + 1)
                    v2_lam = (1 - ALPHA_PEER) * own_lam + ALPHA_PEER * peer_lam
                else:
                    v2_lam = own_lam

                median_train = sorted(train_counts)[len(train_counts)//2] if train_counts else 0
                thresholds = list(set([1, max(1, median_train), max(2, int(round(own_lam + 1)))]))
                for thr in thresholds:
                    p1 = poisson_pmf_at_least(thr, own_lam)
                    p2 = poisson_pmf_at_least(thr, v2_lam)
                    y = 1 if actual >= thr else 0
                    gp_v1.append((p1, y))
                    gp_v2.append((p2, y))

        if not gp_v1:
            continue
        b1, b2 = brier(gp_v1), brier(gp_v2)
        per_group[f"{speaker}|{event_type}"] = {"v1": b1, "v2": b2, "n": len(gp_v1),
                                                  "delta": b2 - b1}
        v1_preds.extend(gp_v1)
        v2_preds.extend(gp_v2)
        delta = b2 - b1
        marker = "✅" if delta < 0 else ("❌" if delta > 0.005 else "≈")
        label = f"{speaker or '_'}|{event_type}"
        print(f"{label:<35} {b1:>7.4f} {b2:>7.4f} {delta:>+7.4f} {marker}")

    print("-" * 60)
    overall_v1 = brier(v1_preds)
    overall_v2 = brier(v2_preds)
    delta_overall = overall_v2 - overall_v1
    print(f"{'OVERALL':<35} {overall_v1:>7.4f} {overall_v2:>7.4f} {delta_overall:>+7.4f}")
    print(f"\nN predictions: {len(v1_preds)}")
    print(f"Edge improvement: {-delta_overall:+.4f} (positive = v2 better)")

    out = {
        "alpha_peer": ALPHA_PEER,
        "peer_window_days": PEER_WINDOW_DAYS,
        "v1_brier": overall_v1,
        "v2_brier": overall_v2,
        "delta": delta_overall,
        "n_predictions": len(v1_preds),
        "per_group": per_group,
    }
    with open("/tmp/calibration_v2.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n{'='*82}\nVERDICT")
    print(f"{'='*82}")
    if delta_overall < -0.005:
        print(f"GREEN — peer-context blend HELPS by {-delta_overall:.4f}. Ship v2.")
    elif delta_overall < 0:
        print(f"YELLOW — marginal improvement {-delta_overall:.4f}. Not worth complexity yet.")
    else:
        print(f"RED — peer blend HURTS by {delta_overall:.4f}. Stick with v1 baseline.")


if __name__ == "__main__":
    main()
