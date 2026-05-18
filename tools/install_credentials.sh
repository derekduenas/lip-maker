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
    # CRITICAL: strip ALL whitespace and control chars from pasted value.
    # Clipboard pastes often include trailing newlines or surrounding spaces
    # that bash captures into `var`. If we write those into a systemd
    # Environment="VAR=$var" line, systemd silently rejects the whole line
    # and the env var never reaches the process. Worse, downstream diagnostic
    # output (e.g. systemd-analyze verify) may echo the malformed lines.
    # Strip via tr; reject any value that contains characters that aren't
    # valid in a credential string.
    var="$(printf '%s' "$var" | tr -d '[:space:]')"
    if [ -z "$var" ] && [ -n "$existing" ]; then
        echo "$existing"
    else
        echo "$var"
    fi
}

# Conservative pattern check before writing — values must look like
# real credentials, not e.g. an accidentally-pasted log line. Each must
# be 16+ chars of base64-ish / uuid-ish content. Reject obvious paste errors.
validate_creds() {
    local name="$1"
    local val="$2"
    # Empty is OK (user skipped this one)
    [ -z "$val" ] && return 0
    # Min length sanity (Kraken keys ~56 chars; PM keys >= 32; UUIDs 36)
    if [ ${#val} -lt 16 ]; then
        echo "  ⨯ $name failed validation: too short (${#val} chars). Aborting." >&2
        return 1
    fi
    # Must contain only valid credential chars (base64, hex, dash, underscore, slash, plus, equals)
    if ! printf '%s' "$val" | LC_ALL=C grep -qE '^[A-Za-z0-9_+/=.-]+$'; then
        echo "  ⨯ $name failed validation: contains unexpected characters. Aborting." >&2
        return 1
    fi
    return 0
}

echo "── Polymarket US ──"
pm_key=$(prompt_secret "PM_API_KEY" "$existing_pm_key" "(from polymarket.com → API)")
pm_secret=$(prompt_secret "PM_SECRET" "$existing_pm_secret" "(HMAC secret)")
pm_wallet=$(prompt_secret "PM_USDC_WALLET_ADDRESS" "$existing_pm_wallet" "(your deposit address)")

echo ""
echo "── Kraken Pro ──"
kraken_key=$(prompt_secret "KRAKEN_API_KEY" "$existing_kraken_key" "(from kraken.com → Settings → API)")
kraken_secret=$(prompt_secret "KRAKEN_API_SECRET" "$existing_kraken_secret" "(base64 private key)")

# ── Validate every value before writing — fail fast on malformed paste ─────
validate_creds "PM_API_KEY" "$pm_key" || exit 1
validate_creds "PM_SECRET" "$pm_secret" || exit 1
validate_creds "PM_USDC_WALLET_ADDRESS" "$pm_wallet" || exit 1
validate_creds "KRAKEN_API_KEY" "$kraken_key" || exit 1
validate_creds "KRAKEN_API_SECRET" "$kraken_secret" || exit 1

# ── Write the drop-in via a temp file, then validate, then move ──────────
# Two-stage write protects against partially-malformed config landing in
# the systemd config dir. If any line isn't a single line containing both
# the var name AND the value, we abort and shred the temp file BEFORE
# moving it into place — so /etc/systemd/.../credentials.conf never holds
# malformed content. systemd-analyze verify is NOT used because it echoes
# malformed lines (which contain the value).
umask 077
TMP_FILE="$(mktemp /tmp/innait-creds.XXXXXX)"
chmod 600 "$TMP_FILE"

cat > "$TMP_FILE" <<EOF
[Service]
# innait hedge-venue credentials — installed $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Do NOT commit this file. It is mode 600 + root-owned.
EOF

write_env_line() {
    local var="$1"
    local val="$2"
    [ -z "$val" ] && return 0
    # CRITICAL: build the line in memory and verify it's a SINGLE logical
    # line containing both name and value before writing. If we ever get
    # here with an embedded newline, abort — something is very wrong.
    local line
    line="Environment=\"${var}=${val}\""
    if printf '%s' "$line" | LC_ALL=C grep -q $'[\r\n]'; then
        echo "  ⨯ ${var}: embedded newline detected. Aborting write." >&2
        shred -u "$TMP_FILE" 2>/dev/null || rm -f "$TMP_FILE"
        exit 1
    fi
    # Verify the line literally starts with Environment="VAR= with no whitespace gap
    if ! printf '%s' "$line" | LC_ALL=C grep -qE "^Environment=\"${var}=[A-Za-z0-9_+/=.-]" ; then
        echo "  ⨯ ${var}: line does not match expected pattern. Aborting." >&2
        shred -u "$TMP_FILE" 2>/dev/null || rm -f "$TMP_FILE"
        exit 1
    fi
    printf '%s\n' "$line" >> "$TMP_FILE"
}

write_env_line "PM_API_KEY" "$pm_key"
write_env_line "PM_SECRET" "$pm_secret"
write_env_line "PM_USDC_WALLET_ADDRESS" "$pm_wallet"
write_env_line "KRAKEN_API_KEY" "$kraken_key"
write_env_line "KRAKEN_API_SECRET" "$kraken_secret"

# Final structural check on the temp file: line count = 3 (header) + Nvars,
# and every non-header line MUST start with Environment=
header_lines=3
data_lines=$(grep -c '^Environment=' "$TMP_FILE")
total_lines=$(wc -l < "$TMP_FILE")
expected_total=$((header_lines + data_lines))
if [ "$total_lines" -ne "$expected_total" ]; then
    echo "  ⨯ file structure check failed: $total_lines lines, expected $expected_total." >&2
    shred -u "$TMP_FILE" 2>/dev/null || rm -f "$TMP_FILE"
    exit 1
fi

# All checks passed — atomic move into place
mv "$TMP_FILE" "$CREDS_FILE"
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
