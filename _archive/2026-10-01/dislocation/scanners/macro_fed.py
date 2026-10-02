"""Macro / Fed rate decision dislocation scanner.

EVENT PAIRS:
  Kalshi KXFEDDECISION-* binaries  ↔  CME ZQ (Fed funds) futures

WHY THIS IS THE WEDGE:
  - Both venues mature, deep, low fees.
  - Pair is spec-matched: ZQ settles on EFFR average for the contract month;
    Kalshi resolves on the FOMC target-range announcement. Resolver is
    deterministic (see fed_funds.py for the FedWatch decomposition).
  - Liquidity asymmetry: Kalshi prediction market has retail flow with
    weaker rate-decision priors than the rates desk consensus.
    Persistent 3-10pp dislocations have been observed in the wild.
  - Settlement is calendar-known (8 FOMC meetings/year), so basis risk
    is bounded and convergence date is certain.

DATA SOURCES (today):
  Kalshi: existing KalshiClient via /markets/{ticker}.
  CME ZQ: paid feed (CME DataMine, $200/mo) OR free CME daily settlements
          (https://www.cmegroup.com/markets/interest-rates/stirs/30-day-federal-fund.settlements.html).
          For now: read from a CSV the operator drops in data/zq_settles.csv,
          updated nightly by tools/dislocation_scan.py with the day's print.
          Live intraday upgrade later (see __upgrade_to_live note below).

UNIVERSE:
  Static map of (FOMC date → ZQ contract) for the next 4 meetings, plus
  the Kalshi market tickers for each rate-decision bucket. Operator
  refreshes the map quarterly (8 meetings/yr is low-effort).
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path
from typing import Optional

from ..config import DATA_DIR
from ..event_universe import (
    Domain,
    EventPair,
    Venue,
    VenueQuote,
)
from ..pricing.fed_funds import (
    FOMCContext,
    implied_prob_kalshi_question,
)
from ..pricing.kalshi_prob import fetch_kalshi_quote
from .base import DislocationScanner

_log = logging.getLogger(__name__)

ZQ_SETTLES_CSV = Path(DATA_DIR) / "zq_settles.csv"


# ── Static FOMC + ZQ + Kalshi mapping ────────────────────────────────────
# Operator updates quarterly. Dates from the Fed's published calendar.
# Each FOMC has multiple Kalshi binary markets — one per decision bucket.
# Bucket = post-FOMC target rate LOWER bound (decimal). Kalshi tickers
# are the actual market tickers; replace with real ones when wiring.
#
# As of 2026-05-09 the current target is hypothetically 5.25-5.50% (mid 5.375).
# FOMC dates 2026-2027 from Fed calendar.
FOMC_PAIRS_2026 = [
    {
        "fomc_date":  dt.date(2026, 6, 17),
        "zq_contract": "ZQM26",  # June 2026
        "buckets": {
            # decimal_lower → kalshi_ticker
            0.0500: "KXFEDDECISION-26JUN-CUT25",
            0.0525: "KXFEDDECISION-26JUN-HOLD",
            0.0550: "KXFEDDECISION-26JUN-HIKE25",
        },
    },
    {
        "fomc_date":  dt.date(2026, 7, 29),
        "zq_contract": "ZQN26",  # July 2026 (uses Aug delivery for July FOMC if month-end)
        "buckets": {
            0.0500: "KXFEDDECISION-26JUL-CUT25",
            0.0525: "KXFEDDECISION-26JUL-HOLD",
            0.0550: "KXFEDDECISION-26JUL-HIKE25",
        },
    },
    {
        "fomc_date":  dt.date(2026, 9, 16),
        "zq_contract": "ZQU26",  # September 2026
        "buckets": {
            0.0475: "KXFEDDECISION-26SEP-CUT50",
            0.0500: "KXFEDDECISION-26SEP-CUT25",
            0.0525: "KXFEDDECISION-26SEP-HOLD",
        },
    },
    {
        "fomc_date":  dt.date(2026, 11, 4),
        "zq_contract": "ZQX26",  # November 2026
        "buckets": {
            0.0475: "KXFEDDECISION-26NOV-CUT50",
            0.0500: "KXFEDDECISION-26NOV-CUT25",
            0.0525: "KXFEDDECISION-26NOV-HOLD",
        },
    },
]

# Operator updates these as the Fed actually moves.
CURRENT_TARGET_LOWER = 0.0525
CURRENT_TARGET_UPPER = 0.0550


# ── ZQ settlement-CSV reader (free, lagged by 1 day) ─────────────────────
def _read_zq_settles(csv_path: Path = ZQ_SETTLES_CSV) -> dict[str, float]:
    """Read most recent ZQ settlement prices from a CSV.

    Format:
        contract,settlement_price,settle_date
        ZQM26,94.750,2026-05-08
        ZQN26,94.715,2026-05-08
        ...

    Returns {contract: most_recent_price}. Empty dict if file missing.
    """
    if not csv_path.exists():
        _log.warning(f"ZQ settles file not found: {csv_path}")
        return {}
    out: dict[str, tuple[float, dt.date]] = {}
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                c = row["contract"]
                p = float(row["settlement_price"])
                d = dt.date.fromisoformat(row["settle_date"])
                if c not in out or out[c][1] < d:
                    out[c] = (p, d)
        return {c: p for c, (p, _) in out.items()}
    except Exception as e:
        _log.error(f"failed reading ZQ settles: {e}")
        return {}


# ── Scanner ──────────────────────────────────────────────────────────────
class MacroFedScanner(DislocationScanner):
    domain = Domain.MACRO_FED
    # FedWatch decomposition is mature, well-defined math; basis risk is
    # bounded to intra-month rate-cut surprises (rare). 0.95 prior.
    win_prob_prior: float = 0.95

    def __init__(
        self,
        kalshi_client,
        *,
        bankroll: float,
        deployed: float = 0.0,
        zq_settles_override: Optional[dict[str, float]] = None,
        fomc_pairs_override: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(bankroll=bankroll, deployed=deployed)
        self.kalshi = kalshi_client
        self._zq_settles = zq_settles_override or _read_zq_settles()
        self._fomc_pairs = fomc_pairs_override or FOMC_PAIRS_2026
        self._kalshi_quote_cache: dict[str, VenueQuote] = {}

    # ── pair generation ─────────────────────────────────────────────────
    def load_pairs(self) -> list[EventPair]:
        pairs: list[EventPair] = []
        for meet in self._fomc_pairs:
            fomc_date = meet["fomc_date"]
            zq        = meet["zq_contract"]
            for bucket_lower, kalshi_ticker in meet["buckets"].items():
                pairs.append(EventPair(
                    pair_id=f"fed-{fomc_date.isoformat()}-{int(bucket_lower*10000)}",
                    domain=Domain.MACRO_FED,
                    description=(
                        f"FOMC {fomc_date.isoformat()}: "
                        f"target {bucket_lower*100:.2f}-{(bucket_lower+0.0025)*100:.2f}%"
                    ),
                    venue_a=Venue.KALSHI,
                    market_a_id=kalshi_ticker,
                    venue_b=Venue.CME_FUTURES,
                    market_b_id=zq,
                    settle_at=dt.datetime.combine(fomc_date, dt.time(18, 0)),
                    meta={
                        "bucket_lower":   bucket_lower,
                        "current_lower":  CURRENT_TARGET_LOWER,
                        "current_upper":  CURRENT_TARGET_UPPER,
                        "all_buckets":    list(meet["buckets"].keys()),
                    },
                ))
        return pairs

    # ── quote fetching ──────────────────────────────────────────────────
    def fetch_quotes(
        self, pair: EventPair,
    ) -> tuple[Optional[VenueQuote], Optional[VenueQuote]]:
        # Side A — Kalshi
        if pair.market_a_id in self._kalshi_quote_cache:
            q_a = self._kalshi_quote_cache[pair.market_a_id]
        else:
            q_a = fetch_kalshi_quote(self.kalshi, pair.market_a_id)
            if q_a is not None:
                self._kalshi_quote_cache[pair.market_a_id] = q_a

        # Side B — CME ZQ implied probability for this bucket
        zq_price = self._zq_settles.get(pair.market_b_id)
        if zq_price is None:
            return q_a, None

        ctx = FOMCContext(
            current_target_lower=pair.meta["current_lower"],
            current_target_upper=pair.meta["current_upper"],
            fomc_date=pair.settle_at.date(),
            contract_month_start=pair.settle_at.date().replace(day=1),
            contract_month_end=_month_end(pair.settle_at.date()),
            decision_buckets=pair.meta["all_buckets"],
        )
        bucket = pair.meta["bucket_lower"]
        p_b = implied_prob_kalshi_question(zq_price, ctx, kalshi_target_rate=bucket)

        q_b = VenueQuote(
            venue=Venue.CME_FUTURES,
            market_id=pair.market_b_id,
            mid=p_b,
            timestamp=dt.datetime.utcnow(),
        )
        return q_a, q_b

    def score_basis_risk(self, pair: EventPair) -> float:
        # FedWatch decomposition is well-defined; basis risk mostly comes
        # from intra-month rate-cut surprises (rare, but possible). 0.92.
        return 0.92


def _month_end(d: dt.date) -> dt.date:
    """Last day of d's month."""
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    nxt = dt.date(d.year, d.month + 1, 1)
    return nxt - dt.timedelta(days=1)


