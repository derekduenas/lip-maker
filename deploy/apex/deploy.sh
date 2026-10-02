#!/bin/bash
# Deploy (or update) the PAPER engine + watchdog on the APEX droplet and keep both
# running 24/7 under systemd. Idempotent. Does not arm live trading.
#
#   sudo bash deploy.sh                 # deploy origin/apex/patches-1-17
#   sudo bash deploy.sh --ref <branch>  # deploy another branch or tag
#   sudo bash deploy.sh --rollback <backup-dir>   # restore a previous code tree
#
# Run it from anywhere; it fetches the code itself. What it does:
#   1. backs up /opt/lip-maker (code), /etc/lip-maker/lip-maker.env and the units
#   2. stops both services, swaps in the requested ref, builds .venv from requirements.txt
#   3. applies the pre-deploy checklist in deploy/apex/README.md:
#        - removes LIP_BANKROLL from the env file (it would override policy.conf)
#        - carries KALSHI_PROD_READ_KEY_ID/PATH over from the old repo .env if the env
#          file lacks them (the repo .env is no longer auto-loaded)
#        - appends missing watchdog defaults (LIP_WD_LIVE_ARMED stays false)
#        - installs the polkit rule for the watchdog's stop fallback and auto-recover restart
#   4. installs units + drop-ins, enables both services at boot, starts them
#   5. verifies /status (paper, production books) and the watchdog health file;
#      informational only: verify_ws_frames.py on the newest recording (if any)
#      and the readiness report verdict
set -euo pipefail

REPO_URL=${LIP_REPO_URL:-https://github.com/derekduenas/lip-maker}
REF=apex/patches-1-17
ROLLBACK=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --rollback) ROLLBACK="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "run as root (sudo bash $0)" >&2; exit 1; }

APP=/opt/lip-maker
ENV_DIR=/etc/lip-maker
ENV_FILE=$ENV_DIR/lip-maker.env
STATE=/var/lib/lip-maker
UNITS=/etc/systemd/system
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/var/backups/lip-maker/$TS
POLKIT=/etc/polkit-1/rules.d/50-lip-watchdog.rules

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

stop_services() {
  systemctl stop lip-watchdog.service 2>/dev/null || true
  systemctl stop lip-unattended.service 2>/dev/null || true
}

