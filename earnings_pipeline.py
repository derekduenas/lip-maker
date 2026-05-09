"""SOVEREIGN EARNINGS — proper end-to-end mention market pipeline.

Architecture:
  1. fetch_transcript(url)      → raw text from Motley Fool
  2. ingest_transcript(...)     → store in transcripts DB table
  3. compute_base_rate(t, term) → Laplace-smoothed P(mentioned in next call)
  4. score_kalshi_event(t)      → for each live mention market, compute edge
  5. emit_trade_plan(...)       → ranked, sized, paper-execution-ready

Built: 2026-05-07. First production run: MCD + LYFT earnings May 7.
"""
from __future__ import annotations
import os, sys, re, sqlite3, json, time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Config ---
SOV = Path("/root/sovereign")
DB_PATH = SOV / "data" / "sovereign.db"
UA = "Mozilla/5.0 (compatible; INNAIT-Sovereign/1.0)"
TIMEOUT = 20

# Known transcript URLs per ticker (seed; expand via search later)
TRANSCRIPT_URLS = {
    "MCD": [
        ("2025-11-05", "Q3 2025", "https://www.fool.com/earnings/call-transcripts/2025/11/05/mcdonalds-mcd-q3-2025-earnings-call-transcript/"),
        ("2026-02-11", "Q4 2025", "https://www.fool.com/earnings/call-transcripts/2026/02/11/mcdonalds-mcd-q4-2025-earnings-call-transcript/"),
        ("2025-02-10", "Q4 2024", "https://www.fool.com/earnings/call-transcripts/2025/02/10/mcdonalds-mcd-q4-2024-earnings-call-transcript/"),
        ("2024-10-29", "Q3 2024", "https://www.fool.com/earnings/call-transcripts/2024/10/29/mcdonalds-mcd-q3-2024-earnings-call-transcript/"),
    ],
    "LYFT": [
        ("2025-02-11", "Q4 2024", "https://www.fool.com/earnings/call-transcripts/2025/02/11/lyft-lyft-q4-2024-earnings-call-transcript/"),
    ],
}

# --- DB helpers ---
def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


