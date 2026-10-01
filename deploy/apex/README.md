# APEX droplet deployment (paper)

Exact config running on APEX (2026-10-01). Code = this branch.

- lip-unattended.service: deploy/lip-unattended.service + drop-ins in lip-unattended.service.d/ (override.conf = venv python; policy.conf = paper policy knobs, patches 5-16).
- lip-watchdog.service (patch 17): independent kill switch, `python -m mm.safety.lip_watchdog`, user lip. Settings: watchdog.env.example (append to /etc/lip-maker/lip-maker.env; secrets stay out of the repo).
- Kill flag: /var/lib/lip-maker/KILL (LIP_KILL_FILE). Engine checks it once per second and latches external_kill (cancels all quotes).
- Reset after a trip: `cd /opt/lip-maker && sudo -u lip .venv/bin/python -m mm.safety.lip_watchdog --reset && sudo systemctl restart lip-unattended`
- Health: /var/lib/lip-maker/watchdog_health.json; alerts: /var/lib/lip-maker/alerts.log, alert.json.
