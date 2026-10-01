# Unattended Kalshi loop (paper / demo)

This process is for a small always-on VM in **us-east**, close to the venue, running the paper loop continuously. It does not talk to the production exchange. `LIP_PAPER` stays `true`. A production websocket host is refused.

The demo websocket path this client already signs is `/trade-api/ws/v2` on the demo host already allowed for REST: `wss://demo-api.kalshi.co/trade-api/ws/v2`. Set it with `LIP_KALSHI_WS_URL` only when a demo key is present. Leave the variable unset and the process stays on the local paper feed.

## What the process does on every start

1. Append `cancel_all` and cancel every order it still knows about.
2. Only then quote.
3. Write the heartbeat file.

systemd `Restart=on-failure` starts that sequence again after a crash. A crash inside the quote loop does the same: the watchdog builds a new session, and that session cancels before it quotes.

```bash
sudo useradd --system --home /opt/lip-maker --shell /usr/sbin/nologin lip
sudo mkdir -p /var/lib/lip-maker /opt/lip-maker
sudo cp deploy/lip-unattended.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lip-unattended.service
```

The unit does not set `LIP_PAPER=false` and does not set a production host. `deploy/lip-maker.service` is an older unit and is not this process.

A container runs the same entrypoint:

```bash
docker build -t lip-unattended .
docker run --rm -e LIP_PAPER=true lip-unattended --once \
  --heartbeat /tmp/hb --cancel-log /tmp/cancel
```

## Books

`OrderbookFeed` prefers the websocket. If the last websocket message is older than one second, the next read is REST. `RequoteGate` calls the quote handler in the same turn when the LIP reference (the first level that reaches one-fifth of target size) moves by one cent or more. That call is not deferred, so the decision itself is inside the one-second target.

## Size

`optimize_sizes` picks a contract count at that reference price. The score is reward share times pool per day, minus the markout cost. Per-market, per-event, and total capital caps all apply. Quiet programs whose period is at least two days are funded before louder ones when capital is short. Programs of 15 minutes or less stay in the `short_pools` bucket and are not sized unless `enable_short_pools=True`.

## Health

* Heartbeat file, same format as `mm.safety.supervisor`.
* `render_daily_summary` prints fills, cash P&L, and rewards.
* `LIP_ALERT_WEBHOOK`, when set, receives a payload if the kill switch trips. Unset, the hook is skipped.
* Over the rolling 24 hours, if reward dollars divided by markout-cost dollars fall below 1, the process cancels and stays stopped until something clears `Health.killed`.

Paper mode does not send the cancel to production. The cancel log and the paper adapter are the record.