# --- 1. Transcript fetcher ---
def fetch_transcript(url: str) -> str | None:
    """Pull Motley Fool transcript page, extract article body text."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"  fetch_transcript({url[:60]}...): {e}")
        return None
    soup = BeautifulSoup(r.text, "lxml")
    for cls in ("article-body", "tailwind-article-body", "article__body"):
        body = soup.find(class_=cls)
        if body:
            text = body.get_text(separator=" ", strip=True)
            if len(text) > 1000:
                return text
    # Fallback: <article>
    art = soup.find("article")
    if art:
        text = art.get_text(separator=" ", strip=True)
        if len(text) > 1000:
            return text
    return None


# --- 2. Ingest transcripts ---
def ingest_transcript(ticker: str, event_date: str, raw_text: str, label: str = "") -> int | None:
    """Store transcript in DB. Returns transcript_id or None if duplicate."""
    event_type = f"{ticker.lower()}_earnings"
    conn = _conn()
    # Check duplicate
    existing = conn.execute(
        "SELECT id FROM transcripts WHERE event_type=? AND event_date=?",
        (event_type, event_date),
    ).fetchone()
    if existing:
        conn.close()
        return existing[0]
    cur = conn.execute(
        """INSERT INTO transcripts(source, speaker, event_type, event_date, raw_text, word_count, ingested_at)
           VALUES(?,?,?,?,?,?,?)""",
        ("motley_fool", ticker.lower(), event_type, event_date, raw_text,
         len(raw_text.split()), datetime.now(timezone.utc).isoformat()),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


# --- 3. Term mention extractor ---
def term_mentioned(text: str, term: str) -> bool:
    """Case-insensitive whole-phrase match. Handles multi-word terms."""
    if not text or not term:
        return False
    pattern = r"\b" + re.escape(term.lower()) + r"\b"
    return bool(re.search(pattern, text.lower()))


def compute_base_rate(ticker: str, term: str) -> dict:
    """Laplace-smoothed base rate from DB transcripts.
    Returns {prob, k, n, source: 'corpus'|'no_corpus'}."""
    event_type = f"{ticker.lower()}_earnings"
    conn = _conn()
    rows = conn.execute(
        "SELECT raw_text FROM transcripts WHERE event_type=?", (event_type,)
    ).fetchall()
    conn.close()
    n = len(rows)
    if n == 0:
        return {"prob": None, "k": 0, "n": 0, "source": "no_corpus"}
    k = sum(1 for (txt,) in rows if term_mentioned(txt, term))
    prob = (k + 1) / (n + 2)   # Laplace smoothing
    return {"prob": prob, "k": k, "n": n, "source": "corpus"}


# --- 4. Score Kalshi mention markets ---
def score_kalshi_event(ticker: str) -> list[dict]:
    """For all live Kalshi mention markets matching this ticker, compute edge."""
    sys.path.insert(0, str(SOV))
    from engine.scanner import KalshiClient
    c = KalshiClient()
    series_pfx = f"KXEARNINGSMENTION{ticker.upper()}"
    all_mkts = c.get_mention_markets()
    target = [m for m in all_mkts if m.get("ticker", "").startswith(series_pfx)]
    out = []
    for m in target:
        sub = m.get("subtitle") or m.get("yes_sub_title") or ""
        if not sub:
            continue
        br = compute_base_rate(ticker, sub)
        if br["prob"] is None:
            continue
        ya = m.get("yes_ask")
        na = m.get("no_ask")
        try:
            ya = float(ya) if ya is not None else None
            na = float(na) if na is not None else None
        except: ya = na = None
        # Maker fee approx
        def fee(p): return max(0.0044, 0.0175 * p * (1 - p)) if p else 0
        yes_edge = (br["prob"] - ya - fee(ya)) if ya is not None else None
        no_edge = ((1 - br["prob"]) - na - fee(na)) if na is not None else None
        side, edge, price = None, -99, None
        if yes_edge is not None and yes_edge > edge:
            side, edge, price = "YES", yes_edge, ya
        if no_edge is not None and no_edge > edge:
            side, edge, price = "NO", no_edge, na
        out.append({
            "ticker_kalshi": m["ticker"], "term": sub,
            "base_rate": round(br["prob"], 4),
            "k": br["k"], "n": br["n"],
            "yes_ask": ya, "no_ask": na,
            "side": side, "price": price,
            "edge": round(edge, 4) if edge != -99 else None,
        })
    out.sort(key=lambda x: -(x["edge"] or -99))
    return out


# --- 5. End-to-end runner ---
def run_for_ticker(ticker: str) -> dict:
    print(f"\n=== {ticker} ===")
    urls = TRANSCRIPT_URLS.get(ticker.upper(), [])
    if not urls:
        print(f"  No transcript URLs known for {ticker}. Skipping ingest.")
    else:
        for date, label, url in urls:
            existing = _conn().execute(
                "SELECT id FROM transcripts WHERE event_type=? AND event_date=?",
                (f"{ticker.lower()}_earnings", date),
            ).fetchone()
            if existing:
                print(f"  ✓ {label} already in DB")
                continue
            print(f"  fetching {label}...", end=" ", flush=True)
            text = fetch_transcript(url)
            if text:
                tid = ingest_transcript(ticker, date, text, label)
                print(f"OK ({len(text)} chars, tid={tid})")
            else:
                print("FAIL")
            time.sleep(0.5)
    # Score live markets
    print(f"\n  Scoring live Kalshi mention markets for {ticker}...")
    opps = score_kalshi_event(ticker)
    print(f"  Got {len(opps)} markets with computable edge")
    return {"ticker": ticker, "opps": opps, "n_transcripts": len(urls)}


def main():
    results = {}
    for tkr in ["MCD", "LYFT"]:
        results[tkr] = run_for_ticker(tkr)

    # Combined ranked output
    all_opps = []
    for tkr, r in results.items():
        for o in r["opps"]:
            o["co"] = tkr
            all_opps.append(o)
    all_opps.sort(key=lambda x: -(x["edge"] or -99))

    print()
    print("=" * 110)
    print("RANKED OPPORTUNITIES — REAL CORPUS BASE RATES")
    print("=" * 110)
    print(f"  {'CO':<5} {'TERM':<22} {'BASE':>6} {'K/N':>7} {'YES_$':>6} {'NO_$':>6} {'SIDE':<5} {'EDGE':>7}")
    for o in all_opps[:30]:
        if o["edge"] is None or o["edge"] < -0.5: continue
        flag = "🔥" if o["edge"] >= 0.15 else ("✓" if o["edge"] >= 0.05 else " ")
        kn = f"{o['k']}/{o['n']}"
        ya = f"{o['yes_ask']:.2f}" if o['yes_ask'] is not None else "  --"
        na = f"{o['no_ask']:.2f}" if o['no_ask'] is not None else "  --"
        print(f"  {flag} {o['co']:<3} {o['term'][:20]:<22} {o['base_rate']*100:>5.0f}%  {kn:>7} {ya:>6} {na:>6} {o['side']:<5} {o['edge']*100:>+5.1f}%")

    n_high = sum(1 for o in all_opps if o["edge"] and o["edge"] >= 0.15)
    n_med = sum(1 for o in all_opps if o["edge"] and 0.05 <= o["edge"] < 0.15)
    print(f"\n  HIGH (edge≥15%): {n_high}   MED (5-15%): {n_med}")

    out_path = SOV / "data" / "earnings_corpus_score_may7.json"
    with open(out_path, "w") as f:
        json.dump({"results": results, "ranked": all_opps}, f, indent=2)
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
