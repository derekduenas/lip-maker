# DigitalOcean droplet (Ubuntu 24.04)

Paper first. The unit forces `LIP_PAPER=true` and the demo websocket `wss://demo-api.kalshi.co/trade-api/ws/v2`. It does not set a production host.

## Install

```bash
sudo bash deploy/droplet/setup.sh
```

The script installs Python 3, creates the `lip` user, copies `deploy/droplet/lip-maker.env.example` to `/etc/lip-maker/lip-maker.env` when that file is missing, installs `deploy/lip-unattended.service`, enables ufw with OpenSSH only, and runs `systemctl enable --now lip-unattended.service`. After that the paper system is up.

The process is `python3 -m mm.unattended --run`. It cancels on startup, then continuously runs selector, sizer, quoter, per-second scorer, allocator, and risk. Selection repeats every 10 minutes. Quotes come off at T-15 minutes before `close_ts`. A single fill's premium is capped at $100. The live series gate applies in demo mode. Paper mode records that decision and still quotes, so a new droplet can collect the five days the gate asks for.

Heartbeat: `/var/lib/lip-maker/heartbeat`. Daily summary: `/var/lib/lip-maker/daily-summary`. Status JSON: `http://127.0.0.1:8765/status` (loopback only; ufw does not open it). Logs: `/var/lib/lip-maker/lip.log`, rotating at 1 MB, five files.

`LIP_PAPER=true` simulates fills from public trades with the paper fill model (last in queue, 250 ms latency, only a trade at our price). A demo API key in `/etc/lip-maker/lip-maker.env` (`KALSHI_PRIVATE_KEY_PATH` and `KALSHI_KEY_ID`) lets the process open the demo websocket. Until that key exists the process stays up, writes the heartbeat, and does not open a socket and does not fall back to the production host.

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

`LIP_DEMO=true` together with `LIP_PAPER=false` sends post-only orders to a demo host only (`demo-api.kalshi.co` or `external-api.demo.kalshi.co`). The unit file sets `LIP_PAPER=true` after the env file, so the installed service stays on simulated fills even if the env file also sets `LIP_DEMO`. Demo mode applies the series go/no-go gate. It does not set `allow_production`, the maker-only acknowledgement, or `LIVE_ARMED`.

## Arming live later

This droplet install does not arm live trading. A later step, on a host you mean to use, still has to set `enable_kalshi_maker_only_enforcement` with its acknowledgement, `allow_production`, and `LIP_PAPER=false` together with `LIP_LIVE_ACK`. Leave those off until that decision is explicit. With both `LIP_PAPER` and `LIP_DEMO` off, `--run` refuses to start.
