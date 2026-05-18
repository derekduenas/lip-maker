#!/usr/bin/env python3
"""pm_match_discover.py — auto-discover Kalshi ↔ Polymarket US slug pairings.

Fetches active PM US markets, fuzzy-matches each active Kalshi LIP market
(restricted to series in HEDGE_MAP with venue=Polymarket), writes top
candidates to a `kalshi_pm_match_candidates` table for operator review.

Match heuristic:
  similarity = jaccard_tokens(kalshi_title, pm_question) * 0.7
             + date_proximity_score(kalshi_close, pm_endDate) * 0.3
  candidates with similarity > 0.30 are stored
  candidates with similarity > 0.70 are flagged as high-confidence

USAGE
  python tools/pm_match_discover.py              # discover + write candidates
  python tools/pm_match_discover.py --review     # print pending candidates
  python tools/pm_match_discover.py --reset      # clear candidates table
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "/root/lip-maker/data/lip_maker.db"
PM_BASE = "https://api.polymarket.us"
PM_LIMIT = 200  # per-page

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS kalshi_pm_match_candidates (
    kalshi_ticker      TEXT NOT NULL,
    pm_slug            TEXT NOT NULL,
    pm_question        TEXT,
    pm_end_date        TEXT,
    similarity         REAL NOT NULL,
    discovered_at      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    reviewed_at        TEXT,
    PRIMARY KEY (kalshi_ticker, pm_slug)
);
CREATE INDEX IF NOT EXISTS idx_pm_match_status
    ON kalshi_pm_match_candidates(status, similarity DESC);
"""


def ensure_schema():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


_TOKEN_RE = re.compile(r"\b[a-zA-Z0-9]+\b")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "by", "be", "is", "are", "was", "were", "this", "that",
    "will", "would", "could", "may", "might", "should",
    "any", "some", "all", "no", "yes", "not", "do", "does", "did",
    "kxapr", "kxvote", "kx",  # kalshi-specific prefixes
}


def tokenize(s: str) -> set[str]:
    if not s:
        return set()
    tokens = [t.lower() for t in _TOKEN_RE.findall(s)]
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_score(kalshi_tokens: set[str], pm_tokens: set[str]) -> float:
    """Coverage of Kalshi hint tokens by PM tokens. More forgiving than
    Jaccard when PM titles are much richer than Kalshi tickers."""
    if not kalshi_tokens:
        return 0.0
    return len(kalshi_tokens & pm_tokens) / len(kalshi_tokens)


