"""Kalshi API client — polls mention markets, snapshots prices, classifies markets."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import argparse
from datetime import datetime, timezone
from base64 import b64encode

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH, KALSHI_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Priority targets from Session 1 analysis
PRIME_FOMC_TARGETS = [
    "disinflation",    # 25.7% base rate — most context-sensitive FOMC term
    "soft landing",    # 21.6% base rate — narrative-driven, flips with news
    "stagflation",     # 6.8%  base rate — market chronically overprices this
    "restrictive",     # parlay leg: restrictive+disinflation = +11.5pp pair edge
    "tariffs",         # parlay leg: tariffs+trade = +8.6pp pair edge
    "transitory",      # historically volatile — monitor for Powell-era comeback
]

# Skip list: base rate >90%, no edge
SKIP_TERMS = ["unemployment", "inflation", "labor market"]


def _get_conn():
    return sqlite3.connect(DB_PATH)


class KalshiClient:
    """Kalshi trading API wrapper for mention markets."""

    def __init__(self):
        self.base_url = KALSHI_BASE_URL
        self.api_key = os.getenv("KALSHI_API_KEY")
        self.private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        self._session = requests.Session()
        self._private_key = None

        if self.api_key and self.api_key != "your_kalshi_api_key_here":
            self._session.headers.update({"Content-Type": "application/json"})
            self.authenticated = True
            # Load private key once
            if self.private_key_path and os.path.exists(self.private_key_path):
                try:
                    from cryptography.hazmat.primitives import serialization
                    with open(self.private_key_path, "rb") as f:
                        self._private_key = serialization.load_pem_private_key(f.read(), password=None)
                except Exception as e:
                    print(f"  Warning: Could not load private key: {e}")
        else:
            self.authenticated = False

    def _sign_request(self, method: str, path: str) -> dict:
        """Sign request with RSA-PSS per Kalshi v2 spec.
        - Timestamp in milliseconds
        - Message: {timestamp_ms}{METHOD}{path} (no body, no query params)
        - RSA-PSS with SHA256, MGF1, salt_length=DIGEST_LENGTH
        """
        timestamp_ms = str(int(time.time() * 1000))
        # Strip query params from path for signing
        sign_path = path.split("?")[0]
        msg = f"{timestamp_ms}{method}{sign_path}"

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

        if self._private_key:
            try:
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import padding

                signature = self._private_key.sign(
                    msg.encode("utf-8"),
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
                headers["KALSHI-ACCESS-SIGNATURE"] = b64encode(signature).decode("utf-8")
            except Exception as e:
                print(f"  Warning: RSA signing failed: {e}")

        return headers

    def _get(self, path: str, params: dict = None) -> dict:
        """Make authenticated GET request."""
        url = f"{self.base_url}{path}"
        # Sign the full path including /trade-api/v2 prefix
        full_path = f"/trade-api/v2{path}" if not path.startswith("/trade-api") else path
        headers = self._sign_request("GET", full_path)
        resp = self._session.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_mention_markets(self) -> list[dict]:
        """
        Scans all open Kalshi events for mention/say markets.
        Returns normalized list with: ticker, title, yes_price, no_price, volume, close_time, rules, event_title.
        """
        if not self.authenticated:
            print("  [NO API KEY] Returning demo data. Set KALSHI_API_KEY in .env for live data.")
            return _get_demo_mention_markets()

        try:
            mention_markets = []
            cursor = None
            pages = 0

            while pages < 30:
                params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
                if cursor:
                    params["cursor"] = cursor

                data = self._get("/events", params=params)
                events = data.get("events", [])

                for event in events:
                    event_title = (event.get("title", "") or "").lower()
                    event_ticker = (event.get("event_ticker", "") or "").lower()

                    # Filter for mention/say events
                    if not any(kw in event_title for kw in ["mention", " say ", "will say", "said"]) \
                       and "mention" not in event_ticker:
                        continue

                    for m in event.get("markets", []):
                        # Normalize price: use ask/bid midpoint, or ask, or bid
                        yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
                        yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
                        if yes_ask > 0 and yes_bid > 0:
                            yes_price = (yes_ask + yes_bid) / 2
                        elif yes_ask > 0:
                            yes_price = yes_ask
                        elif yes_bid > 0:
                            yes_price = yes_bid
                        else:
                            last = float(m.get("last_price_dollars", 0) or 0)
                            yes_price = last if last > 0 else 0.5

                        no_price = 1.0 - yes_price

                        mention_markets.append({
                            "ticker": m.get("ticker", ""),
                            "title": m.get("title", ""),
                            "yes_price": yes_price,
                            "no_price": no_price,
                            "yes_ask": yes_ask,
                            "yes_bid": yes_bid,
                            "volume": float(m.get("volume_fp", 0) or 0),
                            "close_time": m.get("close_time", ""),
                            "event_title": event.get("title", ""),
                            "event_ticker": event.get("event_ticker", ""),
                            "rules": m.get("rules_primary", ""),
                            "subtitle": m.get("yes_sub_title", ""),
                        })

                cursor = data.get("cursor", "")
                if not cursor or not events:
                    break
                pages += 1

            return mention_markets

        except requests.exceptions.HTTPError as e:
            print(f"  API error: {e}")
            if "401" in str(e) or "403" in str(e):
                print("  Authentication failed. Check KALSHI_API_KEY and private key.")
            return _get_demo_mention_markets()
        except Exception as e:
            print(f"  Connection error: {e}")
            return _get_demo_mention_markets()

    def get_market_detail(self, ticker: str) -> dict:
        """
        GET /markets/{ticker}
        Returns full detail including resolution rules text.
        """
        if not self.authenticated:
            return {"ticker": ticker, "rules": "Demo mode — no resolution rules available"}

        try:
            data = self._get(f"/markets/{ticker}")
            market = data.get("market", data)
            return {
                "ticker": market.get("ticker", ticker),
                "title": market.get("title", ""),
                "rules": market.get("rules_primary", ""),
                "yes_price": market.get("yes_price", 0) / 100.0 if market.get("yes_price") else 0.5,
                "no_price": market.get("no_price", 0) / 100.0 if market.get("no_price") else 0.5,
                "volume": market.get("volume", 0),
                "close_time": market.get("close_time", ""),
                "status": market.get("status", ""),
                "result": market.get("result", ""),
            }
        except Exception as e:
            print(f"  Error fetching {ticker}: {e}")
            return {"ticker": ticker, "error": str(e)}

    def get_orderbook(self, ticker: str) -> dict:
        """
        GET /markets/{ticker}/orderbook
        Returns bids/asks for limit price selection.
        """
        if not self.authenticated:
            return {"ticker": ticker, "bids": [], "asks": []}

        try:
            data = self._get(f"/markets/{ticker}/orderbook")
            return {
                "ticker": ticker,
                "bids": data.get("orderbook", {}).get("yes", []),
                "asks": data.get("orderbook", {}).get("no", []),
            }
        except Exception as e:
            print(f"  Error fetching orderbook for {ticker}: {e}")
            return {"ticker": ticker, "bids": [], "asks": [], "error": str(e)}

    def snapshot_market(self, market: dict) -> None:
        """Insert current market state into market_snapshots table."""
        conn = _get_conn()
        classified = classify_market(market)
        conn.execute(
            """INSERT INTO market_snapshots
               (kalshi_market_id, ticker, term, event_type, event_date, yes_price, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                market.get("ticker", ""),
                market.get("ticker", ""),
                classified.get("term", "unknown"),
                classified.get("event_type", "other"),
                classified.get("event_date"),
                market.get("yes_price", 0.5),
                market.get("volume", 0),
            )
        )
        conn.commit()
        conn.close()

    def poll_all_mention_markets(self) -> list[dict]:
        """
        Fetches all open mention markets, snapshots each.
        Prints summary table.
        """
        markets = self.get_mention_markets()
        for m in markets:
            self.snapshot_market(m)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\nSnapped {len(markets)} mention markets — {now}")
        return markets


