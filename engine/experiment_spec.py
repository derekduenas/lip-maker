"""Pre-registration for the entry-cutoff paper experiment. FROZEN 2026-09-21.

Fixed BEFORE any evaluation, so that neither the market selection nor the
winning policy can be chosen after seeing results. The fingerprint is
recorded with every result file; if these constants change, the fingerprint
changes and a later reader can tell.

Selection
---------
From the live universe of enrolled, currently-open incentive programs:

  * rank by pool RATE, `period_reward_usd / period_seconds` ($/sec) — not by
    total pool, which favours long-dated programs whose per-second rate
    rounds to nothing;
  * require the MARKET to be open for trading right now, i.e.
    open_time <= now < close_time. Program start, market open and market
    close are three different clocks: the highest-rate programs observed
    were for windows ~20 hours ahead, whose markets had not opened and
    whose books were therefore empty;
  * require a two-sided book at capture time (a one-sided book cannot carry
    a LIP quote at all);
  * STRATIFY by the market's own trading window, `close_time - open_time`:
        SHORT   duration <  SHORT_MAX_MIN
        LONG    duration >= SHORT_MAX_MIN
    and take the top N_PER_STRATUM of each by pool rate.

Stratifying is the point of the experiment: the cutoff policies only differ
where the market is short, so a sample of long-dated markets alone could not
distinguish them, and a sample of short markets alone could not show whether
the change costs anything elsewhere.

Arms
----
Every arm sees the SAME captured market stream and starts with its own
independent ACCOUNT_USD. Differences between arms are therefore attributable
to the policy, not to different data or a shared balance.

Measurement
-----------
Reported per arm, never pooled: eligible opportunities and refusal reasons,
quotes, modelled fills, qualified resting time, trading P&L after fees,
reward estimates (separately from payments, of which there are none),
inventory and exit costs, capital usage, and the data gaps.

No fill is manufactured to demonstrate activity. An arm that quotes nothing
reports nothing, and that is a result.
"""
from __future__ import annotations

import hashlib
import json

ACCOUNT_USD = 5000.0
SHORT_MAX_MIN = 30.0
N_PER_STRATUM = 6
REQUIRE_TWO_SIDED_BOOK = True
REQUIRE_MARKET_OPEN_NOW = True
RANK_BY = "pool_rate_usd_per_sec"

STRATUM_SHORT = "short_window"
STRATUM_LONG = "long_dated"
STRATA = (STRATUM_SHORT, STRATUM_LONG)


def stratum_for(duration_min):
    """Which stratum a market belongs to, or None when its duration is
    unknown — unknown-duration markets are EXCLUDED rather than guessed
    into a bucket."""
    if duration_min is None or duration_min <= 0:
        return None
    return STRATUM_SHORT if duration_min < SHORT_MAX_MIN else STRATUM_LONG


def spec_fingerprint() -> str:
    blob = json.dumps({
        "account_usd": ACCOUNT_USD, "short_max_min": SHORT_MAX_MIN,
        "n_per_stratum": N_PER_STRATUM,
        "require_two_sided_book": REQUIRE_TWO_SIDED_BOOK,
        "require_market_open_now": REQUIRE_MARKET_OPEN_NOW,
        "rank_by": RANK_BY, "strata": list(STRATA)}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def describe() -> dict:
    from engine.entry_cutoff import describe as cutoff_describe
    return {
        "spec_fingerprint": spec_fingerprint(),
        "frozen_utc": "2026-09-21",
        "account_usd_per_arm": ACCOUNT_USD,
        "selection": {
            "rank_by": RANK_BY,
            "require_two_sided_book": REQUIRE_TWO_SIDED_BOOK,
            "require_market_open_now": REQUIRE_MARKET_OPEN_NOW,
            "strata": {
                STRATUM_SHORT: f"market duration < {SHORT_MAX_MIN} min",
                STRATUM_LONG: f"market duration >= {SHORT_MAX_MIN} min",
            },
            "n_per_stratum": N_PER_STRATUM,
            "unknown_duration": "excluded, not bucketed",
        },
        "cutoff_policies": cutoff_describe(),
        "matching": ("every arm replays the SAME captured book and trade "
                     "stream, each with its own independent account"),
    }
