"""Venue-specific implied-probability extractors.

Every module here exposes one function: implied_prob(market_id, ...) -> float
in [0, 1], representing the probability that market's "YES" outcome resolves
true based on the venue's current pricing.

Sources today:
    fed_funds.py    CME ZQ (Fed funds) futures   → P(rate cut at FOMC)
    kalshi_prob.py  Kalshi market mid            → direct probability

Add as needed:
    options_iv.py   ATM straddle implied move    → P(stock moves > X)
    pm_prob.py      Polymarket mid               → direct probability
    sportsbook.py   Decimal odds → implied prob  (after vig removal)
"""
