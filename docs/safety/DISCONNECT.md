# Disconnect safety

Fetched 1 October 2026. Live sending stays off unless
`enable_kalshi_maker_only_enforcement` has been called and, separately,
the process is out of paper mode. Nothing here flips those defaults.

## Kalshi FIX cancel-on-disconnect

`CancelOrdersOnDisconnect` is tag **8013** on Logon (`35=A`).

Source: <https://docs.kalshi.com/fix/authentication> (fetched 1 October 2026).

* Default **N**. `Y` cancels open orders on any disconnect, including a
  graceful Logout (`35=5`).
* A listener session (tag 20126 = Y) must not set 8013 to Y
  (<https://docs.kalshi.com/fix/listener-sessions>).
* Required logon fields used here: `98=0` (EncryptMethod none), `108`
  HeartbeatInt (at least 3 seconds), `1137=9` (FIX50SP2), `96` RawData
  signature. BeginString is `FIXT.1.1`.
* The flag `KALSHI_FIX_CANCEL_ON_DISCONNECT` in `mm/safety/fix_session.py`
  defaults **False**. The logon builder emits `8013=N` unless the caller
  passes `cancel_on_disconnect=True` or that flag is set. This module does
  not open a socket. `MockKalshiFixAcceptor` is the test double: on
  transport close it cancels resting orders only when the logon said Y.

## Kalshi REST

REST has no cancel-on-disconnect. Two layers:

1. **Order groups.** Every order sent through `SafeSender` carries an
   `order_group_id`. Manual cancel is
   `PUT /portfolio/order_groups/{id}/trigger`, which cancels resting orders
   in the group
   (<https://docs.kalshi.com/api-reference/order-groups/trigger-order-group>,
   <https://docs.kalshi.com/getting_started/order_groups>). Groups do not
   cross shards, so there is one group per `(market, exchange_index)`.
2. **Dead-man.** `DeadMan` watches a websocket beat, a REST beat, an
   explicit disconnect, and `RiskEngine.killed`. If market data is older
   than `stale_ms` (default 3000), either beat stops, or the risk engine
   trips, it triggers every group. A quiet market still needs a beat from
   the socket ping; silence is treated as a stall.

## Supervisor process

`python -m mm.safety.supervisor --heartbeat PATH --cancel-log PATH --once`

The main loop writes a timestamp with `write_heartbeat`. A **separate**
process reads it. If the file is missing or older than `--stale-ms`, the
supervisor appends `cancel_all` to the cancel log and exits 2. The default
supervisor does not hold API keys and does not send. A deployment that
should actually flatten points the same decision at `SafeSender.trigger_all`.

## Polymarket US

* REST cancel-all: `POST /v1/orders/open/cancel` with `{"slugs": []}`. An
  empty list cancels every open order
  (<https://docs.polymarket.us/api-reference/orders/cancel-all-open-orders>).
  There is no PM US REST order-group or cancel-on-disconnect. `SafeSender`
  for PM US calls that cancel-all.
* FIX, institutional: Cancel on Disconnect is a **session config**
  (`CancelOnDisconnect=Y`), off by default. It cancels **DAY** orders.
  GTC and GTD stay up
  (<https://docs.polymarket.us/institutional/fix-api/fix-session-management>,
  fetched 1 October 2026). `pmus_fix_session` records that, behind
  `PMUS_FIX_CANCEL_ON_DISCONNECT` default False. The mock cancels DAY and
  leaves GTC.
* The international CLOB `POST /heartbeats` auto-cancel (about 10 seconds)
  is **polymarket.com**, not Polymarket US. This repo does not send it.
