"""SOVEREIGN EARNINGS v2 — god-mode end-to-end pipeline.

Improvements over v1:
  - YES + NO side edge (catches short opportunities)
  - 11 known MCD/LYFT transcript URLs (was 5)
  - Kelly-sized position recommendations (capped 5% bankroll/trade)
  - Confidence stratification (HIGH n>=6, MED 3-5, LOW <3)
  - Paper-trade-ready JSON output
  - Markdown report for human review

Designed for: MCD earnings May 7 morning, LYFT after-market.
"""
from __future__ import annotations
import os, sys, re, sqlite3, json, time, math
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOV = Path("/root/sovereign")
DB_PATH = SOV / "data" / "sovereign.db"
UA = "Mozilla/5.0 (compatible; INNAIT-Sovereign/2.0)"
TIMEOUT = 20
BANKROLL = 1000.0          # paper bankroll for sizing
KELLY_FRAC = 0.25         # quarter-Kelly
MAX_PCT_PER_TRADE = 0.05  # 5% bankroll cap per single trade

# Expanded URL registry — verified via search
TRANSCRIPT_URLS = {
    "MCD": [
        ("2024-10-29", "Q3 2024", "https://www.fool.com/earnings/call-transcripts/2024/10/29/mcdonalds-mcd-q3-2024-earnings-call-transcript/"),
        ("2025-02-10", "Q4 2024", "https://www.fool.com/earnings/call-transcripts/2025/02/10/mcdonalds-mcd-q4-2024-earnings-call-transcript/"),
        ("2025-05-01", "Q1 2025", "https://www.fool.com/earnings/call-transcripts/2025/05/01/mcdonalds-mcd-q1-2025-earnings-call-transcript/"),
        ("2025-07-30", "Q2 2025", "https://www.fool.com/earnings/call-transcripts/2025/07/30/mcdonalds-mcd-q2-2025-earnings-call-transcript/"),
        ("2025-11-05", "Q3 2025", "https://www.fool.com/earnings/call-transcripts/2025/11/05/mcdonalds-mcd-q3-2025-earnings-call-transcript/"),
        ("2026-02-11", "Q4 2025", "https://www.fool.com/earnings/call-transcripts/2026/02/11/mcdonalds-mcd-q4-2025-earnings-call-transcript/"),
    ],
    "LYFT": [
        ("2024-05-07", "Q1 2024", "https://www.fool.com/earnings/call-transcripts/2024/05/07/lyft-lyft-q1-2024-earnings-call-transcript/"),
        ("2025-02-11", "Q4 2024", "https://www.fool.com/earnings/call-transcripts/2025/02/11/lyft-lyft-q4-2024-earnings-call-transcript/"),
        ("2025-08-06", "Q2 2025", "https://www.fool.com/earnings/call-transcripts/2025/08/06/lyft-lyft-q2-2025-earnings-call-transcript/"),
        ("2025-11-05", "Q3 2025", "https://www.fool.com/earnings/call-transcripts/2025/11/05/lyft-lyft-q3-2025-earnings-call-transcript/"),
        ("2026-02-10", "Q4 2025", "https://www.fool.com/earnings/call-transcripts/2026/02/10/lyft-lyft-q4-2025-earnings-call-transcript/"),
    ],
}


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def fetch_transcript(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    for cls in ("article-body", "tailwind-article-body", "article__body"):
        body = soup.find(class_=cls)
        if body:
            text = body.get_text(separator=" ", strip=True)
            if len(text) > 1000:
                return text
    art = soup.find("article")
    if art:
        text = art.get_text(separator=" ", strip=True)
        if len(text) > 1000:
            return text
    return None


def ingest_transcript(ticker: str, event_date: str, raw_text: str) -> int | None:
    event_type = f"{ticker.lower()}_earnings"
    conn = _conn()
    existing = conn.execute(
        "SELECT id FROM transcripts WHERE event_type=? AND event_date=?",
        (event_type, event_date),
    ).fetchone()
    if existing:
        conn.close()
        return existing[0]
    cur = conn.execute(
        "INSERT INTO transcripts(source, speaker, event_type, event_date, raw_text, word_count, ingested_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("motley_fool", ticker.lower(), event_type, event_date, raw_text,
         len(raw_text.split()), datetime.now(timezone.utc).isoformat()),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def term_mentioned(text: str, term: str) -> bool:
    """Loose stem-match: catches term + plural, possessive, hyphenated forms.
    For multi-word phrases, requires exact phrase but allows trailing chars on last word.
    BUG FIX 2026-05-07: was strict \b{term}\b which missed 'dividends' (plural).
    Now uses \b{term}\w* which matches dividend, dividends, dividend-paying, etc."""
    if not text or not term:
        return False
    t = term.lower().strip()
    text_l = text.lower()
    # Multi-word phrase: just check substring within word boundaries
    if " " in t:
        # "supply chain" → match "supply chain" or "supply chains"
        pattern = r"\b" + re.escape(t) + r"\w*"
    else:
        # Single word: allow stem suffix (s, ed, ing, -hyphenated)
        pattern = r"\b" + re.escape(t) + r"\w*"
    return bool(re.search(pattern, text_l))


def compute_base_rate(ticker: str, term: str) -> dict:
    conn = _conn()
    rows = conn.execute(
        "SELECT raw_text FROM transcripts WHERE event_type=?",
        (f"{ticker.lower()}_earnings",),
    ).fetchall()
    conn.close()
    n = len(rows)
    if n == 0:
        return {"prob": None, "k": 0, "n": 0, "conf": "no_corpus"}
    k = sum(1 for (t,) in rows if term_mentioned(t, term))
    prob = (k + 1) / (n + 2)
    if n >= 6: conf = "HIGH"
    elif n >= 3: conf = "MED"
    else: conf = "LOW"
    return {"prob": prob, "k": k, "n": n, "conf": conf}


def kelly_size(edge: float, price: float, bankroll: float = BANKROLL) -> dict:
    """Kelly fraction × bankroll, capped. price in dollars (0-1)."""
    if edge <= 0 or price <= 0 or price >= 1:
        return {"contracts": 0, "stake_usd": 0, "kelly_frac": 0}
    # Edge in payout terms: pay $price, win (1 - price). Kelly: f* = (bp - q) / b
    # where b = (1 - price) / price, p = our prob = price + edge, q = 1 - p
    p = price + edge
    q = 1 - p
    b = (1 - price) / price
    if b <= 0:
        return {"contracts": 0, "stake_usd": 0, "kelly_frac": 0}
    f_star = (b * p - q) / b
    f_capped = min(max(0, f_star * KELLY_FRAC), MAX_PCT_PER_TRADE)
    stake = bankroll * f_capped
    contracts = int(stake / price)
    return {"contracts": contracts, "stake_usd": round(stake, 2), "kelly_frac": round(f_capped, 4)}


def score_kalshi_event(ticker: str) -> list[dict]:
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
        na = m.get("no_ask") or (m.get("no_price") if m.get("no_price") else None)
        try:
            ya = float(ya) if ya is not None else None
            na = float(na) if na is not None else None
        except: ya = na = None

        def fee(p): return max(0.0044, 0.0175 * p * (1 - p)) if p else 0
        yes_edge = (br["prob"] - ya - fee(ya)) if ya is not None else None
        no_edge = ((1 - br["prob"]) - na - fee(na)) if na is not None else None
        side, edge, price = None, None, None
        if yes_edge is not None and (edge is None or yes_edge > edge):
            side, edge, price = "YES", yes_edge, ya
        if no_edge is not None and (edge is None or no_edge > edge):
            side, edge, price = "NO", no_edge, na
        if edge is None:
            continue

        sizing = kelly_size(edge, price) if edge > 0 else {"contracts": 0, "stake_usd": 0, "kelly_frac": 0}
        out.append({
            "ticker_kalshi": m["ticker"], "term": sub,
            "base_rate": round(br["prob"], 4),
            "k": br["k"], "n": br["n"], "conf": br["conf"],
            "yes_ask": ya, "no_ask": na,
            "side": side, "price": price, "edge": round(edge, 4),
            **sizing,
        })
    out.sort(key=lambda x: -x["edge"])
    return out


def main():
    # Step 1: Ingest transcripts
    print("=" * 100)
    print("PHASE 1: TRANSCRIPT INGEST")
    print("=" * 100)
    for ticker, urls in TRANSCRIPT_URLS.items():
        print(f"\n  {ticker} — {len(urls)} known URLs")
        for date, label, url in urls:
            existing = _conn().execute(
                "SELECT id FROM transcripts WHERE event_type=? AND event_date=?",
                (f"{ticker.lower()}_earnings", date),
            ).fetchone()
            if existing:
                print(f"    ✓ {label} already in DB (tid={existing[0]})")
                continue
            print(f"    fetching {label}...", end=" ", flush=True)
            text = fetch_transcript(url)
            if text:
                tid = ingest_transcript(ticker, date, text)
                print(f"OK ({len(text)} chars, tid={tid})")
            else:
                print("FAIL (404 or empty)")
            time.sleep(0.3)

    # Step 2: Verify corpus depth
    print("\n" + "=" * 100)
    print("PHASE 2: CORPUS DEPTH AUDIT")
    print("=" * 100)
    for ticker in TRANSCRIPT_URLS:
        n = _conn().execute(
            "SELECT COUNT(*) FROM transcripts WHERE event_type=?",
            (f"{ticker.lower()}_earnings",),
        ).fetchone()[0]
        conf = "HIGH" if n >= 6 else ("MED" if n >= 3 else "LOW")
        print(f"  {ticker}: {n} transcripts in DB ({conf} confidence)")

    # Step 3: Score live markets
    print("\n" + "=" * 100)
    print("PHASE 3: SCORE LIVE KALSHI MARKETS")
    print("=" * 100)
    all_opps = []
    for tkr in TRANSCRIPT_URLS:
        opps = score_kalshi_event(tkr)
        for o in opps: o["co"] = tkr
        all_opps.extend(opps)

    # Sort by edge desc, filter actionable
    actionable = [o for o in all_opps if o["edge"] >= 0.05 and o["contracts"] > 0]
    actionable.sort(key=lambda x: -x["edge"])

    print(f"\n  {len(all_opps)} markets scored, {len(actionable)} actionable (edge≥5%, contracts>0)")
    print()
    print(f"  {'#':<3} {'CO':<5} {'TERM':<22} {'BASE':>5} {'CORPUS':>8} {'SIDE':<5} {'PRICE':>6} {'EDGE':>7} {'CTRS':>5} {'STAKE':>8}")
    print(f"  {'-'*3} {'-'*5} {'-'*22} {'-'*5} {'-'*8} {'-'*5} {'-'*6} {'-'*7} {'-'*5} {'-'*8}")
    for i, o in enumerate(actionable[:30], 1):
        flag = "🔥" if o["edge"] >= 0.15 else ("✓ " if o["edge"] >= 0.08 else "  ")
        kn = f"{o['k']}/{o['n']} {o['conf']}"
        print(f"  {i:<3} {flag}{o['co']:<3} {o['term'][:20]:<22} {o['base_rate']*100:>4.0f}% {kn:>8} {o['side']:<5} {o['price']:>5.2f}c {o['edge']*100:>+6.1f}% {o['contracts']:>5} ${o['stake_usd']:>6.2f}")

    # Summary stats
    n_high = sum(1 for o in actionable if o["edge"] >= 0.15)
    n_med = sum(1 for o in actionable if 0.08 <= o["edge"] < 0.15)
    n_low = sum(1 for o in actionable if 0.05 <= o["edge"] < 0.08)
    total_stake = sum(o["stake_usd"] for o in actionable)
    expected_profit = sum(o["edge"] * o["contracts"] * o["price"] for o in actionable)

    print()
    print(f"  HIGH (edge≥15%): {n_high}    MED (8-15%): {n_med}    LOW (5-8%): {n_low}")
    print(f"  TOTAL STAKE: ${total_stake:.2f}    EXPECTED GROSS PROFIT: ${expected_profit:.2f}")
    print(f"  ROI: {expected_profit/max(0.01,total_stake)*100:.1f}%   (on ${BANKROLL} paper bankroll)")

    # Step 4: Save plan
    out_path = SOV / "data" / "earnings_plan_may7_v2.json"
    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bankroll": BANKROLL,
        "actionable_trades": actionable,
        "all_scored": all_opps,
        "summary": {
            "n_high": n_high, "n_med": n_med, "n_low": n_low,
            "total_stake_usd": round(total_stake, 2),
            "expected_profit_usd": round(expected_profit, 2),
        },
    }
    with open(out_path, "w") as f:
        json.dump(plan, f, indent=2)
    print(f"\n  ✅ Plan saved: {out_path}")
    print(f"  Tomorrow morning: review prices, place orders manually OR run paper executor.")


if __name__ == "__main__":
    main()
