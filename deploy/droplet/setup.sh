#!/bin/bash
# Ubuntu 24.04 paper install for a small DigitalOcean droplet (APEX layout).
# Does not arm live trading. Does not open the status port on the public interface.
#
# Layout it produces:
#   /opt/lip-maker            code, owned root:root, read-only to user lip
#   /opt/lip-maker/.venv      venv with requirements.txt, owned root
#   /etc/lip-maker/lip-maker.env   root:lip 0640 (lip reads, cannot edit)
#   /var/lib/lip-maker        state (heartbeat, logs, recordings, KILL), owned lip
#   lip-unattended.service    deploy/lip-unattended.service + deploy/apex drop-ins
#   lip-watchdog.service      deploy/apex/lip-watchdog.service
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root on the droplet" >&2
  exit 1
fi

REPO=/opt/lip-maker
ENV_DIR=/etc/lip-maker
ENV_FILE=$ENV_DIR/lip-maker.env
STATE=/var/lib/lip-maker

if [[ ! -f $REPO/mm/unattended/__main__.py ]]; then
  echo "copy the repo to $REPO before running this script" >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ufw

id -u lip >/dev/null 2>&1 || useradd --system --home $REPO --shell /usr/sbin/nologin lip
mkdir -p $STATE $ENV_DIR

# Code: root-owned, world-readable, not writable by lip.
chown -R root:root $REPO
chmod -R u+rwX,go+rX,go-w $REPO

# Virtualenv with pinned runtime deps (the units run $REPO/.venv/bin/python).
if [[ ! -x $REPO/.venv/bin/python ]]; then
  python3 -m venv $REPO/.venv
fi
$REPO/.venv/bin/pip install --upgrade pip
$REPO/.venv/bin/pip install -r $REPO/requirements.txt
chown -R root:root $REPO/.venv

# Env file: root:lip 0640. Secrets stay out of the repo.
if [[ ! -f $ENV_FILE ]]; then
  cp $REPO/deploy/droplet/lip-maker.env.example $ENV_FILE
fi
if ! grep -q '^LIP_WD_INTERVAL_S=' $ENV_FILE; then
  # Watchdog settings (engine ignores LIP_WD_*). LIP_WD_LIVE_ARMED stays false.
  cat $REPO/deploy/apex/watchdog.env.example >> $ENV_FILE
fi
chown root:lip $ENV_DIR $ENV_FILE
chmod 0750 $ENV_DIR
chmod 0640 $ENV_FILE
for pem in $ENV_DIR/*.pem; do
  [[ -f $pem ]] || continue
  chown root:lip "$pem"
  chmod 0640 "$pem"
done

# State: the only place lip writes.
chown -R lip:lip $STATE
chmod 0750 $STATE

# Units: base unit + APEX drop-ins (venv python, paper policy) + watchdog.
install -m 0644 $REPO/deploy/lip-unattended.service /etc/systemd/system/lip-unattended.service
install -d -m 0755 /etc/systemd/system/lip-unattended.service.d
install -m 0644 $REPO/deploy/apex/lip-unattended.service.d/*.conf /etc/systemd/system/lip-unattended.service.d/
install -m 0644 $REPO/deploy/apex/lip-watchdog.service /etc/systemd/system/lip-watchdog.service
systemctl daemon-reload

# SSH only. The status page binds 127.0.0.1 and is not allowed through here.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

systemctl enable --now lip-unattended.service
systemctl enable --now lip-watchdog.service

echo "Paper units enabled: lip-unattended.service, lip-watchdog.service."
echo "LIP_PAPER=true. Orders stay simulated."
echo "Production books: set KALSHI_PROD_READ_KEY_ID and KALSHI_PROD_READ_KEY_PATH in $ENV_FILE,"
echo "put the pem in $ENV_DIR (root:lip 0640), then systemctl restart lip-unattended.service."
echo "Without that read key the status page shows: demo-books: results not representative"
echo "Note: values in $ENV_FILE override the units' Environment= lines (systemd.exec(5));"
echo "do not set LIP_PAPER=false there. This script does not arm live trading."
