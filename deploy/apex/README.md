# APEX droplet deployment (paper)

Exact config running on APEX (2026-10-01). Code = this branch.

- lip-unattended.service: deploy/lip-unattended.service + drop-ins in lip-unattended.service.d/ (override.conf = venv python; policy.conf = paper policy knobs, patches 5-16).
- Paper is forced three ways: `LIP_FORCE_PAPER=1` (resolve_mode refuses anything but paper while it is set), `Environment=` lines in the base unit and both drop-ins, and an `/usr/bin/env LIP_FORCE_PAPER=1 LIP_PAPER=true` prefix on ExecStart. override.conf replaces the base ExecStart, so it repeats that prefix; /etc/lip-maker/lip-maker.env (EnvironmentFile) overrides `Environment=` lines but not the env(1) prefix.
- Bankroll is pinned in policy.conf (`LIP_BANKROLL=1500`). The env file overrides it, so an existing /etc/lip-maker/lip-maker.env must not set LIP_BANKROLL (older copies of lip-maker.env.example set 5000: delete that line).
- Engine state: positions, fill/fee aggregates, rolled periods, cooldowns and an internal kill latch persist in `LIP_STATE_FILE` (default /var/lib/lip-maker/engine_state.json) across restarts. An unreadable state file latches the engine kill until it is moved aside. To clear an engine kill latch that was persisted there (positions kept): `cd /opt/lip-maker && sudo -u lip .venv/bin/python -m mm.unattended --reset-kill`, then restart lip-unattended. This is separate from the watchdog's `--reset`.
- Manual tool runs (tools/*.py, ad hoc scripts using execution/kalshi_auth.KalshiClient): the repo .env is no longer loaded on import. Credentials come from the environment; set `LIP_LOAD_DOTENV=1` to have the client read the repo .env (it never sets LIP_* keys). The services do not set it.
- Python: requirements.txt was pinned on Python 3.13 (the Dockerfile uses python:3.13-slim); every pin also has a 3.12 wheel. Which Python the APEX venv (/opt/lip-maker/.venv, created by the command-box install noted in override.conf) runs is not recorded in this repo; deploy/droplet/setup.sh targets Ubuntu 24.04, whose python3 is 3.12. Check with `/opt/lip-maker/.venv/bin/python --version`.
- lip-watchdog.service (patch 17): independent kill switch, `python -m mm.safety.lip_watchdog`, user lip, `Restart=always` with no start-rate limit. Settings: watchdog.env.example (append to /etc/lip-maker/lip-maker.env; secrets stay out of the repo).
- Kill flag: /var/lib/lip-maker/KILL (LIP_KILL_FILE). The engine checks it on a timer that runs independently of market-data frames, so a stalled feed does not stop the check, and latches external_kill (cancels all quotes). (At cd8af63 it was only checked inside the frame callback, at most once per second while frames arrived.)
- Kill file unwritable: the watchdog runs `LIP_WD_STOP_CMD` (default `systemctl stop lip-unattended`) every tick until the file lands, with TRIP alerts. User lip cannot stop a system unit by default. Install this polkit rule (it works with the unit's NoNewPrivileges=true; a sudoers entry would not):

  ```
  // /etc/polkit-1/rules.d/50-lip-watchdog.rules
  polkit.addRule(function(action, subject) {
      if (action.id == "org.freedesktop.systemd1.manage-units" &&
          action.lookup("unit") == "lip-unattended.service" &&
          action.lookup("verb") == "stop" && subject.user == "lip") {
          return polkit.Result.YES;
      }
  });
  ```
  Check: `sudo -u lip systemctl stop lip-unattended` stops it without a password prompt.
- Live cancel-all arming: real Kalshi API writes only when the watchdog's own env has `LIP_WD_LIVE_ARMED=true` and `LIP_PAPER` not true. Given that, it cancels when /status reports live_armed, or the engine was ever seen live (persisted in watchdog_state.json until `--reset`; a corrupt state file counts as seen live), or /status is unreachable (dead/hung engine). Engine reporting live while the watchdog is not armed is a TRIP: "engine live but watchdog unarmed". The engine gets LIP_PAPER from its drop-in and the watchdog from the env file; keep them in step.
- Cancel scope: the Kalshi account is shared with the Weather engine. `LIP_WD_CANCEL_SCOPE=ours` (default) cancels only orders whose client_order_id starts with `LIP_WD_COID_PREFIX` (default `LIP-`); `all` cancels every resting order on the account. Cancels use `DELETE /portfolio/events/orders/{id}` with each order's market_ticker and exchange_index, then re-list to verify.
- Watchdog self-failure: a tick exception alerts; `LIP_WD_TICK_FAILS` (3) in a row latches the watchdog (watchdog_tick_failing) and writes the kill file.
- Reset after a trip: `cd /opt/lip-maker && sudo -u lip .venv/bin/python -m mm.safety.lip_watchdog --reset && sudo systemctl restart lip-unattended`. Reset removes the kill file and clears the latch and the persisted engine-seen-live flag. If the engine was stopped by LIP_WD_STOP_CMD, `systemctl start lip-unattended` instead of restart.
- Health: /var/lib/lip-maker/watchdog_health.json (includes config_armed, engine_seen_live, inventory basis); alerts: /var/lib/lip-maker/alerts.log, alert.json.
- Scheduled-event calendar (off by default): set `LIP_EVENT_CALENDAR_FILE` (e.g. `/etc/lip-maker/event_calendar.json`, readable by lip) to pull and refuse quotes on markets whose series/ticker starts with an event's prefix, from `at - pre_minutes` to `at + post_minutes` (status/pulls reason `scheduled_event`). Start from event_calendar.example.json: its entries are EXAMPLE placeholders dated 2099, not real release dates; the operator must enter and maintain real, verified times. Unset: no pulls (logged once). Set but missing/invalid: every market is blocked (`scheduled_event_calendar_error`, CRITICAL alert) until fixed. The file is re-read within 60 s of a change.
- Phase 4 measurement (paper estimates): /status `markout_horizons` (fill markouts at 5 s / 30 s / 2 min / 10 min / 1 h and settlement, vs book mark and external fair value, by venue and bucket; pending checks bounded by `LIP_MARKOUT_MAX_PENDING`, 5000 fills) and `pnl_attribution` (spread capture, adverse selection, inventory/settlement MTM, estimated rewards per venue, rebates, fees; sums to `pnl_usd`; also in the daily summary).
