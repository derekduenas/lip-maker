# DigitalOcean droplet (Ubuntu 24.04)

Paper first. The unit sets `LIP_PAPER=true` and the demo websocket `wss://demo-api.kalshi.co/trade-api/ws/v2`. It does not set a production host. Note that the unit's `Environment=LIP_PAPER=true` does NOT override the env file: per systemd.exec(5), variables read from `EnvironmentFile=` override those set with `Environment=`, whatever their order in the unit. An env file containing `LIP_PAPER=false` therefore wins. Paper is enforced in code by `LIP_FORCE_PAPER` (being added on the fix/loop branch), not by unit ordering.

## Install

```bash
sudo bash deploy/droplet/setup.sh
```

The script installs Python 3, creates the `lip` user, makes `/opt/lip-maker` root-owned (read-only to `lip`), creates `/opt/lip-maker/.venv` and installs `requirements.txt` into it, copies `deploy/droplet/lip-maker.env.example` to `/etc/lip-maker/lip-maker.env` when that file is missing and appends `deploy/apex/watchdog.env.example` once (env file `root:lip 0640`), gives `/var/lib/lip-maker` to `lip`, installs `deploy/lip-unattended.service` with the `deploy/apex/lip-unattended.service.d/` drop-ins and `deploy/apex/lip-watchdog.service`, enables ufw with OpenSSH only, and enables both units. After that the paper system is up.

The process is `python -m mm.unattended --run` (venv python, via the drop-in). On startup it logs `cancel_all` to the cancel log (no venue cancel; see `docs/UNATTENDED.md`), then continuously runs selector, sizer, quoter, per-second scorer, allocator, and risk. Selection repeats every 10 minutes. Quotes come off at T-15 minutes before `close_ts`. A single fill's premium is capped at $100. The live series gate applies in demo mode. Paper mode records that decision and still quotes, so a new droplet can collect the five days the gate asks for.

Heartbeat: `/var/lib/lip-maker/heartbeat`. Daily summary: `/var/lib/lip-maker/daily-summary`. Status JSON: `http://127.0.0.1:8765/status` (loopback only; ufw does not open it). Logs: `/var/lib/lip-maker/lip.log`, rotating at 1 MB, five files.

`LIP_PAPER=true` simulates fills from public trades with the paper fill model (last in queue, 250 ms latency, only a trade at our price). With `KALSHI_PROD_READ_KEY_ID` and `KALSHI_PROD_READ_KEY_PATH` set, those books and trades come from the production exchange through a read-only client. Without that key the process uses demo books and the status page and daily summary say `demo-books: results not representative`.

The read-only client allows GET market data (markets, events, series, order books, trades, incentive programs, exchange status) and a websocket subscription to `orderbook_delta`, `ticker`, and `trade`. A POST, PUT, DELETE, PATCH, portfolio route, or order/fill/position channel logs and exits. The quote path, `SafeSender`, and the demo sender cannot take that client. `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` stay the demo order key and are separate.

## Production read key

1. In the Kalshi web UI create an API key. Kalshi shows the key id and gives you the private key file once.
2. Copy the pem to the droplet and lock it down. From your own machine:

```bash
scp ./kalshi_prod_read.pem root@YOUR_DROPLET:/etc/lip-maker/kalshi-prod-read.pem
ssh root@YOUR_DROPLET 'chown lip:lip /etc/lip-maker/kalshi-prod-read.pem && chmod 600 /etc/lip-maker/kalshi-prod-read.pem'
```

3. Put the id and the path in `/etc/lip-maker/lip-maker.env` (mode 600). Do not put the pem on the command line.

```bash
KALSHI_PROD_READ_KEY_ID=the-id-from-the-kalshi-ui
KALSHI_PROD_READ_KEY_PATH=/etc/lip-maker/kalshi-prod-read.pem
```

4. Restart the paper service:

```bash
sudo systemctl restart lip-unattended.service
```

The status page at `http://127.0.0.1:8765/status` then shows `data_source` `production-books`. The daily summary at `/var/lib/lip-maker/daily-summary` includes the same line. Live arming flags stay off.

Secrets stay in `/etc/lip-maker/lip-maker.env` or in files that variable points at. A private key on the command line is refused.

## Replay a recorded stream

```bash
sudo -u lip env LIP_PAPER=true python3 -m mm.unattended \
  --run --replay /var/lib/lip-maker/stream.jsonl \
  --report /var/lib/lip-maker/run.json \
  --summary /var/lib/lip-maker/daily-summary \
  --log-file /var/lib/lip-maker/lip.log \
  --heartbeat /var/lib/lip-maker/heartbeat \
  --cancel-log /var/lib/lip-maker/startup-cancel \
  --once
```

That replay does not open a socket. `--cycle` remains the one-shot recording used by the earlier paper pass.

## Demo orders later

`LIP_DEMO=true` together with `LIP_PAPER=false` sends post-only orders to a demo host only (`demo-api.kalshi.co` or `external-api.demo.kalshi.co`). The unit file's `Environment=LIP_PAPER=true` does not stop this: an `EnvironmentFile=` value overrides `Environment=` (systemd.exec(5)), so an env file with `LIP_PAPER=false` and `LIP_DEMO=true` would switch the service to demo orders. Keeping the service on simulated fills relies on the env file not setting those, and on the code-level `LIP_FORCE_PAPER` guard (fix/loop). Demo mode applies the series go/no-go gate. It does not set `allow_production`, the maker-only acknowledgement, or `LIVE_ARMED`.

## Arming live later

This droplet install does not arm live trading. A later step, on a host you mean to use, still has to set `enable_kalshi_maker_only_enforcement` with its acknowledgement, `allow_production`, and `LIP_PAPER=false` together with `LIP_LIVE_ACK`. Leave those off until that decision is explicit. With both `LIP_PAPER` and `LIP_DEMO` off, `--run` refuses to start.
