"""Resolution Rule Engine — fetches and parses Kalshi market rules to extract exact trigger phrases."""

import re
import logging
import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

logger = logging.getLogger("rules")


def extract_trigger_phrase(rules_text: str) -> dict:
    """
    Parse Kalshi rules_primary to extract the exact resolution trigger.

    Example input: 'If "tariffs" is said by any PepsiCo, Inc. representative
    (including the operator of the call) during the next PepsiCo, Inc.
    earnings call (including the Q+A), then the market resolves to Yes.'

    Returns: {
        "trigger_phrase": "tariffs",
        "scope": "any representative including operator",
        "includes_qa": True,
        "exact_match": True,  # False if variants are listed
        "variants": ["tariffs"],  # all accepted forms
    }
    """
    if not rules_text:
        return {"trigger_phrase": None, "scope": "unknown", "includes_qa": False,
                "exact_match": True, "variants": []}

    rules_lower = rules_text.lower()

    # Extract the trigger phrase — try multiple patterns
    phrase_match = re.search(r'if\s+["\u201c]([^"\u201d]+)["\u201d]', rules_lower)
    if not phrase_match:
        # Unquoted: "If X is said" or "If X is stated"
        phrase_match = re.search(r'if\s+(.+?),?\s+(?:or\s+a\s+plural|is\s+(?:said|stated|mentioned))', rules_lower)
    if not phrase_match:
        # Broader fallback: "If X, ... is stated by"
        phrase_match = re.search(r'if\s+(.+?)\s+is\s+(?:said|stated|mentioned)\s+by', rules_lower)

    trigger = phrase_match.group(1).strip() if phrase_match else None

    # Check for variant phrases: "X / Y" or "X or Y"
    variants = []
    if trigger:
        # Split on " / " for slash-separated variants
        if " / " in trigger:
            variants = [v.strip() for v in trigger.split(" / ")]
        else:
            variants = [trigger]

    # Scope detection
    includes_operator = "operator" in rules_lower
    includes_qa = "q+a" in rules_lower or "q&a" in rules_lower or "question" in rules_lower
    scope = "any representative"
    if includes_operator:
        scope += " including operator"
    if includes_qa:
        scope += " + Q&A"

    # Detect rolling window vs single event
    is_rolling = bool(re.search(r'before\s+\w+\s+\d{1,2},?\s+\d{4}', rules_lower))
    if not is_rolling:
        is_rolling = bool(re.search(r'before\s+\w{3}\s+\d{1,2},?\s+\d{4}', rules_lower))

    return {
        "trigger_phrase": trigger,
        "scope": scope,
        "includes_qa": includes_qa,
        "exact_match": len(variants) <= 1,
        "variants": variants,
        "is_rolling_window": is_rolling,
    }


def fetch_and_cache_rules(client, ticker: str) -> dict:
    """Fetch market rules from Kalshi and cache them."""
    try:
        raw = client._get(f"/markets/{ticker}")
        m = raw.get("market", raw)
        rules_text = m.get("rules_primary", "")
        parsed = extract_trigger_phrase(rules_text)
        parsed["raw_rules"] = rules_text
        parsed["ticker"] = ticker
        return parsed
    except Exception as e:
        logger.debug(f"Could not fetch rules for {ticker}: {e}")
        return {"trigger_phrase": None, "ticker": ticker, "variants": []}


def get_search_terms_from_rules(rules: dict) -> list[str]:
    """
    Given parsed rules, return the list of terms to search for in transcripts.
    This is what we should match against — not a generic term name.
    """
    if rules.get("variants"):
        return rules["variants"]
    if rules.get("trigger_phrase"):
        return [rules["trigger_phrase"]]
    return []
