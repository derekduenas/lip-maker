#!/usr/bin/env python3
"""install_creds_safe.py — Python-only credential installer for innait.

Why this exists: bash + read -s + clipboard paste is fragile (trailing
newlines, leading spaces, accidental double-paste of the Environment=
wrapper) and downstream diagnostics (systemd-analyze verify, requests
InvalidHeader) can ECHO malformed value content in their error messages.

This script never echoes a value. Not on success, not on validation
failure, not on exception. Values exist only in:
  - Python's getpass() input buffer (process memory)
  - The written credentials.conf (mode 600, root only)
  - The lip-maker process env when it restarts

USAGE
-----
  sudo /root/lip-maker/venv/bin/python /root/lip-maker/tools/install_creds_safe.py
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import subprocess
import sys
import tempfile

DROPIN_DIR = "/etc/systemd/system/lip-maker.service.d"
CREDS_FILE = os.path.join(DROPIN_DIR, "credentials.conf")

# Per-credential validation patterns. Each must match a "clean credential
# string" — base64 / hex / dash / underscore / dot. Length floors prevent
# obviously-bad pastes.
VALIDATION = {
    "PM_API_KEY":          {"min_len": 16, "max_len": 256},
    "PM_SECRET":           {"min_len": 16, "max_len": 256},
    "KRAKEN_API_KEY":      {"min_len": 16, "max_len": 256},
    "KRAKEN_API_SECRET":   {"min_len": 16, "max_len": 256},
}
ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9_+/=.\-]+$")


def prompt_credential(label: str) -> str | None:
    """Prompt for a credential with silent input. Strips ALL whitespace
    automatically. Returns None on empty input (skip) or validation fail.

    NEVER echoes the value, even on failure — only the var name."""
    raw = getpass.getpass(f"  {label} (paste, then Enter; empty to skip): ")
    # Strip every whitespace char + control char, no matter where it lands
    cleaned = re.sub(r"\s+", "", raw)
    if not cleaned:
        return None
    config = VALIDATION[label]
    if len(cleaned) < config["min_len"]:
        print(f"  ⨯ {label}: too short ({len(cleaned)} chars, min {config['min_len']}). Skipping.",
              file=sys.stderr)
        return None
    if len(cleaned) > config["max_len"]:
        print(f"  ⨯ {label}: too long ({len(cleaned)} chars, max {config['max_len']}). Skipping.",
              file=sys.stderr)
        return None
    if not ALLOWED_CHARS.fullmatch(cleaned):
        # Don't echo what character was bad — just say "rejected"
        print(f"  ⨯ {label}: contains characters outside [A-Za-z0-9_+/=.-]. Skipping.",
              file=sys.stderr)
        return None
    return cleaned


def main() -> int:
    if os.geteuid() != 0:
        print("ERROR: must run as root (writes systemd drop-in).", file=sys.stderr)
        return 1

    print()
    print("=" * 55)
    print("  innait credential installer (Python — no shell layer)")
    print("=" * 55)
    print()
    print("Input is hidden. Press Enter on a prompt to skip that key.")
    print("All values are validated before write. Nothing is echoed.")
    print()

    print("── Polymarket US ──")
    pm_key = prompt_credential("PM_API_KEY")
    pm_secret = prompt_credential("PM_SECRET")

    print()
    print("── Kraken Pro ──")
    kraken_key = prompt_credential("KRAKEN_API_KEY")
    kraken_secret = prompt_credential("KRAKEN_API_SECRET")

    # Build the file content in memory (never on disk except for the final
    # atomic move). Each Environment= line is constructed via f-string so
    # there's no shell parsing or sed transformation possible.
    lines = [
        "[Service]",
        "# innait hedge-venue credentials — installed via install_creds_safe.py",
        "# Mode 600, root-owned. Never commit.",
    ]
    pairs = [
        ("PM_API_KEY", pm_key),
        ("PM_SECRET", pm_secret),
        ("KRAKEN_API_KEY", kraken_key),
        ("KRAKEN_API_SECRET", kraken_secret),
    ]
    n_written = 0
    for name, val in pairs:
        if val is None:
            continue
        # Final safety: ensure the value doesn't contain a quote that could
        # break the Environment= line. ALLOWED_CHARS already excludes ", but
        # belt-and-suspenders.
        if '"' in val or '\n' in val or '\r' in val:
            print(f"  ⨯ {name}: contains quote or newline — REFUSING to write.",
                  file=sys.stderr)
            return 1
        lines.append(f'Environment="{name}={val}"')
        n_written += 1

    if n_written == 0:
        print("No credentials provided. Aborting.", file=sys.stderr)
        return 1

    content = "\n".join(lines) + "\n"

    # Two-stage write: temp file (mode 600 from creation) → atomic mv.
    # tempfile.mkstemp creates with mode 600 by default on POSIX.
    os.makedirs(DROPIN_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".creds-", dir="/tmp", text=True)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # Structural sanity check: each non-header line starts with Environment=
        env_line_count = 0
        with open(tmp_path, "r") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if stripped.startswith("Environment="):
                    env_line_count += 1
        if env_line_count != n_written:
            print(f"  ⨯ structural mismatch (wrote {n_written}, found {env_line_count})",
                  file=sys.stderr)
            return 1
        # Atomic move
        os.replace(tmp_path, CREDS_FILE)
        os.chmod(CREDS_FILE, 0o600)
        os.chown(CREDS_FILE, 0, 0)  # root:root
    finally:
        if os.path.exists(tmp_path):
            # Shouldn't reach here on success — but if we did, scrub it
            try:
                with open(tmp_path, "wb") as f:
                    f.write(b"\x00" * 4096)
                os.unlink(tmp_path)
            except OSError:
                pass

    # Reload + restart (subprocess, no shell)
    print()
    print(f"installed: {CREDS_FILE} (mode 600, root:root, {n_written} env line(s))")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "lip-maker.service"], check=True)

    import time
    time.sleep(6)

    # Membership-only verification — never echo values
    pid_out = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "lip-maker.service"],
        capture_output=True, text=True, check=True,
    )
    pid = pid_out.stdout.strip()
    print()
    print(f"lip-maker.service: pid={pid}")

    # Read /proc/PID/environ AS BYTES; never decode or print value content
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            environ_bytes = f.read()
    except FileNotFoundError:
        print("  ⨯ could not read process environ (service not running?)", file=sys.stderr)
        return 1

    # Parse: split on \0, then check each entry's prefix
    var_names_present = set()
    for entry in environ_bytes.split(b"\x00"):
        if not entry:
            continue
        eq_idx = entry.find(b"=")
        if eq_idx > 0:
            var_names_present.add(entry[:eq_idx].decode("ascii", errors="replace"))

    print()
    print("env var membership in running process (no values):")
    all_set = True
    for name, val in pairs:
        if val is None:
            print(f"  · {name}  (skipped)")
            continue
        if name in var_names_present:
            print(f"  ✓ {name}")
        else:
            print(f"  ⨯ {name}  NOT in process env")
            all_set = False

    if all_set:
        print()
        print("done. Credentials are now in the running process.")
        print("AUTO_HEDGE_<venue>=true flags remain off — adapters stay dry-run")
        print("until you explicitly flip them in paper.conf.")
        return 0
    else:
        print("  one or more vars failed to load — investigate without echoing values",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
