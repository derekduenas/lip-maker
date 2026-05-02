"""Adaptive quote sizing — scale our quote to target a specific share of the
LIP snapshot score, using observed book competition.

Core insight (from passive-observer data):
  - KXCOPPERD: total_qualifying_yes ~73, ~116. At 25 contracts we got 26% share.
  - KXTRUMPPHOTO: total ~113, ~330. 25 contracts → 12.8% share.
  - KXCAPRIMARY: total ~3,000–8,000. 25 contracts → ~0.3-0.8% share (wasted).

Fix: size dynamically based on measured competition to hit target share band.

Math:
  Our score at best price: our_size × DiscountFactor^0 = our_size × 1.0 = our_size
  Total qualifying score (without us): S_total_other
  Our share: our_size / (our_size + S_total_other) = target_share

Solve for our_size:
  our_size = S_total_other × target_share / (1 - target_share)

For target_share = 0.25: our_size = S_total × 0.33 (about 1/3 of existing qual)
For target_share = 0.30: our_size = S_total × 0.43

Floor at MIN_QUOTE_SIZE_CONTRACTS. Cap by MAX_GROSS_PER_MARKET_USD.

Observation window: rolling last N valid snapshots per market. Use EWMA so
recent changes weight more than stale history.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from config import settings


@dataclass
class MarketStats:
    """Rolling stats per market for one side."""
    # EWMA of qualifying score EXCLUDING our contribution
    ema_qual_score: float = 0.0
    # Simple rolling mean over last N snapshots
    recent_window: deque = None
    samples:       int = 0
    last_seen_ts:  float = 0.0

    def __post_init__(self):
        if self.recent_window is None:
            self.recent_window = deque(maxlen=60)  # 60 snapshots ≈ 1 minute

    def update(self, qual_score_excluding_us: float, ts: float):
        """Fold a new observation into rolling stats."""
        self.recent_window.append(qual_score_excluding_us)
        self.samples += 1
        # EWMA with alpha=0.1 (slow adaptation — 10-sample half-life)
        alpha = 0.1 if self.samples > 1 else 1.0
        self.ema_qual_score = alpha * qual_score_excluding_us + (1 - alpha) * self.ema_qual_score
        self.last_seen_ts = ts

    def estimated_competition(self) -> float:
        """Our best estimate of what we're competing against."""
        # Prefer the rolling window mean if we have enough samples
        if len(self.recent_window) >= 10:
            return sum(self.recent_window) / len(self.recent_window)
        # Fall back to EWMA
        return self.ema_qual_score


