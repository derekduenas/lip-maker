# Kalshi post_only enforcement, demo wire, 2026-09-30

Host: `https://demo-api.kalshi.co/trade-api/v2` only. OpenAPI 3.32.0.
The production host was not called. No key material is in this note.

Crossing `post_only: true` orders on `KXRAIN-26SEP30-DTW` were rejected:

* YES bid at the ask (`side: bid`, `price: 0.5800`) → HTTP 400
  `invalid_order` / `post only cross`
* YES bid through the ask (`0.6000`) → the same 400
* NO buy that sells YES at the bid (`side: ask`, `price: 0.5400`) → the same 400

An otherwise identical order one tick inside the spread rested (HTTP 201,
`fill_count: 0.00`). Amending that resting order to a crossing price returned
HTTP 200 with `remaining_count: 0.00`, `fill_count: 0.00`, and status
`canceled`. No fill, no fee, balance unchanged.

That is enough to say Kalshi's demo exchange enforces `post_only` on V2
event orders, including through amend (cancel, not take). It is not a
statement about Polymarket US, and it is not a reason to turn live trading
on. `MAKER_ONLY_ENFORCEMENT_VERIFIED` stays `False`. The Kalshi-only switch
is `enable_kalshi_maker_only_enforcement` in `execution/order_request.py`,
and it stays off unless that function is called with the acknowledgement
below. `KalshiRestTransport` still refuses `api.elections.kalshi.com` and
`external-api.kalshi.com` unless `allow_production=True` is passed separately.

Acknowledgement string:

    kalshi-demo-2026-09-30-post-only-cross
