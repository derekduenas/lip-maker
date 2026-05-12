# Known Issues — non-urgent follow-ups

Items that don't block trading but should get cleaned up when convenient.

---

## tools/argus_status.py — bankroll env-source bug

**Filed:** 2026-05-12
**Priority:** cosmetic (no trading impact)
**Effort:** ~5 LOC

**Symptom:** `python -m tools.argus_status` shows `bankroll=$1000  headroom=$-694.38` when the operator runs it manually (no systemd env), even though the scanner is correctly using `ARGUS_PAPER_BANKROLL=5000` from the systemd unit. Display says "over the cap" when the engine is actually well under.

**Root cause:** `tools/argus_status.py` reads `PAPER_BANKROLL` at module import time from `os.getenv("ARGUS_PAPER_BANKROLL") or os.getenv("ARGUS_BANKROLL", "1000")`. Manual shell invocations don't inherit the systemd unit's env, so it falls back to the $1000 default.

**Fix options:**
  1. Have `argus_status` read the *effective* bankroll from the most recent `argus_paper_positions` row's notes / config snapshot rather than from env.
  2. Persist the bankroll-in-use to a small `argus_config` table when the scanner runs, and have status read from there.
  3. Source from `/etc/default/lip-maker` so both systemd + shell pick it up.

Option 3 is the cleanest 5-LOC fix.

**Verification:** the actual engine deployment is correct (deploy=$1,694 = 33% of $5k, well under the 50% deployed cap). The bug is dashboard-only.

---

(add new items below as they surface)
