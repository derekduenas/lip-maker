"""Observed trade flow per market, used to estimate how often we get filled.

The problem this solves
-----------------------
engine.quote_economics needs `expected_fills_per_horizon`. Until a market has
filled us we have no fill history, so the module declared it unknown and
priced it at one full fill of the whole quote. That placeholder turned out to
be the single term deciding every quote: at a qualifying size of ~1,000
contracts it charges a complete round-trip fee (tens of dollars) against a
daily reward of a few dollars, so every market is rejected — and because we
never quote, we never learn the real rate. A circular refusal.

The fix is to stop guessing and measure something we can actually see. Kalshi
publishes every trade on a market. Volume that traded is an upper bound on
volume that could have hit a resting order of ours, so:

    expected fills of a `size` quote over `horizon`
        ~= observed contracts/sec x horizon / size

is an OVERESTIMATE of how often we are filled (it credits every trade in the
market to our level, ignores queue position, and ignores that half the flow
hits the other side). Overestimating fills overstates fees and inventory
cost, which is the conservative direction for a decision about whether to
quote at all.

It is still an estimate, and it is labelled one: `measured()` is False until
a market has been observed for the minimum window, and the economics module
keeps declaring the unknown until then.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

# Below this much observation a rate is noise, not a measurement.
MIN_OBSERVATION_SEC = 60.0


@dataclass
class _Market:
    contracts: float = 0.0
    first_ts: float = 0.0
    last_ts: float = 0.0
    trades: int = 0
    by_price: dict = field(default_factory=lambda: defaultdict(float))


class FlowStats:
    """Accumulates observed public trade volume per market."""

    def __init__(self, min_observation_sec: float = MIN_OBSERVATION_SEC):
        self._m: dict[str, _Market] = {}
        self.min_observation_sec = min_observation_sec

    def observe(self, *, ticker: str, contracts: float, price_cents: int | None = None,
                ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        m = self._m.get(ticker)
        if m is None:
            m = _Market(first_ts=ts)
            self._m[ticker] = m
        m.contracts += float(contracts)
        m.trades += 1
        m.last_ts = ts
        if price_cents is not None:
            m.by_price[int(price_cents)] += float(contracts)

    def observe_trades(self, trades: list) -> int:
        n = 0
        for tr in trades or []:
            try:
                qty = float(tr.get("count_fp") or tr.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or not tr.get("ticker"):
                continue
            px = None
            try:
                px = int(round(float(tr.get("yes_price_dollars") or 0) * 100))
            except (TypeError, ValueError):
                pass
            self.observe(ticker=tr["ticker"], contracts=qty, price_cents=px)
            n += 1
        return n

    def window_sec(self, ticker: str) -> float:
        m = self._m.get(ticker)
        if m is None:
            return 0.0
        return max(0.0, m.last_ts - m.first_ts)

    def measured(self, ticker: str) -> bool:
        """True only when we have watched long enough for a rate to mean
        something. Until then the caller must keep treating it as unknown."""
        m = self._m.get(ticker)
        return bool(m and m.trades > 0
                    and self.window_sec(ticker) >= self.min_observation_sec)

    def contracts_per_sec(self, ticker: str) -> float | None:
        if not self.measured(ticker):
            return None
        m = self._m[ticker]
        w = self.window_sec(ticker)
        return m.contracts / w if w > 0 else None

    def expected_fills(self, ticker: str, size: float, horizon_sec: float,
                       queue_depth: float = 0.0) -> float | None:
        """Expected number of times a `size` quote is fully filled over
        `horizon_sec`, or None when the market has not been observed long
        enough to say.

        To fill our quote once, observed volume must clear the depth already
        ahead of us AND then our own size:

            fills = volume / (queue_depth + size)

        Crediting every trade in the market to our price level still
        overstates the rate (flow splits across both sides and across
        levels), so this remains an upper bound — which overstates fees and
        inventory cost, the conservative direction for a go/no-go decision.
        """
        rate = self.contracts_per_sec(ticker)
        if rate is None or size <= 0:
            return None
        denom = max(1.0, float(queue_depth) + float(size))
        return (rate * float(horizon_sec)) / denom

    def summary(self) -> dict:
        return {t: {"contracts": round(m.contracts, 2), "trades": m.trades,
                    "window_sec": round(self.window_sec(t), 1),
                    "measured": self.measured(t)}
                for t, m in self._m.items()}
