"""Quality gate for all transcript ingestion. Nothing enters sovereign.db without passing."""

import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from corpus.sources import EARNINGS_SOURCES
from config.settings import DB_PATH

logger = logging.getLogger("validator")

REJECTED_LOG = os.path.join(os.path.dirname(__file__), "rejected.log")


@dataclass
class ValidationResult:
    passed: bool
    word_count: int
    failure_reason: str | None
    warnings: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    speaker_changes: int = 0


def validate_transcript(
    text: str,
    source_name: str,
    ticker: str,
    quarter: str,
) -> ValidationResult:
    """
    Runs all quality gates. Returns ValidationResult with passed=True only
    if ALL hard gates clear.
    """
    warnings = []
    source_config = next(
        (s for s in EARNINGS_SOURCES if s["name"] == source_name), None
    )
    if not source_config:
        # Use generic gates
        source_config = next(s for s in EARNINGS_SOURCES if s["name"] == "generic")

    gates = source_config["quality_gates"]
    words = text.split()
    word_count = len(words)
    text_lower = text.lower()

    # HARD GATE 1: Word count
    if word_count < gates["min_word_count"]:
        return ValidationResult(
            passed=False, word_count=word_count,
            failure_reason=f"Too short: {word_count} words (min {gates['min_word_count']})",
        )
    if word_count > gates["max_word_count"]:
        return ValidationResult(
            passed=False, word_count=word_count,
            failure_reason=f"Too long: {word_count} words (max {gates['max_word_count']})",
        )

    # HARD GATE 2: Forbidden phrases
    for phrase in gates["forbidden_phrases"]:
        if phrase.lower() in text_lower:
            return ValidationResult(
                passed=False, word_count=word_count,
                failure_reason=f"Forbidden phrase: '{phrase}'",
            )

    # HARD GATE 3: Duplicate check
    dup = check_duplicate(text, ticker)
    if dup["is_duplicate"]:
        return ValidationResult(
            passed=False, word_count=word_count,
            failure_reason=f"Duplicate of transcript #{dup['existing_id']} ({dup['similarity']:.0%} similar)",
        )

    # SOFT GATE 1: Required phrases
    required = gates.get("required_phrases", [])
    found_required = sum(1 for p in required if p.lower() in text_lower)
    if required and found_required < 2:
        warnings.append(f"Only {found_required}/{len(required)} required phrases found")

    # SOFT GATE 2: Speaker changes
    speaker_pattern = re.compile(r"^[A-Z][A-Z\s]{2,30}:", re.MULTILINE)
    speaker_changes = len(speaker_pattern.findall(text))
    if speaker_changes < gates.get("min_speaker_changes", 3):
        warnings.append(f"Only {speaker_changes} speaker labels found")

    # SOFT GATE 3: Earnings content
    earnings_terms = ["revenue", "earnings", "quarter", "guidance", "margin"]
    earnings_found = sum(1 for t in earnings_terms if t in text_lower)
    if earnings_found < 2:
        warnings.append(f"Low earnings content signal ({earnings_found}/5 key terms)")

    # Quality score
    wc_score = min(1.0, word_count / 8000)
    phrase_score = found_required / max(len(required), 1)
    speaker_score = min(1.0, speaker_changes / 10)
    quality_score = round(wc_score * 0.4 + phrase_score * 0.3 + speaker_score * 0.3, 3)

    return ValidationResult(
        passed=True,
        word_count=word_count,
        failure_reason=None,
        warnings=warnings,
        quality_score=quality_score,
        speaker_changes=speaker_changes,
    )


def check_duplicate(text: str, ticker: str) -> dict:
    """Check if a near-identical transcript already exists."""
    fingerprint_words = set(text.split()[:500])
    if not fingerprint_words:
        return {"is_duplicate": False, "existing_id": None, "similarity": 0.0}

    conn = sqlite3.connect(DB_PATH)
    event_type = f"{ticker.lower()}_earnings"
    rows = conn.execute(
        "SELECT id, raw_text FROM transcripts WHERE event_type=?",
        (event_type,)
    ).fetchall()
    conn.close()

    for existing_id, existing_text in rows:
        existing_words = set(existing_text.split()[:500])
        if not existing_words:
            continue
        overlap = len(fingerprint_words & existing_words)
        union = len(fingerprint_words | existing_words)
        similarity = overlap / union if union > 0 else 0
        if similarity > 0.85:
            return {"is_duplicate": True, "existing_id": existing_id, "similarity": similarity}

    return {"is_duplicate": False, "existing_id": None, "similarity": 0.0}


def log_rejection(ticker: str, quarter: str, source: str,
                  reason: str, url: str = None) -> None:
    """Log all rejections — never silently discard."""
    from datetime import datetime
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "ticker": ticker, "quarter": quarter,
        "source": source, "url": url, "reason": reason,
    }
    os.makedirs(os.path.dirname(REJECTED_LOG), exist_ok=True)
    with open(REJECTED_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.warning(f"REJECTED {ticker} {quarter} from {source}: {reason}")
