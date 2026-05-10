"""Per-brain exposure caps.

PHASE 1 stub — equal-slice allocation across brain roadmap target (11 brains).
Real impl ships before any brain trades live.
"""
from __future__ import annotations

# Roadmap target — keep in sync with project plan.
N_BRAINS_TARGET = 11


def per_brain_headroom(
    brain_id:         str,
    bankroll:         float,
    current_exposure: float,
) -> float:
    """Headroom available to this brain right now.

    Phase 1: equal slices across N_BRAINS_TARGET. Refined later to weight
    proven brains higher.
    """
    slice_max = bankroll / max(1, N_BRAINS_TARGET)
    return max(0.0, slice_max - current_exposure)


if __name__ == "__main__":
    h = per_brain_headroom("music", bankroll=1100, current_exposure=20)
    print(f"headroom for music brain (bankroll=$1100, exposure=$20): ${h:.2f}")
    assert abs(h - 80.0) < 1e-6, h
    print("OK limits")
