import os, subprocess, base64, binascii

pid = subprocess.run(
    ["systemctl", "show", "-p", "MainPID", "--value", "lip-maker.service"],
    capture_output=True, text=True, check=True,
).stdout.strip()

with open(f"/proc/{pid}/environ", "rb") as f:
    raw = f.read()

secret = None
api_key = None
for entry in raw.split(b"\x00"):
    if entry.startswith(b"PM_SECRET="):
        secret = entry[len(b"PM_SECRET="):].decode("ascii", errors="replace")
    elif entry.startswith(b"PM_API_KEY="):
        api_key = entry[len(b"PM_API_KEY="):].decode("ascii", errors="replace")

if secret is None:
    print("PM_SECRET not in env")
    exit(1)

print(f"PM_API_KEY length: {len(api_key) if api_key else 'MISSING'} chars")
print(f"PM_SECRET length:  {len(secret)} chars")
print()
print("PM_SECRET shape (charset only, no content):")
print(f"  alnum_only:    {secret.isalnum()}")
print(f"  has '-':       {'-' in secret}")
print(f"  has '_':       {'_' in secret}")
print(f"  has '/':       {'/' in secret}")
print(f"  has '+':       {'+' in secret}")
print(f"  has '=':       {'=' in secret}")
print(f"  has '.':       {'.' in secret}")
print(f"  has 'BEGIN':   {'BEGIN' in secret}")
print()
print("decode attempts (target = 32 bytes for Ed25519):")

try:
    b = base64.b64decode(secret)
    label = "OK 32-byte Ed25519 key" if len(b) == 32 else f"wrong size for Ed25519"
    print(f"  std base64:    decodes to {len(b)} bytes ({label})")
except binascii.Error:
    print(f"  std base64:    fails (binascii.Error)")

try:
    b = base64.urlsafe_b64decode(secret)
    label = "OK 32-byte Ed25519 key" if len(b) == 32 else f"wrong size for Ed25519"
    print(f"  urlsafe b64:   decodes to {len(b)} bytes ({label})")
except binascii.Error:
    print(f"  urlsafe b64:   fails")

try:
    cleaned = secret.replace("0x", "").strip()
    b = bytes.fromhex(cleaned)
    label = "OK 32-byte Ed25519 key" if len(b) == 32 else f"wrong size for Ed25519"
    print(f"  hex:           decodes to {len(b)} bytes ({label})")
except ValueError:
    print(f"  hex:           fails")

print(f"  PEM:           {'looks like PEM' if 'BEGIN' in secret else 'not PEM'}")

if "." in secret:
    parts = secret.split(".")
    print(f"  dotted:        {len(parts)} segments of lengths {[len(p) for p in parts]}")

# Try base64 with padding fix (some APIs strip = padding)
needs_pad = len(secret) % 4
if needs_pad:
    padded = secret + "=" * (4 - needs_pad)
    try:
        b = base64.b64decode(padded)
        label = "OK 32-byte Ed25519 key" if len(b) == 32 else f"wrong size for Ed25519"
        print(f"  b64 +padding:  decodes to {len(b)} bytes ({label})")
    except binascii.Error:
        pass
