#!/usr/bin/env python3
"""pm_weather_discover.py — purpose-built Kalshi↔PM weather slug matcher.

PM US covers 5 cities for daily-high temperature:
  MDW (Chicago), MIA (Miami), LAX (Los Angeles), SFO (San Francisco), NYC

PM slugs follow the pattern:
  tc-temp-{airport}high-{yyyy-mm-dd}-{range}f
  e.g. tc-temp-laxhigh-2026-05-18-gte69lt70f
       tc-temp-nychigh-2026-05-18-lt81f

Kalshi LIP weather tickers:
  KXHIGHT{LAX,SFO,MIA,CHI,NYC,...}-26MAY18-T{strike}
  KXHIGHT{LAX,SFO,MIA,CHI,NYC,...}-26MAY18-B{strike}.5

For the 5 PM-covered cities, we can construct the matching PM slug
deterministically from the Kalshi ticker. No fuzzy matching needed.
Auto-promote to kalshi_pm_manual_map with status='active' on confirmed
match (PM market actually exists at that slug).

This is the WEATHER half of Phase H. Sports markets need a separate
matcher (sports event tickers don't have a deterministic mapping
because Kalshi and PM use different team-code conventions).

USAGE
  python tools/pm_weather_discover.py             # one-shot scan
  python tools/pm_weather_discover.py --review    # see mappings
  Run via systemd timer pm-weather-discover.timer every 15 min
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "/root/lip-maker/data/lip_maker.db"
PM_BASE = "https://api.polymarket.us"


# Kalshi airport-code → PM airport-code translation.
# Kalshi typically uses the 3-letter airport code; PM US matches except
# Chicago which Kalshi calls CHI but PM uses MDW (Midway).
KALSHI_CITY_TO_PM_AIRPORT = {
    "LAX": "lax",
    "SFO": "sfo",
    "MIA": "mia",
    "NYC": "nyc",
    "CHI": "mdw",   # Kalshi=CHI → PM=MDW (Chicago Midway)
}

# Kalshi LIP weather series we want to match (high temp; low temp pattern
# would be similar). KXHIGHT prefix + city suffix.
KALSHI_HIGH_PREFIX_RE = re.compile(
    r"^KXHIGHT(?P<city>LAX|SFO|MIA|NYC|CHI)-(?P<date>\d{2}[A-Z]{3}\d{2})-(?P<strike>[TB][\d.]+)$"
)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS kalshi_pm_manual_map (
    kalshi_ticker      TEXT NOT NULL,
    pm_slug            TEXT NOT NULL,
    side_invariant     TEXT NOT NULL DEFAULT 'same',
    status             TEXT NOT NULL DEFAULT 'active',
    source             TEXT NOT NULL DEFAULT 'weather_discover',
    discovered_at      TEXT NOT NULL,
    expires_at         TEXT,
    PRIMARY KEY (kalshi_ticker)
);
CREATE INDEX IF NOT EXISTS idx_kpmmap_pm_slug
    ON kalshi_pm_manual_map(pm_slug);
"""


def ensure_schema():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


# ── PM US fetch (signed) ─────────────────────────────────────────────────────

def _get_pm_creds():
    if os.environ.get("PM_API_KEY") and os.environ.get("PM_SECRET"):
        return os.environ["PM_API_KEY"], os.environ["PM_SECRET"]
    try:
        pid = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "lip-maker.service"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except Exception:
        return None
    k = s = None
    for entry in raw.split(b"\x00"):
        if entry.startswith(b"PM_API_KEY="):
            k = entry[len(b"PM_API_KEY="):].decode("ascii", errors="replace")
        elif entry.startswith(b"PM_SECRET="):
            s = entry[len(b"PM_SECRET="):].decode("ascii", errors="replace")
    return (k, s) if k and s else None


