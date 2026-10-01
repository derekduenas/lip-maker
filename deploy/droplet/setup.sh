#!/bin/bash
# Ubuntu 24.04 paper install for a small DigitalOcean droplet.
# Does not arm live trading. Does not open the status port on the public interface.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root on the droplet" >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ufw

id -u lip >/dev/null 2>&1 || useradd --system --home /opt/lip-maker --shell /usr/sbin/nologin lip
mkdir -p /opt/lip-maker /var/lib/lip-maker /etc/lip-maker
if [[ -d /opt/lip-maker/.git ]]; then
  echo "repo already at /opt/lip-maker"
else
  echo "copy the repo to /opt/lip-maker before starting the unit" >&2
fi

if [[ ! -f /etc/lip-maker/lip-maker.env ]]; then
  cp /opt/lip-maker/deploy/droplet/lip-maker.env.example /etc/lip-maker/lip-maker.env
fi
chown -R lip:lip /opt/lip-maker /var/lib/lip-maker /etc/lip-maker
chmod 600 /etc/lip-maker/lip-maker.env

cp /opt/lip-maker/deploy/lip-unattended.service /etc/systemd/system/lip-unattended.service
systemctl daemon-reload

# SSH only. The status page binds 127.0.0.1 and is not allowed through here.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "Paper unit is installed and not started."
echo "Run a recording first:"
echo "  sudo -u lip LIP_PAPER=true python3 -m mm.unattended --cycle /var/lib/lip-maker/books.jsonl --once --heartbeat /var/lib/lip-maker/heartbeat --cancel-log /var/lib/lip-maker/startup-cancel"
echo "Then: systemctl enable --now lip-unattended.service"
echo "Live trading stays off. Arming it later takes a separate acknowledgement, allow_production, and LIP_PAPER=false. This script does not do that."