def classify_market(market: dict) -> dict:
    """
    Takes raw Kalshi market dict.
    Returns classified info: event_type, term, speaker, event_date.
    Uses both the market title and the event_title/event_ticker fields.
    """
    title = (market.get("title", "") or "").lower()
    ticker = (market.get("ticker", "") or "").upper()
    event_title = (market.get("event_title", "") or "").lower()
    event_ticker = (market.get("event_ticker", "") or "").upper()
    subtitle = (market.get("subtitle", "") or "").lower()
    full_text = f"{title} {event_title} {subtitle}"

    result = {
        "event_type": "other",
        "term": None,
        "speaker": None,
        "event_date": None,
    }

    # Classify event type from event_ticker or title
    if any(kw in event_ticker for kw in ["FOMC", "FEDDECISION"]):
        result["event_type"] = "fomc_presser"
        result["speaker"] = "powell"
    elif ("EARNINGSMENTION" in event_ticker or "EARNINGMENTION" in event_ticker
          or "EARNINGSMENTION" in ticker or "EARNINGMENTION" in ticker
          or "MENTIONEARN" in event_ticker or "MENTIONEARN" in ticker):
        # Earnings mention: extract company from event ticker.
        # Supports BOTH ticker formats:
        #   KXEARNINGSMENTIONTSLA-26APR22  (older)
        #   KXMENTIONEARNFDX-26MAR19       (newer — FedEx, etc.)
        company_match = (
            re.search(r"EARNINGS?MENTION(\w+?)-", event_ticker)
            or re.search(r"MENTIONEARN(\w+?)-", event_ticker)
        )
        if company_match:
            company = company_match.group(1)
            # Use the canonical TICKER_SPEAKER_MAP for all lookups
            from corpus.sources import TICKER_SPEAKER_MAP, get_event_type, get_speaker
            speaker = TICKER_SPEAKER_MAP.get(company)
            result["event_type"] = get_event_type(company)
            result["speaker"] = speaker
        else:
            result["event_type"] = "earnings_mention"
    elif any(kw in full_text for kw in [
                "fomc", "powell", "federal reserve", "the fed ", " fed ",
                "fed chair", "fed meeting", "fed decision",
            ]):
        # Deliberately NOT matching bare "fed" — it collides with
        # "FedEx" (the earnings-mention ticker KXMENTIONEARNFDX).
        result["event_type"] = "fomc_presser"
        result["speaker"] = "powell"
    elif any(kw in full_text for kw in ["tesla", "tsla"]):
        result["event_type"] = "tsla_earnings"
        result["speaker"] = "musk"
    elif "TRUMPMENTION" in event_ticker or "TRUMPFIRE" in event_ticker:
        result["event_type"] = "trump_speech"
        result["speaker"] = "trump"
    elif any(kw in full_text for kw in ["trump", "president trump", "potus"]):
        result["event_type"] = "trump_speech"
        result["speaker"] = "trump"
    elif "RFKMENTION" in event_ticker:
        result["event_type"] = "rfk_speech"
        result["speaker"] = "rfk"
    elif "POLITICSMENTION" in event_ticker or "LUTNICK" in event_ticker:
        result["event_type"] = "political_speech"
        result["speaker"] = None
    elif "CANDACE" in event_ticker:
        result["event_type"] = "candace_livestream"
        result["speaker"] = "candace"
    elif any(kw in full_text for kw in ["nfl", "football", "broadcast", "announcer"]):
        result["event_type"] = "nfl_broadcast"
        result["speaker"] = "announcer"

    # Extract term from ticker suffix (e.g., -TARI for tariffs, -INFL for inflation)
    # The ticker suffix is the term abbreviation
    ticker_suffix_map = {
        "TARI": "tariffs", "INFL": "inflation", "RECE": "recession",
        "AI": "AI", "TRUM": "trump", "CYBR": "cybertruck",
        "STAR": "starlink", "NEWA": "new aircraft", "DELV": "deliveries",
        "DIVI": "dividend", "CRED": "credit", "HEAD": "headwinds",
        "REAL": "real estate", "OIL": "oil", "CHIN": "china",
        "VOLU": "volume", "ACQU": "acquisition", "ARTI": "artificial intelligence",
        "PRIC": "price", "SUBS": "subscribers", "AD": "advertising",
        "COMP": "competition", "MLB": "MLB", "SNAP": "snapshot",
        "CATA": "catastrophe", "DEPA": "department", "APPL": "apple",
        "NVID": "nvidia", "ARIZ": "arizona", "IRAN": "iran",
        "LAUN": "launch", "TOOT": "toothpaste", "MA": "M&A",
        "ON": "on track", "ATC": "ATC", "RECO": "record",
    }

    # Try to extract term from ticker suffix
    ticker_parts = ticker.split("-")
    if len(ticker_parts) >= 3:
        suffix = ticker_parts[-1]
        if suffix in ticker_suffix_map:
            result["term"] = ticker_suffix_map[suffix]

    # Fallback: check known terms in title
    if not result["term"]:
        from config.settings import FOMC_TERMS
        all_terms = FOMC_TERMS + PRIME_FOMC_TARGETS
        for term in sorted(all_terms, key=len, reverse=True):
            if term.lower() in full_text:
                result["term"] = term
                break

    # Fallback: extract from subtitle or quotes in title
    if not result["term"]:
        if subtitle:
            result["term"] = subtitle[:50]
        else:
            quote_match = re.search(r'["\u201c]([^"\u201d]+)["\u201d]', title)
            if quote_match:
                result["term"] = quote_match.group(1).lower()

    # Extract date from event_ticker or title
    # Event ticker format: KXEARNINGSMENTIONTSLA-26APR22 → 2026-04-22
    date_match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", ticker)
    if date_match:
        year = f"20{date_match.group(1)}"
        month_str = date_match.group(2)
        day = date_match.group(3)
        months = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
                  "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
        month = months.get(month_str, "01")
        result["event_date"] = f"{year}-{month}-{day}"

    # Fallback: try dates in text
    if not result["event_date"]:
        for pattern in [r'(\d{4}-\d{2}-\d{2})', r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+\d{4}']:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                try:
                    date_str = match.group(0)
                    for fmt in ["%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]:
                        try:
                            result["event_date"] = datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass
                if result["event_date"]:
                    break

    return result


def _get_demo_mention_markets() -> list[dict]:
    """
    Demo data based on typical Kalshi FOMC mention markets.
    Used when no API key is configured. Prices approximate real market levels.
    """
    # Next FOMC: 2026-05-07
    event_date = "2026-05-07"
    return [
        {"ticker": "KXFOMCMENTION-INFLATION-26MAY07", "title": f'Will Powell say "inflation" at FOMC press conference on May 7?',
         "yes_price": 0.94, "no_price": 0.06, "volume": 2450, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'inflation' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-DISINFLATION-26MAY07", "title": f'Will Powell say "disinflation" at FOMC press conference on May 7?',
         "yes_price": 0.32, "no_price": 0.68, "volume": 890, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'disinflation' appears in official Fed transcript. 'Disinflationary' does NOT count."},

        {"ticker": "KXFOMCMENTION-SOFTLANDING-26MAY07", "title": f'Will Powell say "soft landing" at FOMC press conference on May 7?',
         "yes_price": 0.28, "no_price": 0.72, "volume": 620, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'soft landing' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-STAGFLATION-26MAY07", "title": f'Will Powell say "stagflation" at FOMC press conference on May 7?',
         "yes_price": 0.18, "no_price": 0.82, "volume": 1100, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'stagflation' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-RECESSION-26MAY07", "title": f'Will Powell say "recession" at FOMC press conference on May 7?',
         "yes_price": 0.55, "no_price": 0.45, "volume": 1800, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'recession' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-RESTRICTIVE-26MAY07", "title": f'Will Powell say "restrictive" at FOMC press conference on May 7?',
         "yes_price": 0.42, "no_price": 0.58, "volume": 530, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'restrictive' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-TARIFFS-26MAY07", "title": f'Will Powell say "tariffs" at FOMC press conference on May 7?',
         "yes_price": 0.38, "no_price": 0.62, "volume": 950, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'tariffs' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-LABORMARKET-26MAY07", "title": f'Will Powell say "labor market" at FOMC press conference on May 7?',
         "yes_price": 0.93, "no_price": 0.07, "volume": 1900, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'labor market' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-UNCERTAINTY-26MAY07", "title": f'Will Powell say "uncertainty" at FOMC press conference on May 7?',
         "yes_price": 0.78, "no_price": 0.22, "volume": 1500, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'uncertainty' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-TRANSITORY-26MAY07", "title": f'Will Powell say "transitory" at FOMC press conference on May 7?',
         "yes_price": 0.12, "no_price": 0.88, "volume": 780, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'transitory' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-DATADEPENDENT-26MAY07", "title": f'Will Powell say "data dependent" at FOMC press conference on May 7?',
         "yes_price": 0.25, "no_price": 0.75, "volume": 680, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'data dependent' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-RATECUT-26MAY07", "title": f'Will Powell say "rate cut" at FOMC press conference on May 7?',
         "yes_price": 0.40, "no_price": 0.60, "volume": 1200, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'rate cut' appears in official Fed transcript. 'Cut rates' also counts."},

        {"ticker": "KXFOMCMENTION-PIVOT-26MAY07", "title": f'Will Powell say "pivot" at FOMC press conference on May 7?',
         "yes_price": 0.08, "no_price": 0.92, "volume": 420, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'pivot' appears in official Fed transcript."},

        {"ticker": "KXFOMCMENTION-HOUSING-26MAY07", "title": f'Will Powell say "housing" at FOMC press conference on May 7?',
         "yes_price": 0.70, "no_price": 0.30, "volume": 870, "close_time": f"{event_date}T20:00:00Z",
         "subtitle": f"FOMC {event_date}", "rules": "Resolves YES if 'housing' appears in official Fed transcript."},
    ]


def print_market_table(markets: list[dict]) -> None:
    """Print a formatted table of mention markets."""
    if not markets:
        print("No mention markets found.")
        return

    print(f"\n{'Ticker':<45} | {'Term':<20} | {'YES':>5} | {'NO':>5} | {'Vol':>6} | {'Type':<15}")
    print("─" * 110)
    for m in markets:
        classified = classify_market(m)
        term = classified.get("term", "?")
        etype = classified.get("event_type", "other")
        yes_p = m.get("yes_price", 0)
        no_p = m.get("no_price", 0)
        vol = m.get("volume", 0)
        ticker = m.get("ticker", "?")
        print(f"{ticker:<45} | {term:<20} | {yes_p*100:>4.0f}c | {no_p*100:>4.0f}c | {vol:>6} | {etype:<15}")
    print("─" * 110)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Kalshi Scanner")
    parser.add_argument("--poll", action="store_true", help="Poll all open mention markets")
    parser.add_argument("--detail", type=str, help="Get detail for a specific ticker")
    args = parser.parse_args()

    client = KalshiClient()

    if args.poll:
        markets = client.poll_all_mention_markets()
        print_market_table(markets)
    elif args.detail:
        detail = client.get_market_detail(args.detail)
        print(json.dumps(detail, indent=2))
    else:
        # Default: poll
        markets = client.poll_all_mention_markets()
        print_market_table(markets)
