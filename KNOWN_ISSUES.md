# Known Issues — non-urgent follow-ups

Items that don't block trading but should get cleaned up when convenient.

---

## corpus/auto_ingest._get_missing_quarters — future-quarter retries

**Filed:** 2026-05-12
**Priority:** cosmetic (no calibration impact)
**Effort:** ~10 LOC

**Symptom:** When `loop/auto_reingest` runs after a resolved earnings market, the cascading `auto_ingest(ticker)` call attempts to fetch quarters that haven't happened yet. Example from 2026-05-12 15:00 UTC reingest cron firing on TSLA:

```
AUTO-INGEST: TSLA | speaker=musk
TSLA Q2 2026: fetching...        ← hasn't happened (current quarter)
TSLA Q1 2026: fetching...        ← already in corpus
TSLA Q1 2025: fetching...
TSLA Q4 2024: fetching...
```

Result: `status=transcript_pending` because Motley Fool has no slug for the future quarters; the cron retries every 6h, racking up wasted HTTP traffic on the source.

**Root cause:** `_get_missing_quarters` computes a candidate list spanning `AUTO_INGEST_QUARTERS_BACK=8` quarters back from `date.today()` but doesn't gate on whether each quarter's reporting date has actually arrived (Q1 reports Apr-May, Q2 reports Jul-Aug, etc., per `_fetch_motley_fool`'s own `report_configs` map).

**Fix:** when generating the quarter candidate list, skip any quarter whose earliest reporting month is in the future relative to `today()`. Same `report_configs` table used by `_fetch_motley_fool` can be reused.

**Impact:** harmless (cron correctly status=transcript_pending and retries; no bad data ingested). Just noisy log + a couple extra Motley Fool HEADs every 6h. Worth fixing for log cleanliness and for the eventual "switch to a paid transcript API" decision (don't want to spam paid endpoints on quarters that don't exist).

---

(add new items below as they surface)
