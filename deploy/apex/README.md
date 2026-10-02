# APEX droplet deployment (paper)

Exact config running on APEX (2026-10-01). Code = this branch.

- lip-unattended.service: deploy/lip-unattended.service + drop-ins in lip-unattended.service.d/ (override.conf = venv python; policy.conf = paper policy knobs, patches 5-16).
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