def date_proximity_score(kalshi_iso: str | None, pm_iso: str | None) -> float:
    """Return 1.0 if dates within 24h, decaying to 0 at 30 days."""
    if not kalshi_iso or not pm_iso:
        return 0.0
    try:
        kd = datetime.fromisoformat(kalshi_iso.replace("Z", "+00:00"))
        pd = datetime.fromisoformat(pm_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    delta_days = abs((kd - pd).total_seconds() / 86400.0)
    if delta_days <= 1:
        return 1.0
    if delta_days >= 30:
        return 0.0
    return 1.0 - (delta_days - 1) / 29.0


def _get_pm_creds() -> tuple[str, str] | None:
    """Pull PM_API_KEY + PM_SECRET from lip-maker service env. Never logs."""
    import os, subprocess
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
    creds = {}
    for entry in raw.split(b"\x00"):
        if entry.startswith(b"PM_API_KEY="):
            creds["key"] = entry[len(b"PM_API_KEY="):].decode("ascii", errors="replace")
        elif entry.startswith(b"PM_SECRET="):
            creds["secret"] = entry[len(b"PM_SECRET="):].decode("ascii", errors="replace")
    if "key" in creds and "secret" in creds:
        return creds["key"], creds["secret"]
    return None


def _sign_pm(secret_b64: str, payload: str) -> str:
    """Ed25519 sign + base64. Used by PM US auth. Never echoes content."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    secret_bytes = base64.b64decode(secret_b64)
    seed = secret_bytes[:32] if len(secret_bytes) == 64 else secret_bytes
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return base64.b64encode(key.sign(payload.encode("utf-8"))).decode()


def fetch_pm_markets() -> list[dict]:
    """Fetch active PM US markets with Ed25519-signed auth."""
    import base64, time
    creds = _get_pm_creds()
    if creds is None:
        print("  ⨯ PM credentials not found in lip-maker service env",
              file=sys.stderr)
        return []
    api_key, secret = creds

    results = []
    offset = 0
    while True:
        # Sign the path WITHOUT query string (PM US convention per testing).
        # Full path-with-query goes in the URL.
        path_for_sign = "/v1/markets"
        full_path = (
            f"/v1/markets?active=true&closed=false"
            f"&limit={PM_LIMIT}&offset={offset}"
        )
        timestamp = str(int(time.time() * 1000))
        payload = f"{timestamp}GET{path_for_sign}"
        try:
            signature = _sign_pm(secret, payload)
        except Exception as e:
            print(f"  ⨯ signing failed: {type(e).__name__}", file=sys.stderr)
            return []
        url = f"{PM_BASE}{full_path}"
        req = urllib.request.Request(url, headers={
            "X-PM-Access-Key": api_key,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature,
            "User-Agent": "innait-pm-disc/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  ⨯ PM /v1/markets http {e.code} at offset={offset}",
                  file=sys.stderr)
            break
        except Exception as e:
            print(f"  ⨯ PM fetch error: {type(e).__name__}", file=sys.stderr)
            break
        # PM US returns {"markets": [...]}; offshore PM/gamma uses data.
        # Be defensive about both.
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("markets") or data.get("data") or []
        else:
            items = []
        if not items:
            break
        results.extend(items)
        if len(items) < PM_LIMIT:
            break
        offset += PM_LIMIT
        if offset >= 2000:
            break
    return results


def fetch_active_kalshi_lip() -> list[dict]:
    """Active LIP markets where the SERIES has a PM HEDGE_MAP entry."""
    from cross_venue.market_match import hedge_for_ticker
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute("""
            SELECT market_ticker, reward_per_day_usd, end_date
            FROM lip_programs
            WHERE end_date > datetime('now')
              AND reward_per_day_usd >= 5.0
        """).fetchall()
    finally:
        conn.close()
    # Filter to PM-routed series only
    pm_targets = []
    for t, pool, end in rows:
        spec = hedge_for_ticker(t)
        if spec is not None and spec.hedge_venue == "Polymarket":
            pm_targets.append({"ticker": t, "pool": pool, "end_date": end})
    return pm_targets


def discover():
    print("=" * 60)
    print("  Kalshi ↔ PM US slug discovery")
    print("=" * 60)
    print()

    print("fetching PM US active markets (no auth needed)...")
    pm_markets = fetch_pm_markets()
    print(f"  ✓ {len(pm_markets)} PM markets fetched")
    print()

    print("fetching Kalshi LIP markets in PM-routed series...")
    kalshi = fetch_active_kalshi_lip()
    print(f"  ✓ {len(kalshi)} Kalshi candidates")
    print()

    # Precompute PM token sets
    pm_index = []
    for m in pm_markets:
        slug = m.get("slug") or ""
        question = m.get("question") or ""
        end_date = m.get("endDate") or m.get("end_date") or ""
        category = m.get("category") or ""
        if not slug or not question:
            continue
        tokens = tokenize(question + " " + slug + " " + category)
        pm_index.append({
            "slug": slug, "question": question,
            "end_date": end_date, "tokens": tokens,
        })

    ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=30000")
    n_written = 0
    n_high_conf = 0
    matches_per_kalshi = {}
    for k in kalshi:
        # Build Kalshi token bag from ticker (best we can do without titles)
        # The series prefix carries semantic info: KXAPRPOTUS → "approval potus"
        k_tokens = tokenize(k["ticker"].replace("KX", "").replace("-", " "))
        # Add domain hints based on series
        prefix = k["ticker"].split("-", 1)[0]
        prefix_hints = {
            "KXVOTEHUBTRUMPUPDOWN": {"trump", "approval", "weekly"},
            "KXAPRPOTUS": {"trump", "approval", "poll", "friday"},
            "KXTRUTHSOCIAL": {"trump", "truth", "social", "posts", "weekly"},
            "KXTRUMPACT": {"trump", "executive", "orders", "weekly", "eo"},
            "KXTRUMPENDORSEMENTS": {"trump", "endorse", "endorsement", "weekly"},
            "KXMAMDANIEO": {"mamdani", "executive", "order", "nyc"},
            "KXLAKECONF": {"lake", "kari", "confirmation", "ambassador"},
            "KXCPIYOY_PM": {"cpi", "inflation", "yoy"},
        }
        k_tokens |= prefix_hints.get(prefix, set())
        scored = []
        for pm in pm_index:
            # overlap_score = fraction of Kalshi tokens covered by PM tokens.
            # More forgiving when PM titles are richer than Kalshi tickers.
            sim_overlap = overlap_score(k_tokens, pm["tokens"])
            sim_date = date_proximity_score(k["end_date"], pm["end_date"])
            score = 0.7 * sim_overlap + 0.3 * sim_date
            if score >= 0.30:
                scored.append((score, pm))
        scored.sort(key=lambda x: -x[0])
        top = scored[:5]
        matches_per_kalshi[k["ticker"]] = len(top)
        for score, pm in top:
            now_iso = datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO kalshi_pm_match_candidates
                    (kalshi_ticker, pm_slug, pm_question, pm_end_date,
                     similarity, discovered_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(kalshi_ticker, pm_slug) DO UPDATE SET
                    pm_question = excluded.pm_question,
                    pm_end_date = excluded.pm_end_date,
                    similarity  = excluded.similarity,
                    discovered_at = excluded.discovered_at
                WHERE status = 'pending'
            """, (k["ticker"], pm["slug"], pm["question"], pm["end_date"],
                  score, now_iso))
            n_written += 1
            if score >= 0.70:
                n_high_conf += 1
    conn.commit()
    conn.close()
    print(f"wrote {n_written} candidate pairings (high-confidence: {n_high_conf})")
    print()

    # Coverage report
    kalshi_with_match = sum(1 for v in matches_per_kalshi.values() if v > 0)
    print("coverage:")
    print(f"  Kalshi PM-routed markets:          {len(kalshi)}")
    print(f"  ... with ≥1 candidate match:       {kalshi_with_match}")
    print(f"  ... unmatched:                     {len(kalshi) - kalshi_with_match}")


def review():
    ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        rows = conn.execute("""
            SELECT kalshi_ticker, pm_slug, pm_question, similarity, status
            FROM kalshi_pm_match_candidates
            WHERE status = 'pending'
            ORDER BY similarity DESC
            LIMIT 30
        """).fetchall()
    finally:
        conn.close()
    if not rows:
        print("no pending candidates")
        return
    print(f"{'kalshi_ticker':<40s} {'sim':>5s}  pm_question (slug)")
    print("-" * 100)
    for t, slug, q, sim, status in rows:
        q_short = (q[:55] + "...") if q and len(q) > 55 else (q or "")
        print(f"{t:<40s} {sim:>5.2f}  {q_short}")
        print(f"  {' ' * 40} slug: {slug}")


def reset():
    ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("DELETE FROM kalshi_pm_match_candidates WHERE status = 'pending'")
    conn.commit()
    conn.close()
    print("pending candidates cleared")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--review", action="store_true")
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()

    if a.reset:
        reset()
        return
    if a.review:
        review()
        return
    discover()


if __name__ == "__main__":
    main()