class AdaptiveSizer:
    """Per-market size computer. Call update() after each snapshot score,
    then size() to get the desired contract count for the next quote."""

    def __init__(self, target_share: float = 0.25):
        self.target_share = target_share
        self.yes_stats: dict[str, MarketStats] = defaultdict(MarketStats)
        self.no_stats:  dict[str, MarketStats] = defaultdict(MarketStats)
        # 2026-05-02 PREDATOR K4: per-market target overrides from realized
        # share over last 7d. Falls back to global self.target_share when
        # not present. Refreshed hourly via update_target_shares_from_db().
        self.per_market_target: dict[str, float] = {}

    def get_target_share(self, market_ticker: str) -> float:
        """Per-market target if known, else global default."""
        return self.per_market_target.get(market_ticker, self.target_share)

    def update_target_shares_from_db(self, db_path: str, days: int = 7,
                                      lo: float = 0.10, hi: float = 0.50) -> int:
        """Pull rolling realized share per market from lip_snapshots.

        For each market with ≥10 valid snapshots in the window, compute
        median(our_score / total_score) and clamp to [lo, hi]. Sets
        self.per_market_target so size_for() respects realized capacity.

        Returns the number of markets updated. Called hourly from runner.
        """
        import sqlite3
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
        except Exception:
            return 0
        try:
            rows = conn.execute(
                f"""SELECT market_ticker,
                           our_score * 1.0 / total_score AS share
                   FROM lip_snapshots
                   WHERE total_score > 0
                     AND snapshot_valid = 1
                     AND datetime(captured_at) > datetime('now','-{int(days)} days')
                """
            ).fetchall()
        except Exception:
            conn.close()
            return 0
        conn.close()

        # Group by ticker, compute median
        from collections import defaultdict as _dd
        per_mkt: dict[str, list[float]] = _dd(list)
        for tkr, share in rows:
            if tkr and share is not None:
                per_mkt[tkr].append(float(share))

        updated = 0
        for tkr, shares in per_mkt.items():
            if len(shares) < 10:
                continue
            shares_sorted = sorted(shares)
            mid = shares_sorted[len(shares_sorted) // 2]
            self.per_market_target[tkr] = max(lo, min(hi, mid))
            updated += 1
        return updated

    def observe(
        self,
        market_ticker: str,
        yes_total_qual: float,
        no_total_qual: float,
        our_yes_contribution: float = 0.0,
        our_no_contribution: float = 0.0,
        ts: float = 0.0,
    ):
        """Record one snapshot observation.

        `yes_total_qual` is the TOTAL qualifying score on yes side INCLUDING us
        (if we were quoting). `our_yes_contribution` is our piece of that total.
        The "competition" is total - ours.
        """
        import time as _time
        ts = ts or _time.time()
        self.yes_stats[market_ticker].update(
            max(0.0, yes_total_qual - our_yes_contribution), ts,
        )
        self.no_stats[market_ticker].update(
            max(0.0, no_total_qual - our_no_contribution), ts,
        )

    def size_for(self, market_ticker: str, side: str,
                 target_size_program: int) -> int:
        """Compute our quote size for one side of one market.

        Args:
            market_ticker: which market
            side: "yes" or "no"
            target_size_program: the LIP TargetSize parameter (needed for
                minimum-size qualification)

        Returns a contract count to quote. Falls back to DEFAULT when
        we don't have observations yet.
        """
        stats = self.yes_stats[market_ticker] if side == "yes" else self.no_stats[market_ticker]
        if stats.samples < 3:
            # Not enough data yet — use conservative default
            return max(
                settings.MIN_QUOTE_SIZE_CONTRACTS,
                min(
                    settings.DEFAULT_QUOTE_SIZE_CONTRACTS,
                    int(target_size_program * settings.QUOTE_SIZE_AS_FRACTION_OF_TARGET),
                ),
            )

        s_competition = stats.estimated_competition()
        # Solve: our_size / (our_size + S) = target_share
        #        our_size = S × target_share / (1 - target_share)
        # K4: use per-market realized target if available, else global default
        target_share = self.get_target_share(market_ticker)
        if target_share >= 0.99:
            computed = 1e6
        else:
            computed = s_competition * target_share / (1.0 - target_share)
        computed_size = int(round(computed))

        # Floor at MIN_QUOTE_SIZE
        size = max(settings.MIN_QUOTE_SIZE_CONTRACTS, computed_size)
        # Don't exceed 5x the MIN_QUOTE_SIZE just to capture big shares —
        # defensive cap to avoid ballooning position on misjudged competition.
        size = min(size, settings.MIN_QUOTE_SIZE_CONTRACTS * 20)
        return size

    def report(self) -> dict:
        out = {}
        for tkr in set(self.yes_stats.keys()) | set(self.no_stats.keys()):
            out[tkr] = {
                "yes_competition_ema": round(self.yes_stats[tkr].ema_qual_score, 1),
                "no_competition_ema":  round(self.no_stats[tkr].ema_qual_score, 1),
                "yes_samples": self.yes_stats[tkr].samples,
                "no_samples":  self.no_stats[tkr].samples,
                "yes_recent_avg": round(
                    sum(self.yes_stats[tkr].recent_window) / max(1, len(self.yes_stats[tkr].recent_window)),
                    1
                ) if self.yes_stats[tkr].recent_window else 0,
                "no_recent_avg": round(
                    sum(self.no_stats[tkr].recent_window) / max(1, len(self.no_stats[tkr].recent_window)),
                    1
                ) if self.no_stats[tkr].recent_window else 0,
            }
        return out


def _self_test():
    """Validate sizer matches target share."""
    import time
    az = AdaptiveSizer(target_share=0.25)
    # Simulate: a thin market (competition ~100) and a thick one (~3000)
    # Record 20 snapshots to warm up
    for i in range(20):
        az.observe("KX-THIN-MKT", yes_total_qual=100 + i, no_total_qual=120, ts=time.time())
        az.observe("KX-THICK-MKT", yes_total_qual=3000 + i*10, no_total_qual=3200, ts=time.time())

    # Size for thin: expect ~33 contracts (100 * 0.25/0.75 = 33)
    s_thin = az.size_for("KX-THIN-MKT", "yes", target_size_program=300)
    # Size for thick: expect 1000 capped to min*20 = 200
    s_thick = az.size_for("KX-THICK-MKT", "yes", target_size_program=2500)

    print(f"thin market:  size={s_thin:3d} (expected ~33)")
    print(f"thick market: size={s_thick:3d} (should be capped; ideal 1000, capped to 200)")

    assert 20 <= s_thin <= 45, f"thin size out of band: {s_thin}"
    assert s_thick >= 100, f"thick size too small: {s_thick}"

    report = az.report()
    print(f"report: {len(report)} markets tracked")
    for tkr, stats in report.items():
        print(f"  {tkr}: {stats}")

    print("adaptive_sizer self-test PASSED")


if __name__ == "__main__":
    _self_test()
