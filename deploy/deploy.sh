#!/bin/bash
# deploy/deploy.sh — Sovereign Daily Scan Deployment
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

source deploy/config.sh

echo ""
echo "═══════════════════════════════════════════════"
echo " SOVEREIGN DEPLOY — Daily Scan Mode"
echo " Target: ${SERVER_USER}@${SERVER_IP}"
echo "═══════════════════════════════════════════════"
echo ""

# ── PRE-DEPLOY CHECKS ────────────────────────────────────────────────────────
echo "Running pre-deploy checks..."

python3 -m pytest tests/ -q --tb=short
if [ $? -ne 0 ]; then
  echo "✗ Tests failed. Aborting deploy."
  exit 1
fi
echo "✓ All tests passing"

python3 loop/daily_scan.py --test-run > /dev/null 2>&1
if [ $? -ne 0 ]; then
  echo "✗ daily_scan.py --test-run failed. Aborting deploy."
  exit 1
fi
echo "✓ Daily scan test-run clean"

grep -q "schedule" requirements.txt
if [ $? -ne 0 ]; then
  echo "✗ 'schedule' not in requirements.txt. Aborting."
  exit 1
fi
echo "✓ requirements.txt includes schedule"

echo ""

# ── SYNC CODE ────────────────────────────────────────────────────────────────
echo "Syncing code to ${SERVER_IP}..."

rsync -avz --progress \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='logs/' \
  --exclude='data/*.db' \
  --exclude='data/watchlist.json' \
  --exclude='corpus/**/*.html' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.git' \
  --exclude='*.pyc' \
  --exclude='config/kalshi_private_key.pem' \
  ./ "${SERVER_USER}@${SERVER_IP}:${APP_DIR}/"

echo "✓ Code synced"

# ── SERVER UPDATES ───────────────────────────────────────────────────────────
echo "Installing dependencies on server..."

ssh "${SERVER_USER}@${SERVER_IP}" "
  cd ${APP_DIR}
  /home/sovereign/venv/bin/pip install -q -r requirements.txt
  echo 'Dependencies installed'
"

# ── SERVICE UPDATE ───────────────────────────────────────────────────────────
echo "Updating systemd services..."

ssh root@"${SERVER_IP}" "
  cp ${APP_DIR}/deploy/sovereign.service /etc/systemd/system/sovereign.service
  cp ${APP_DIR}/deploy/sovereign-watchdog.service /etc/systemd/system/sovereign-watchdog.service
  cp ${APP_DIR}/deploy/sovereign-watchdog.timer /etc/systemd/system/sovereign-watchdog.timer

  systemctl daemon-reload

  systemctl restart sovereign
  sleep 5

  systemctl enable sovereign-watchdog.timer
  systemctl start sovereign-watchdog.timer

  echo 'Services restarted'
"

echo "✓ Services updated"

# ── VERIFY DEPLOYMENT ────────────────────────────────────────────────────────
echo ""
echo "Verifying deployment..."
sleep 8

# Check service is running
SERVICE_STATUS=$(ssh root@"${SERVER_IP}" "systemctl is-active sovereign" 2>/dev/null)
if [ "$SERVICE_STATUS" = "active" ]; then
  echo "✓ sovereign service: active"
else
  echo "✗ sovereign service: ${SERVICE_STATUS}"
  echo "  Check logs: ssh ${SERVER_USER}@${SERVER_IP} 'tail -50 ${APP_DIR}/logs/sovereign.log'"
  exit 1
fi

# Show recent logs
echo ""
echo "Recent logs:"
ssh "${SERVER_USER}@${SERVER_IP}" "tail -15 ${APP_DIR}/logs/sovereign.log" 2>/dev/null || echo "  (no logs yet)"

echo ""
echo "═══════════════════════════════════════════════"
echo " DEPLOY COMPLETE"
echo " Monitor: RUNNING"
echo " Mode: PAPER"
echo " Schedule: 6am scan / 12pm check / 8pm review (ET)"
echo " Logs: ssh ${SERVER_USER}@${SERVER_IP} 'tail -f ${APP_DIR}/logs/sovereign.log'"
echo "═══════════════════════════════════════════════"
