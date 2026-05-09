"""Corpus compiler — for a specific event, assemble all relevant context.

For FOMC: pull the last N press conferences, statements, and minutes into
a unified context object. Optional: include recent speeches by Fed
governors (not just Powell). This is the substrate the base-rate model
and context scorer operate on.

Key decisions:
  - Primary corpus = prior N (default 20) transcripts of event_type
  - Secondary = last 5 statements + last 5 minutes (shorter docs for macro trend)
  - Recency window for "recent transcripts" = last 2 events of event_type
  - Everything joined by event_date for chronological context
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_log = logging.getLogger(__name__)


@dataclass
class CorpusPackage:
    """Compiled context ready for base-rate + context scoring."""
    event_id:              str
    event_type:            str
    prior_transcripts:     list[dict] = field(default_factory=list)  # primary set, sorted old→new
    recent_transcripts:    list[dict] = field(default_factory=list)  # last 2, sorted new→old
    statements:            list[dict] = field(default_factory=list)  # short fomc_statement docs
    minutes:               list[dict] = field(default_factory=list)  # fomc_minutes docs
    intermeeting_speeches: list[dict] = field(default_factory=list)  # fed_speech since last FOMC
    corpus_count:          int = 0

    def summary(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "corpus_count": self.corpus_count,
            "prior_n": len(self.prior_transcripts),
            "recent_n": len(self.recent_transcripts),
            "statement_n": len(self.statements),
            "minutes_n": len(self.minutes),
            "date_range": (
                (self.prior_transcripts[0]["event_date"], self.prior_transcripts[-1]["event_date"])
                if self.prior_transcripts else (None, None)
            ),
        }


def _fetch(db_path: str, event_type: str, limit: int,
           before_date: Optional[str] = None) -> list[dict]:
    """Fetch transcripts of event_type, newest first, optionally before date."""
    conn = sqlite3.connect(db_path)
    try:
        q = """SELECT id, source, speaker, event_type, event_date, raw_text, word_count
               FROM transcripts
               WHERE event_type = ?"""
        params: list = [event_type]
        if before_date:
            q += " AND event_date < ?"
            params.append(before_date)
        q += " ORDER BY event_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    return [
        {
            "id": r[0], "source": r[1], "speaker": r[2], "event_type": r[3],
            "event_date": r[4], "raw_text": r[5], "word_count": r[6],
        }
        for r in rows
    ]


def compile_for_event(
    event_id: str,
    event_type: str,
    event_date_iso: str,
    db_path: str,
    prior_n: int = 20,
    recent_n: int = 2,
    include_statements: bool = True,
    include_minutes: bool = True,
) -> CorpusPackage:
    """Build a CorpusPackage for the given event.

    Args:
        event_date_iso: event date as YYYY-MM-DD string. Only transcripts BEFORE
          this date are included (so we don't leak future data).

    Returns a CorpusPackage with all requested context types.
    """
    pkg = CorpusPackage(event_id=event_id, event_type=event_type)

    # Primary: prior N transcripts of event_type, newest first, excluding this date
    prior = _fetch(db_path, event_type, limit=prior_n, before_date=event_date_iso)
    pkg.prior_transcripts = sorted(prior, key=lambda t: t["event_date"])  # oldest→newest
    pkg.corpus_count = len(pkg.prior_transcripts)

    # Recent: last N (subset of prior, for context adjustment)
    pkg.recent_transcripts = prior[:recent_n]  # already newest-first

    # Optional: fomc_statement + fomc_minutes parallel streams (only for FOMC events)
    if event_type == "fomc_presser":
        if include_statements:
            pkg.statements = _fetch(db_path, "fomc_statement",
                                    limit=recent_n, before_date=event_date_iso)
        if include_minutes:
            pkg.minutes = _fetch(db_path, "fomc_minutes",
                                 limit=recent_n, before_date=event_date_iso)
        # Intermeeting Fed governor speeches (since last FOMC) — signals what's
        # on Fed officials' minds right now. Prediction alpha for Powell's presser.
        if pkg.prior_transcripts:
            last_fomc_date = pkg.prior_transcripts[-1]["event_date"]
            pkg.intermeeting_speeches = _fetch_since(
                db_path, "fed_speech",
                after_date=last_fomc_date, before_date=event_date_iso,
                limit=50,
            )

    return pkg


def _fetch_since(db_path: str, event_type: str, after_date: str,
                 before_date: str, limit: int = 50) -> list[dict]:
    """Transcripts with event_date strictly between (after_date, before_date)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, source, speaker, event_type, event_date, raw_text, word_count
               FROM transcripts
               WHERE event_type = ? AND event_date > ? AND event_date < ?
               ORDER BY event_date DESC LIMIT ?""",
            (event_type, after_date, before_date, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0], "source": r[1], "speaker": r[2], "event_type": r[3],
            "event_date": r[4], "raw_text": r[5], "word_count": r[6],
        }
        for r in rows
    ]
