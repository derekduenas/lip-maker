"""Domain-specific dislocation scanners.

Each scanner owns ONE event-pair domain (macro, weather, bio, sports, etc.).
Scanners are the "AI agents" of this prong — but the AI only enters at:
  1. Universe building (mapping which Kalshi market = which CME contract)
  2. Heterogeneous source parsing (NWS bulletins, FDA calendar, etc.)

Decision and execution are deterministic.

Adding a new scanner:
  1. Subclass DislocationScanner.
  2. Implement load_pairs() — return list[EventPair] for this domain.
  3. Implement fetch_quotes(pair) -> (VenueQuote, VenueQuote).
  4. Optionally override score_basis_risk(pair) -> float (default 1.0).
  5. Register in run_all() in tools/dislocation_scan.py.
"""
