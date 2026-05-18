#!/usr/bin/env python3
"""pm_us_heartbeat.py — auth test for Polymarket US Ed25519-signed requests.

PM US auth model (per user-provided spec):
  X-PM-Access-Key: {keyId}
  X-PM-Timestamp: {unix_ms}
  X-PM-Signature: {base64_signature}

Signature: Ed25519 over (a canonicalized request payload). Most common
payload format for this style of auth is one of:
  (a) "{timestamp}{method}{path}{body}"          (concatenated)
  (b) "{method}\\n{path}\\n{timestamp}\\n{body}"  (newline-joined)
  (c) "{timestamp}{method}{path}"                (no body for GET)

We try (a), then (c) on GET. If both fail with 401/403, the format is
different and we'll iterate based on actual API response shape (status
code only — never the response body, which may echo signed payload).

Endpoints tried (read-only, no orders):
  Various candidates; we try a few standard ones and report which
  returns 200 vs 401/403 vs 404.

USAGE
  sudo /root/lip-maker/venv/bin/python tools/pm_us_heartbeat.py
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def safe_type(e: BaseException) -> str:
    return f"{type(e).__module__}.{type(e).__name__}"


def get_creds_from_lipmaker_env() -> dict[str, str]:
    """Pull credentials from /proc/PID/environ of the running service.
    Never prints values. Returns dict with only the keys present."""
    pid = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "lip-maker.service"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    env = {}
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        eq = entry.find(b"=")
        if eq > 0:
            name = entry[:eq].decode("ascii", errors="replace")
            if name in ("PM_API_KEY", "PM_SECRET"):
                env[name] = entry[eq + 1 :].decode("ascii", errors="replace")
    return env


def sign_request(secret_b64: str, payload: str) -> str:
    """Ed25519-sign `payload`, return base64 signature.

    Handles two common Ed25519 secret formats:
      - 32-byte raw private key (Ed25519 seed)
      - 64-byte libsodium signing key (seed + public, first 32 used)

    NEVER catches and prints exceptions with content."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    secret_bytes = base64.b64decode(secret_b64)
    if len(secret_bytes) == 64:
        # libsodium-style signing key: first 32 bytes are the seed
        seed = secret_bytes[:32]
    elif len(secret_bytes) == 32:
        seed = secret_bytes
    else:
        raise ValueError(f"unexpected secret byte length: {len(secret_bytes)}")
    key = Ed25519PrivateKey.from_private_bytes(seed)
    sig_bytes = key.sign(payload.encode("utf-8"))
    return base64.b64encode(sig_bytes).decode()


def try_endpoint(
    host: str, path: str, api_key: str, secret_b64: str,
    payload_format: str = "concat",
) -> tuple[int, str]:
    """Send a signed GET to host+path. Returns (http_status, note).
    `note` only contains payload-format label and exception TYPE name."""
    timestamp = str(int(time.time() * 1000))
    method = "GET"
    body = ""
    if payload_format == "concat":
        payload = f"{timestamp}{method}{path}{body}"
    elif payload_format == "newline":
        payload = f"{method}\n{path}\n{timestamp}\n{body}"
    elif payload_format == "no_body":
        payload = f"{timestamp}{method}{path}"
    else:
        return -1, f"unknown payload_format={payload_format}"

    try:
        signature = sign_request(secret_b64, payload)
    except BaseException as e:
        return -1, f"sign failed: {safe_type(e)}"

    headers = {
        "X-PM-Access-Key": api_key,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature,
        "User-Agent": "innait-pm-hb/1.0",
    }
    url = f"{host}{path}"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, f"ok (payload_format={payload_format})"
    except urllib.error.HTTPError as e:
        # status code is informative without echoing response body
        return e.code, f"http error {e.code} (payload_format={payload_format})"
    except urllib.error.URLError as e:
        return -1, f"network error: {safe_type(e.reason) if e.reason else 'unknown'}"
    except BaseException as e:
        return -1, f"unexpected: {safe_type(e)}"


def main() -> int:
    print()
    print("=" * 55)
    print("  PM US heartbeat — Ed25519 signing test")
    print("=" * 55)
    print()

    creds = get_creds_from_lipmaker_env()
    if "PM_API_KEY" not in creds or "PM_SECRET" not in creds:
        print("  ⨯ PM credentials not found in lip-maker process env")
        return 1
    print("  ✓ credentials sourced from lip-maker service env")
    print()

    api_key = creds["PM_API_KEY"]
    secret = creds["PM_SECRET"]

    # Per docs.polymarket.us:
    #   base URL candidates: api.polymarket.us OR api.prod.polymarketexchange.com
    #   signing payload: f"{timestamp}{method}{path}"  (no body for GET)
    #   timestamp: milliseconds since epoch
    #   primary heartbeat endpoint: GET /v1/whoami
    # Try both base hosts with the documented payload format.
    candidates = [
        ("https://api.polymarket.us", "/v1/whoami", "no_body"),
        ("https://api.prod.polymarketexchange.com", "/v1/whoami", "no_body"),
        # Fallback: positions endpoint mentioned in docs example
        ("https://api.polymarket.us", "/v1/portfolio/positions", "no_body"),
        ("https://api.prod.polymarketexchange.com", "/v1/portfolio/positions", "no_body"),
        # Balance endpoint
        ("https://api.polymarket.us", "/v1/accounts/balances", "no_body"),
        ("https://api.prod.polymarketexchange.com", "/v1/accounts/balances", "no_body"),
    ]

    print(f"trying {len(candidates)} endpoint candidates...")
    print()
    for host, path, fmt in candidates:
        status, note = try_endpoint(host, path, api_key, secret, fmt)
        icon = "✓" if status == 200 else ("?" if status == -1 else "·")
        print(f"  {icon} {host}{path:30s}  fmt={fmt:8s}  {note}")

    print()
    print("interpretation:")
    print("  200       → auth works at that endpoint + format")
    print("  401/403   → reached PM, signing format or perms wrong")
    print("  404       → endpoint path doesn't exist on that host")
    print("  network   → host wrong / not reachable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
