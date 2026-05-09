"""Cross-Venue Dislocation Harvester — Prong 3.

Hunts for mispricings between two markets pricing the SAME underlying event
at different venues (Kalshi prediction market vs CME Fed funds futures, etc.).

Edge is convergence, not prediction: both venues must agree by settlement,
so cost-adjusted spread > threshold = expected profit on convergence.

Architecture:
    EventUniverse        registry of (venue_a_market, venue_b_market) event pairs
    pricing/             venue-specific implied-probability extractors
    spread.py            cost-adjusted spread + edge calculator
    sizer.py             Kelly-bounded convergence sizer (capital + risk aware)
    scanners/            domain-specific dislocation scanners (one per niche)

The scanners are the "AI agents" — each owns one event-pair domain (macro,
weather, bio, sports, election, earnings, music) and emits ranked candidate
trades. Execution is deterministic; LLMs only enter the universe-builder and
heterogeneous-source-parsing steps where they have actual edge.

Output is candidate trades, not orders. Manual review gate by default until
realized convergence is validated against the model on ≥30 settlements.
"""
