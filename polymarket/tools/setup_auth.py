"""Polymarket API key setup — interactive, secrets never leave your terminal.

WHAT THIS DOES:
  1. Prompts you for API Key ID (visible in dev portal — public part)
  2. Prompts you to paste the private key (PEM format, multi-line)
  3. Writes:
       - config/polymarket_private_key.pem  (chmod 600)
       - .env  (with PM_API_KEY_ID line; no private key in env)
  4. Tests connectivity by calling /v1/portfolio/balance
  5. If you ever rotate keys, just re-run this script

USAGE:
  cd polymarket-maker
  python tools/setup_auth.py

  When prompted for the private key, paste the FULL PEM block including
  the -----BEGIN PRIVATE KEY----- and -----END PRIVATE KEY----- lines,
  then press Ctrl-D (or Ctrl-Z on Windows) to finish.

SECURITY:
  - Private key file gets chmod 600 (only you can read it)
  - Never logged, never written to git (.gitignore guards it)
  - Stays local — never sent to Claude or anywhere else
"""
from __future__ import annotations

import getpass
import os
import stat
import sys
from pathlib import Path

# Ensure project root in path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def gitignore_protect() -> None:
    """Make sure private key + .env are gitignored before any write."""
    gi = PROJECT_ROOT / ".gitignore"
    needed = [
        "config/polymarket_private_key.pem",
        ".env",
        "data/",
        "logs/",
        "__pycache__/",
        "*.pyc",
        "venv/",
    ]
    existing = gi.read_text().splitlines() if gi.exists() else []
    missing = [n for n in needed if n not in existing]
    if missing:
        with open(gi, "a") as f:
            for n in missing:
                f.write(n + "\n")
        print(f"  ✓ added {len(missing)} entries to .gitignore")


def prompt_key_id() -> str:
    print("\n[1/3] API KEY ID")
    print("  This is the PUBLIC identifier from polymarket.us/developer.")
    print("  Usually a UUID like 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'.")
    val = input("  paste API Key ID > ").strip()
    if not val:
        print("ERROR: empty key id")
        sys.exit(1)
    return val


def prompt_private_key() -> str:
    print("\n[2/3] PRIVATE KEY (PEM format)")
    print("  Paste the FULL key including BEGIN/END lines.")
    print("  When done, press Ctrl-D (Linux/Mac) or Ctrl-Z then Enter (Windows).")
    print("  ──────────────")
    lines = []
    try:
        while True:
            line = input()
            lines.append(line)
    except EOFError:
        pass
    pem = "\n".join(lines).strip()
    if "BEGIN" not in pem or "END" not in pem:
        print("\nERROR: doesn't look like a PEM key (missing BEGIN/END markers)")
        sys.exit(1)
    return pem + "\n"


def write_files(api_key_id: str, pem: str) -> tuple[Path, Path]:
    key_path = PROJECT_ROOT / "config" / "polymarket_private_key.pem"
    env_path = PROJECT_ROOT / ".env"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(pem)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    # Append to .env (don't clobber other entries)
    existing = env_path.read_text() if env_path.exists() else ""
    lines = [l for l in existing.splitlines() if not l.startswith("PM_API_KEY_ID=")]
    lines.append(f"PM_API_KEY_ID={api_key_id}")
    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    return key_path, env_path


def test_connectivity() -> None:
    print("\n[3/3] TEST CONNECTIVITY")
    try:
        from execution.pm_auth import PolymarketClient, PMAuthError
    except ImportError as e:
        print(f"  ERROR: import failed — {e}")
        print(f"  Run: pip install cryptography certifi websockets")
        sys.exit(1)
    c = PolymarketClient()
    try:
        bal = c.get_balance()
        print(f"  ✓ /v1/portfolio/balance returned: ${bal:.2f}")
    except PMAuthError as e:
        print(f"  🔴 BALANCE call failed: {e}")
        print()
        print(f"  This usually means one of:")
        print(f"  1. Wrong API base URL (try setting PM_REST_BASE in .env)")
        print(f"  2. Header names different (X-API-KEY vs Polymarket-Api-Key etc)")
        print(f"  3. Signature payload format different")
        print(f"  Check Polymarket developer docs and adjust execution/pm_auth.py")
        return
    try:
        orders = c.list_resting_orders()
        positions = c.list_positions()
        print(f"  ✓ resting orders:  {len(orders)}")
        print(f"  ✓ open positions:  {len(positions)}")
    except Exception as e:
        print(f"  🟡 portfolio queries partial: {e}")


def main() -> int:
    print("══════════════════════════════════════════════════════════════")
    print("  POLYMARKET-MAKER API KEY SETUP")
    print("══════════════════════════════════════════════════════════════")
    gitignore_protect()
    key_id = prompt_key_id()
    pem    = prompt_private_key()
    key_path, env_path = write_files(key_id, pem)
    print(f"\n  ✓ wrote private key:  {key_path}  (perm 600)")
    print(f"  ✓ wrote env:          {env_path}")
    test_connectivity()
    print("\n══ DONE ══")
    print(f"  If connectivity test PASSED:  run python tools/pm_account_check.py")
    print(f"  If connectivity test FAILED:  send me the error, I'll adjust pm_auth.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