# Informational, after the /status check: never fails the deploy.
#  - verify_ws_frames.py on the newest frame recording opened at least 10
#    minutes ago (--min-age-s 600: not the file the engine restarted by this
#    deploy just opened), only if recordings exist (LIP_RECORD_DIR from the
#    env file, else the unit drop-ins, else the default); its exit status is
#    printed, not acted on.
#  - the readiness report's OVERALL verdict line.
DEFAULT_REC_DIR=${DEFAULT_REC_DIR:-/var/lib/lip-maker/recordings}
informational_checks() {
  local rec_dir v rc verdict
  rec_dir=$DEFAULT_REC_DIR
  v=$(sed -nE 's/^[[:space:]]*Environment="?LIP_RECORD_DIR=([^" ]+)"?.*/\1/p' \
        "$UNITS"/lip-unattended.service.d/*.conf 2>/dev/null | tail -1 || true)
  [[ -n "$v" ]] && rec_dir=$v
  v=$(sed -nE 's/^LIP_RECORD_DIR=//p' "$ENV_FILE" 2>/dev/null | tr -d '"' | tail -1 || true)
  [[ -n "$v" ]] && rec_dir=$v
  if compgen -G "$rec_dir/frames-*.jsonl.gz" >/dev/null 2>&1; then
    log "verify_ws_frames on the newest recording older than 10 min in $rec_dir (informational)"
    rc=0
    timeout 300 "$APP/.venv/bin/python" "$APP/tools/verify_ws_frames.py" --dir "$rec_dir" --newest 1 --min-age-s 600 || rc=$?
    log "verify_ws_frames exit $rc (0 ok, 1 an engine-required field missing, 2 no frames; informational)"
  else
    log "no recordings in $rec_dir (LIP_RECORD_ENABLE off or nothing recorded yet): skipping verify_ws_frames"
  fi
  verdict=$(timeout 120 "$APP/.venv/bin/python" "$APP/tools/readiness_report.py" 2>/dev/null \
              | grep -E '^OVERALL:' || true)
  log "readiness (informational): ${verdict:-no verdict (readiness_report failed)}"
  return 0
}

if [[ -n "$ROLLBACK" ]]; then
  [[ -d "$ROLLBACK/app" ]] || { echo "no $ROLLBACK/app" >&2; exit 1; }
  log "rolling back to $ROLLBACK"
  stop_services
  rm -rf "$APP.rollback-tmp"; mv "$APP" "$APP.rollback-tmp"
  cp -a "$ROLLBACK/app" "$APP"
  [[ -f "$ROLLBACK/lip-maker.env" ]] && cp -a "$ROLLBACK/lip-maker.env" "$ENV_FILE"
  if [[ -d "$ROLLBACK/units" ]]; then
    cp -a "$ROLLBACK/units/." "$UNITS/"
  fi
  systemctl daemon-reload
  systemctl start lip-unattended.service
  systemctl start lip-watchdog.service 2>/dev/null || true
  rm -rf "$APP.rollback-tmp"
  log "rolled back; check: systemctl status lip-unattended lip-watchdog"
  exit 0
fi

command -v git >/dev/null || { apt-get update -q && apt-get install -y -q git; }
dpkg -s python3-venv >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q python3-venv; }
command -v curl >/dev/null || apt-get install -y -q curl
id -u lip >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin lip

# 1. Backup ------------------------------------------------------------------
log "backup -> $BACKUP"
mkdir -p "$BACKUP/units"
if [[ -d "$APP" ]]; then
  cp -a "$APP" "$BACKUP/app"
fi
[[ -f "$ENV_FILE" ]] && cp -a "$ENV_FILE" "$BACKUP/lip-maker.env"
for u in lip-unattended.service lip-unattended.service.d lip-watchdog.service; do
  [[ -e "$UNITS/$u" ]] && cp -a "$UNITS/$u" "$BACKUP/units/"
done
OLD_DOTENV="$BACKUP/app/.env"

# 2. Code ---------------------------------------------------------------------
log "fetching $REF from $REPO_URL"
NEW="$APP.new-$TS"
git clone -q --depth 1 --branch "$REF" "$REPO_URL" "$NEW"
log "code at $(git -C "$NEW" rev-parse --short HEAD): $(git -C "$NEW" log -1 --format=%s)"
log "building venv"
python3 -m venv "$NEW/.venv"
"$NEW/.venv/bin/pip" install -q --upgrade pip
"$NEW/.venv/bin/pip" install -q -r "$NEW/requirements.txt"
"$NEW/.venv/bin/python" -c "import mm.unattended.loop, mm.safety.lip_watchdog" \
  || { echo "import check failed; nothing changed on the running system" >&2; rm -rf "$NEW"; exit 1; }

log "stopping services"
stop_services
if [[ -d "$APP" ]]; then
  rm -rf "$APP.prev"; mv "$APP" "$APP.prev"
fi
mv "$NEW" "$APP"
chown -R root:root "$APP"
chmod -R u+rwX,go+rX,go-w "$APP"

# 3. Env file + checklist -----------------------------------------------------
mkdir -p "$ENV_DIR" "$STATE"
[[ -f "$ENV_FILE" ]] || cp "$APP/deploy/droplet/lip-maker.env.example" "$ENV_FILE"

if grep -q '^LIP_BANKROLL=' "$ENV_FILE"; then
  log "removing $(grep '^LIP_BANKROLL=' "$ENV_FILE") from env file (policy.conf pins 1500)"
  sed -i '/^LIP_BANKROLL=/d' "$ENV_FILE"
fi
# Paper everywhere: the watchdog reads LIP_PAPER from this file; the engine is
# also forced by its unit (env(1) prefix + LIP_FORCE_PAPER).
sed -i '/^LIP_PAPER=/d;/^LIP_DEMO=/d;/^LIP_FORCE_PAPER=/d' "$ENV_FILE"
printf 'LIP_PAPER=true\nLIP_FORCE_PAPER=1\n' >> "$ENV_FILE"
if [[ -f "$OLD_DOTENV" ]]; then
  log "the old repo .env is no longer auto-loaded; its keys were: $(grep -oE '^[A-Z0-9_]+' "$OLD_DOTENV" | tr '\n' ' ')"
fi

for k in KALSHI_PROD_READ_KEY_ID KALSHI_PROD_READ_KEY_PATH; do
  if ! grep -q "^$k=" "$ENV_FILE" && [[ -f "$OLD_DOTENV" ]] && grep -q "^$k=" "$OLD_DOTENV"; then
    log "carrying $k over from the old repo .env"
    grep "^$k=" "$OLD_DOTENV" | tail -1 >> "$ENV_FILE"
  fi
done
# A key file that lived inside the old code tree moves to /etc/lip-maker.
KEY_PATH=$(grep '^KALSHI_PROD_READ_KEY_PATH=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' || true)
if [[ -n "$KEY_PATH" && ! -f "$KEY_PATH" && "$KEY_PATH" == "$APP"/* ]]; then
  OLDKEY="$APP.prev/${KEY_PATH#"$APP"/}"
  if [[ -f "$OLDKEY" ]]; then
    NEWKEY="$ENV_DIR/$(basename "$KEY_PATH")"
    log "moving read key $KEY_PATH -> $NEWKEY"
    cp -a "$OLDKEY" "$NEWKEY"
    sed -i "s|^KALSHI_PROD_READ_KEY_PATH=.*|KALSHI_PROD_READ_KEY_PATH=$NEWKEY|" "$ENV_FILE"
    KEY_PATH=$NEWKEY
  fi
fi

# Watchdog defaults: append only keys that are missing. Never arms live.
while IFS= read -r line; do
  [[ "$line" =~ ^LIP_[A-Z0-9_]+= ]] || continue
  key=${line%%=*}
  grep -q "^$key=" "$ENV_FILE" || echo "$line" >> "$ENV_FILE"
done < "$APP/deploy/apex/watchdog.env.example"
sed -i 's/^LIP_WD_LIVE_ARMED=.*/LIP_WD_LIVE_ARMED=false/' "$ENV_FILE"

chown root:lip "$ENV_DIR" "$ENV_FILE"; chmod 0750 "$ENV_DIR"; chmod 0640 "$ENV_FILE"
for pem in "$ENV_DIR"/*.pem "$ENV_DIR"/*.key; do
  [[ -f "$pem" ]] || continue; chown root:lip "$pem"; chmod 0640 "$pem"
done
if [[ -n "${KEY_PATH:-}" && -f "$KEY_PATH" ]]; then
  sudo -u lip test -r "$KEY_PATH" || log "WARNING: user lip cannot read $KEY_PATH"
fi
chown -R lip:lip "$STATE"; chmod 0750 "$STATE"

mkdir -p "$(dirname "$POLKIT")"
cat > "$POLKIT" <<'EOF'
// lip-watchdog may stop lip-unattended when it cannot write the kill file,
// and restart it when auto-recovering a transient (heartbeat/feed) trip.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "lip-unattended.service" &&
        (action.lookup("verb") == "stop" || action.lookup("verb") == "restart") &&
        subject.user == "lip") {
        return polkit.Result.YES;
    }
});
EOF
chmod 0644 "$POLKIT"

# 4. Units ---------------------------------------------------------------------
log "installing units"
LIVE_POLICY="$UNITS/lip-unattended.service.d/policy.conf"
if [[ -f "$LIVE_POLICY" ]] && ! cmp -s "$LIVE_POLICY" "$APP/deploy/apex/lip-unattended.service.d/policy.conf"; then
  # The live drop-in is about to be replaced by the repo copy. Operator edits
  # that were never committed are lost unless re-applied (copy is in $BACKUP/units).
  log "WARNING: live policy.conf differs from the repo copy; these live lines are being replaced:"
  diff <(grep '^Environment=' "$LIVE_POLICY" | sort) \
       <(grep '^Environment=' "$APP/deploy/apex/lip-unattended.service.d/policy.conf" | sort) \
       | sed -n 's/^< /  - /p' || true
  log "  (previous file: $BACKUP/units/lip-unattended.service.d/policy.conf)"
fi
install -m 0644 "$APP/deploy/lip-unattended.service" "$UNITS/lip-unattended.service"
install -d -m 0755 "$UNITS/lip-unattended.service.d"
install -m 0644 "$APP"/deploy/apex/lip-unattended.service.d/*.conf "$UNITS/lip-unattended.service.d/"
install -m 0644 "$APP/deploy/apex/lip-watchdog.service" "$UNITS/lip-watchdog.service"
systemctl daemon-reload
systemctl enable -q lip-unattended.service lip-watchdog.service
systemctl reset-failed lip-unattended.service lip-watchdog.service 2>/dev/null || true
systemctl start lip-unattended.service
sleep 5
systemctl start lip-watchdog.service

# 5. Verify --------------------------------------------------------------------
log "waiting for /status"
ok=""
for _ in $(seq 1 36); do
  if curl -fsS --max-time 3 http://127.0.0.1:8765/status -o /tmp/lip-status.json 2>/dev/null; then ok=1; break; fi
  sleep 5
done
if [[ -z "$ok" ]]; then
  echo "!!! /status did not come up in 180 s. Last log lines:" >&2
  journalctl -u lip-unattended -n 40 --no-pager >&2 || true
  echo "Roll back with: sudo bash $APP/deploy/apex/deploy.sh --rollback $BACKUP" >&2
  exit 1
fi
"$APP/.venv/bin/python" - /tmp/lip-status.json <<'EOF'
import json, sys
s = json.load(open(sys.argv[1]))
mode = s.get("mode"); src = s.get("data_source"); warn = s.get("book_source_warning")
print(f"  mode={mode} live_armed={s.get('live_armed')} data_source={src}")
if warn:
    print(f"  !!! book_source_warning: {warn}")
if s.get("live_armed") or (mode not in (None, "paper")):
    print("  !!! NOT PAPER: stop now: systemctl stop lip-unattended"); sys.exit(1)
EOF
informational_checks
sleep 35
if [[ -f "$STATE/watchdog_health.json" ]]; then
  "$APP/.venv/bin/python" -c "import json;h=json.load(open('$STATE/watchdog_health.json'));print('  watchdog:', {k:h.get(k) for k in ('ok','latched','reasons_now','trip_reasons','config_armed')})"
fi
systemctl --no-pager --lines=0 status lip-unattended.service lip-watchdog.service | grep -E "●|Active:" || true
log "done. Code $(git -C "$APP" rev-parse --short HEAD). Backup: $BACKUP. Previous tree: $APP.prev"
log "rollback: sudo bash $APP/deploy/apex/deploy.sh --rollback $BACKUP"
