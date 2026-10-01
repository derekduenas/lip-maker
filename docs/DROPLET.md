# DigitalOcean droplet (Ubuntu 24.04)

Paper first. The unit does not set a production host and does not turn `LIP_PAPER` off.

## Install

```bash
sudo bash deploy/droplet/setup.sh
```

The script installs Python 3, creates the `lip` user, copies `deploy/droplet/lip-maker.env.example` to `/etc/lip-maker/lip-maker.env` when that file is missing, installs `deploy/lip-unattended.service`, and enables ufw with OpenSSH only. The unit loads that env file and still sets `LIP_PAPER=true`. The status page, when started, binds `127.0.0.1`. Do not add a public firewall rule for it.

Secrets stay in `/etc/lip-maker/lip-maker.env` or in files that variable points at. A private key on the command line is refused.

## Paper cycle

One entrypoint runs the paper book:

```bash
sudo -u lip env LIP_PAPER=true python3 -m mm.unattended \
  --cycle /var/lib/lip-maker/books.jsonl \
  --report /var/lib/lip-maker/cycle.json \
  --log-file /var/lib/lip-maker/lip.log \
  --heartbeat /var/lib/lip-maker/heartbeat \
  --cancel-log /var/lib/lip-maker/startup-cancel \
  --once
```

The recording is JSONL: a `program` row, then `book` rows. The cycle selects, sizes, records a paper quote, scores the books, reconciles tagged credits, shrinks a series factor toward 1, asks the allocator for the next dollar amount, and runs the risk check. It does not open a socket.

Logs rotate at 1 MB, five files. A 429 from the Kalshi adapter returns `backoff_s` instead of spinning. A book whose `exchange_ts` is more than 2 seconds from `ts` is not scored.

## Service

```bash
sudo systemctl enable --now lip-unattended.service
```

That process writes a heartbeat and a startup cancel line. It refuses `LIP_PAPER=false` and a production websocket host.

## Arming live later

This droplet install does not arm live trading. A later step, on a host you mean to use, still has to set `enable_kalshi_maker_only_enforcement` with its acknowledgement, `allow_production`, and `LIP_PAPER=false` together with `LIP_LIVE_ACK`. Leave those off until that decision is explicit.
