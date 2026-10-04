"""Pre-live operations layer (paper only; nothing here can reach an exchange).

Every module takes callables (send, lookup, fetch, clock), never an adapter that
signs requests, and none imports a credential or an order endpoint. They are the
safety net a live order would need first: retry-safe order ids, a rate budget with a
reserved cancel lane, Kalshi order-group modelling, scheduled reconciliation against
venue truth with a halt on unexplained divergence, an off-VM dead-man's switch and a
deploy verifier. Enabling live trading remains the owner's explicit decision behind
the existing interlocks (LIP_PAPER / LIP_LIVE_ACK, maker-only enforcement flags, the
watchdog's LIP_WD_LIVE_ARMED), none of which this package reads or sets.

Kalshi behaviours modelled here come from search snippets of docs.kalshi.com (the
pages could not be fetched when this was written): client_order_id idempotency,
order groups (contracts_limit over a rolling 15 s window), token-bucket rate limits.
Verify each against the current documentation before any live use.
"""
