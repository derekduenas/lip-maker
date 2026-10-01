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
python3 -m venv /opt/lip-maker/.venv
/opt/lip-maker/.venv/bin/pip install -r /opt/lip-maker/requirements.txt

chown -R lip:lip /opt/lip-maker /var/lib/lip-maker /etc/lip-maker
chmod 600 /etc/lip-maker/lip-maker.env
if [[ -f /etc/lip-maker/kalshi-prod-read.pem ]]; then
  chown lip:lip /etc/lip-maker/kalshi-prod-read.pem
  chmod 600 /etc/lip-maker/kalshi-prod-read.pem
fi

cp /opt/lip-maker/deploy/lip-unattended.service /etc/systemd/system/lip-unattended.service
systemctl daemon-reload

# SSH only. The status page binds 127.0.0.1 and is not allowed through here.
# An active firewall that already has rules is the operator's policy.
# Do not replace it with a default-deny or run ufw --force enable.
ufw_status="$(ufw status 2>/dev/null || true)"
if grep -q '^Status: active' <<<"$ufw_status" && grep -Eq 'ALLOW|DENY|REJECT|LIMIT' <<<"$ufw_status"; then
  echo "ufw already active with rules; leaving the firewall unchanged"
else
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow OpenSSH
  ufw --force enable
fi

systemctl enable --now lip-unattended.service

echo "Paper unit is enabled and running (selector, sizer, quoter, scorer, allocator, risk)."
echo "LIP_PAPER=true. Orders stay simulated."
echo "Production books: set KALSHI_PROD_READ_KEY_ID and KALSHI_PROD_READ_KEY_PATH in /etc/lip-maker/lip-maker.env, chmod 600 the pem, then systemctl restart lip-unattended.service."
echo "Without that read key the status page shows: demo-books: results not representative"
echo "Live trading stays off. Arming it later takes a separate acknowledgement, allow_production, and LIP_PAPER=false. This script does not do that."
