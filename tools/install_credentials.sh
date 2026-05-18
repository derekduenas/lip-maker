#!/bin/bash
# install_credentials.sh — one-time installer for hedge-venue API keys.
#
# Prompts for each credential with silent input (read -s; nothing echoes
# to terminal, nothing lands in shell history). Writes them to a
# systemd drop-in at mode 600 so only root can read.
#
# After this runs, keys exist ONLY at:
#   /etc/systemd/system/lip-maker.service.d/credentials.conf  (mode 600)
# and are exposed to the lip-maker process via os.environ. They are
# never:
#   - committed to git
#   - logged to lip_maker.log
#   - written to lip_maker.db
#   - echoed during this install
#
# To rotate: re-run the script with new keys.
# To revoke: delete the file + systemctl restart lip-maker.service.
#
# USAGE
#   ssh root@147.182.138.189
#   cd /root/lip-maker
#   bash tools/install_credentials.sh

set -e

# Must be root (systemd drop-in lives in /etc/systemd/system)
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (systemd drop-in path requires it)"
    exit 1
fi

DROPIN_DIR="/etc/systemd/system/lip-maker.service.d"
CREDS_FILE="$DROPIN_DIR/credentials.conf"

mkdir -p "$DROPIN_DIR"

echo "=================================================="
echo "  innait hedge-venue credentials installer"
echo "=================================================="
echo ""
echo "This will prompt for each credential. Input is hidden."
echo "Press Enter on a prompt to SKIP that venue (leave existing"
echo "value unchanged if it was set previously)."
echo ""

# ── Read existing values to allow partial updates ─────────────────────────
existing_pm_key=""; existing_pm_secret=""; existing_pm_wallet=""
existing_kraken_key=""; existing_kraken_secret=""
if [ -f "$CREDS_FILE" ]; then
    echo "Existing credentials file found. Empty input KEEPS the old value."
    echo ""
    # Source-style parse without exposing values
    existing_pm_key=$(grep -oP 'PM_API_KEY=\K[^"]+' "$CREDS_FILE" 2>/dev/null | head -1 | tr -d '"' || true)
    existing_pm_secret=$(grep -oP 'PM_SECRET=\K[^"]+' "$CREDS_FILE" 2>/dev/null | head -1 | tr -d '"' || true)
    existing_pm_wallet=$(grep -oP 'PM_USDC_WALLET_ADDRESS=\K[^"]+' "$CREDS_FILE" 2>/dev/null | head -1 | tr -d '"' || true)
    existing_kraken_key=$(grep -oP 'KRAKEN_API_KEY=\K[^"]+' "$CREDS_FILE" 2>/dev/null | head -1 | tr -d '"' || true)
    existing_kraken_secret=$(grep -oP 'KRAKEN_API_SECRET=\K[^"]+' "$CREDS_FILE" 2>/dev/null | head -1 | tr -d '"' || true)
fi

prompt_secret() {
    local label="$1"
    local existing="$2"
    local hint="$3"
    local var
    if [ -n "$existing" ]; then
        read -rsp "  $label (currently set, hit Enter to keep): " var
    else
        read -rsp "  $label $hint: " var
    fi
    echo ""
    if [ -z "$var" ] && [ -n "$existing" ]; then
        echo "$existing"
    else
        echo "$var"
    fi
}

echo "── Polymarket US ──"
pm_key=$(prompt_secret "PM_API_KEY" "$existing_pm_key" "(from polymarket.com → API)")
pm_secret=$(prompt_secret "PM_SECRET" "$existing_pm_secret" "(HMAC secret)")
pm_wallet=$(prompt_secret "PM_USDC_WALLET_ADDRESS" "$existing_pm_wallet" "(your deposit address)")

echo ""
echo "── Kraken Pro ──"
kraken_key=$(prompt_secret "KRAKEN_API_KEY" "$existing_kraken_key" "(from kraken.com → Settings → API)")
kraken_secret=$(prompt_secret "KRAKEN_API_SECRET" "$existing_kraken_secret" "(base64 private key)")

# ── Write the drop-in ─────────────────────────────────────────────────────
# Mode 600 from the start (umask + chmod) so values are never readable
# by non-root, even transiently.
umask 077
cat > "$CREDS_FILE" <<EOF
[Service]
# innait hedge-venue credentials — installed $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Do NOT commit this file. It is mode 600 + root-owned.
EOF

[ -n "$pm_key" ]        && echo "Environment=\"PM_API_KEY=$pm_key\"" >> "$CREDS_FILE"
[ -n "$pm_secret" ]     && echo "Environment=\"PM_SECRET=$pm_secret\"" >> "$CREDS_FILE"
[ -n "$pm_wallet" ]     && echo "Environment=\"PM_USDC_WALLET_ADDRESS=$pm_wallet\"" >> "$CREDS_FILE"
[ -n "$kraken_key" ]    && echo "Environment=\"KRAKEN_API_KEY=$kraken_key\"" >> "$CREDS_FILE"
[ -n "$kraken_secret" ] && echo "Environment=\"KRAKEN_API_SECRET=$kraken_secret\"" >> "$CREDS_FILE"

chmod 600 "$CREDS_FILE"
chown root:root "$CREDS_FILE"

echo ""
echo "── installed at $CREDS_FILE (mode 600, root-owned) ──"
echo ""
ls -l "$CREDS_FILE"
echo ""
echo "── reloading systemd + restarting lip-maker ──"
systemctl daemon-reload
systemctl restart lip-maker.service

sleep 5
if systemctl is-active --quiet lip-maker.service; then
    echo "✓ lip-maker active"
else
    echo "✗ lip-maker NOT active — check 'journalctl -u lip-maker.service -n 30'"
    exit 1
fi

# ── verify env vars reached the process (without echoing values) ───────────
echo ""
echo "── credentials present in running process (values masked) ──"
pid=$(systemctl show -p MainPID --value lip-maker.service)
for var in PM_API_KEY PM_SECRET PM_USDC_WALLET_ADDRESS KRAKEN_API_KEY KRAKEN_API_SECRET; do
    if cat "/proc/$pid/environ" 2>/dev/null | tr '\0' '\n' | grep -q "^${var}="; then
        echo "  ✓ $var  set"
    else
        echo "  ⨯ $var  NOT set"
    fi
done

echo ""
echo "done. Adapters will read these via os.getenv() when their flags flip live."
echo ""
echo "Next steps:"
echo "  1. Test heartbeat:  python -c 'from execution.kraken_adapter import KrakenAdapter; print(KrakenAdapter(dry_run=False).heartbeat())'"
echo "  2. Same for PM:     python -c 'from execution.polymarket_adapter import PolymarketAdapter; print(PolymarketAdapter(dry_run=False).heartbeat())'"
echo "  3. Tiny manual test order on each venue before flipping AUTO_HEDGE_<venue>=true"