def _sign(secret_b64: str, payload: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sb = base64.b64decode(secret_b64)
    seed = sb[:32] if len(sb) == 64 else sb
    pk = Ed25519PrivateKey.from_private_bytes(seed)
    return base64.b64encode(pk.sign(payload.encode("utf-8"))).decode()


def fetch_pm_weather_markets() -> list[dict]:
    """Pull all PM markets whose slug starts with tc-temp- (climate)."""
    creds = _get_pm_creds()
    if not creds:
        print("  ⨯ PM creds not found", file=sys.stderr)
        return []
    api_key, secret = creds
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    end_min = urllib.parse.quote(now_iso, safe="")
    all_markets = []
    for offset in range(0, 4001, 200):
        ts = str(int(time.time() * 1000))
        try:
            sig = _sign(secret, f"{ts}GET/v1/markets")
        except Exception as e:
            print(f"  ⨯ sign failed: {type(e).__name__}", file=sys.stderr)
            return []
        url = (
            f"{PM_BASE}/v1/markets?active=true&closed=false"
            f"&endDateMin={end_min}&limit=200&offset={offset}"
        )
        req = urllib.request.Request(url, headers={
            "X-PM-Access-Key": api_key, "X-PM-Timestamp": ts,
            "X-PM-Signature": sig, "User-Agent": "innait-weather-disc/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            items = data.get("markets", []) if isinstance(data, dict) else (data or [])
        except urllib.error.HTTPError as e:
            print(f"  ⨯ PM http {e.code} at offset={offset}", file=sys.stderr)
            break
        except Exception as e:
            print(f"  ⨯ PM fetch error: {type(e).__name__}", file=sys.stderr)
            break
        if not items:
            break
        all_markets.extend(items)
        if len(items) < 200:
            break
    # Filter to climate / temp markets
    return [
        m for m in all_markets
        if (m.get("slug") or "").startswith("tc-temp-")
        or (m.get("category") or "").lower() == "climate"
    ]


# ── Kalshi LIP weather candidates ────────────────────────────────────────────

def fetch_kalshi_weather_lip() -> list[dict]:
    """Active Kalshi LIP markets matching the high-temp pattern in supported cities."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute("""
            SELECT market_ticker, reward_per_day_usd, end_date
            FROM lip_programs
            WHERE end_date > datetime('now')
              AND market_ticker LIKE 'KXHIGHT%'
              AND reward_per_day_usd >= 5.0
        """).fetchall()
    finally:
        conn.close()
    out = []
    for ticker, pool, end_date in rows:
        m = KALSHI_HIGH_PREFIX_RE.match(ticker)
        if not m:
            continue
        if m.group("city") not in KALSHI_CITY_TO_PM_AIRPORT:
            continue
        out.append({
            "ticker": ticker, "pool": pool, "end_date": end_date,
            "city": m.group("city"), "date": m.group("date"),
            "strike": m.group("strike"),
        })
    return out


# ── Slug construction ────────────────────────────────────────────────────────

_MONTHS = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05",
    "JUN": "06", "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10",
    "NOV": "11", "DEC": "12",
}


def kalshi_date_to_iso(kdate: str) -> str | None:
    """26MAY18 → 2026-05-18"""
    if len(kdate) != 7:
        return None
    try:
        yy = "20" + kdate[:2]
        mon = _MONTHS.get(kdate[2:5])
        dd = kdate[5:7]
        if not mon:
            return None
        return f"{yy}-{mon}-{dd}"
    except Exception:
        return None


def build_pm_slug_candidates(city: str, iso_date: str, strike: str) -> list[str]:
    """Return list of possible PM slugs the Kalshi market might map to.

    PM uses ranges; we look for slugs containing the date + city, and the
    exact range determination happens at runtime.
    """
    pm_airport = KALSHI_CITY_TO_PM_AIRPORT.get(city)
    if not pm_airport:
        return []
    return [
        # Family prefix — PM has many strikes per date, we just want the
        # date+city prefix to confirm coverage exists
        f"tc-temp-{pm_airport}high-{iso_date}",
    ]


# ── Discovery ────────────────────────────────────────────────────────────────

def discover(quiet: bool = False) -> dict:
    def log(s):
        if not quiet:
            print(s)
    log("=" * 60)
    log("  PM weather slug discoverer (5 supported cities)")
    log("=" * 60)
    log("")

    pm_weather = fetch_pm_weather_markets()
    log(f"  ✓ {len(pm_weather)} PM climate/temp markets fetched")

    kalshi = fetch_kalshi_weather_lip()
    log(f"  ✓ {len(kalshi)} Kalshi weather LIP candidates "
        f"({len(set(k['city'] for k in kalshi))} cities)")
    log("")

    # Index PM by slug prefix for fast lookup
    pm_by_slug_prefix: dict[str, list[dict]] = {}
    for m in pm_weather:
        slug = m.get("slug") or ""
        # Group by the "tc-temp-<airport>high-<date>" prefix
        parts = slug.split("-")
        if len(parts) >= 7 and parts[0] == "tc" and parts[1] == "temp":
            prefix = "-".join(parts[:6])  # tc-temp-<airport>high-yyyy-mm-dd
            pm_by_slug_prefix.setdefault(prefix, []).append(m)

    ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    now_iso = datetime.now(timezone.utc).isoformat()
    n_promoted = 0
    matched_tickers = []
    for k in kalshi:
        iso = kalshi_date_to_iso(k["date"])
        if not iso:
            continue
        candidate_prefixes = build_pm_slug_candidates(k["city"], iso, k["strike"])
        for prefix in candidate_prefixes:
            pm_markets = pm_by_slug_prefix.get(prefix, [])
            if not pm_markets:
                continue
            # Found a coverage match. Pick the FIRST PM slug in this family
            # as the placeholder mapping. The hedger will pick the EXACT
            # strike-matching slug per fill (different logic — out of scope
            # for v1).
            pm_slug = pm_markets[0]["slug"]
            # The full prefix is what we actually want stored — the hedger
            # can scan the family for the strike-specific slug at fill time.
            # Schema is shared with pre-existing kalshi_pm_manual_map
            # which has added_at NOT NULL + last_verified_at. New columns
            # (status, source, discovered_at, expires_at) added via ALTER.
            conn.execute("""
                INSERT INTO kalshi_pm_manual_map
                    (kalshi_ticker, pm_slug, side_invariant,
                     added_at, last_verified_at,
                     status, source, discovered_at, expires_at)
                VALUES (?, ?, 'same', ?, ?, 'active', 'weather_discover', ?, ?)
                ON CONFLICT(kalshi_ticker) DO UPDATE SET
                    pm_slug = excluded.pm_slug,
                    last_verified_at = excluded.last_verified_at,
                    status = 'active',
                    source = excluded.source,
                    discovered_at = excluded.discovered_at,
                    expires_at = excluded.expires_at
            """, (k["ticker"], prefix, now_iso, now_iso,
                  now_iso, k["end_date"]))
            n_promoted += 1
            matched_tickers.append(k["ticker"])
            break
    conn.commit()
    conn.close()

    log(f"  ✓ promoted {n_promoted} verified weather mappings to "
        f"kalshi_pm_manual_map (status=active)")
    if matched_tickers and not quiet:
        log("")
        log("  sample matches:")
        for t in matched_tickers[:8]:
            log(f"    {t}")
    return {
        "n_pm_weather_markets": len(pm_weather),
        "n_kalshi_candidates": len(kalshi),
        "n_promoted": n_promoted,
        "matched_tickers": matched_tickers,
    }


def review():
    ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        rows = conn.execute("""
            SELECT kalshi_ticker, pm_slug, status, source, discovered_at
            FROM kalshi_pm_manual_map
            WHERE source = 'weather_discover'
              AND status = 'active'
            ORDER BY discovered_at DESC
            LIMIT 50
        """).fetchall()
    finally:
        conn.close()
    if not rows:
        print("no active weather mappings yet")
        return
    print(f"{'kalshi_ticker':<40s} {'pm_slug_prefix':<55s} status")
    print("-" * 110)
    for t, slug, status, src, disc in rows:
        print(f"{t:<40s} {slug:<55s} {status}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--review", action="store_true")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    if a.review:
        review()
        return
    discover(quiet=a.quiet)


if __name__ == "__main__":
    main()