# ── Self-test (offline, mocked Kalshi + ZQ data) ─────────────────────────
if __name__ == "__main__":
    class _MockKalshi:
        """Mocks KalshiClient.get_unauth → returns yes_bid/yes_ask cents."""
        def __init__(self, mids: dict[str, float]):
            self._mids = mids
        def get_unauth(self, path):
            ticker = path.rsplit("/", 1)[-1]
            mid = self._mids.get(ticker, 0.50)
            return {"market": {
                "yes_bid": int(mid * 100) - 1,
                "yes_ask": int(mid * 100) + 1,
            }}

    # Synthetic dislocation: Kalshi prices "cut to 5.00%" at 60%, but ZQM26
    # at 94.75 implies ~75% prob of a cut → 15pp Kalshi underprice.
    mock_mids = {
        "KXFEDDECISION-26JUN-CUT25":  0.60,
        "KXFEDDECISION-26JUN-HOLD":   0.35,
        "KXFEDDECISION-26JUN-HIKE25": 0.05,
        # other meetings stub to 0.50
    }
    mock_zq = {"ZQM26": 94.75, "ZQN26": 94.70, "ZQU26": 94.50, "ZQX26": 94.40}

    scanner = MacroFedScanner(
        kalshi_client=_MockKalshi(mock_mids),
        bankroll=1000,
        zq_settles_override=mock_zq,
    )
    candidates = scanner.scan(now=dt.datetime(2026, 5, 9))
    print(f"loaded {len(scanner.load_pairs())} pairs, scanned → {len(candidates)} candidates")
    for c in sorted(candidates, key=lambda x: -x.spread.edge_pp)[:6]:
        print(c.explain())

    # Expect the "cut25" bucket to flag (Kalshi 60 vs ZQ implied ~75).
    cut25 = [c for c in candidates if "CUT25" in c.pair.market_a_id and "26JUN" in c.pair.market_a_id]
    assert cut25, "should have a June cut25 candidate"
    print(f"June cut25: edge_pp={cut25[0].spread.edge_pp:.2f} actionable={cut25[0].actionable}")

    print("macro_fed self-test OK")
