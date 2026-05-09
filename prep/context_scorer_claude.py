"""Claude-powered context scorer — real alpha uplift over heuristic.

Given a term + base_rate + recent FOMC transcripts, ask Claude to produce
a logit delta (-2.0 to +2.0) capturing contextual pressure for THIS specific
press conference. Falls back to heuristic when Claude unavailable.

Key design:
  - One API call per term (batchable if needed later)
  - Output parsed as strict JSON {"delta": float, "reasoning": str, "confidence": str}
  - Caches by (term_key, date) to avoid re-hitting Claude on reruns
  - Graceful degrade: if anthropic SDK missing or API errors → heuristic
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

CACHE_PATH_DEFAULT = "/root/sovereign/data/context_cache.db"


def _cache_init(path: str):
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS context_cache (
            key        TEXT PRIMARY KEY,
            delta      REAL NOT NULL,
            reasoning  TEXT,
            confidence TEXT,
            source     TEXT,
            cached_at  TEXT NOT NULL
        );
        """)
        conn.commit()
    finally:
        conn.close()


def _cache_get(path: str, key: str, max_age_hours: int = 48) -> Optional[tuple[float, str]]:
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT delta, reasoning, cached_at FROM context_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if not row:
                return None
            delta, reasoning, cached_at = row
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(cached_at)).total_seconds() / 3600
            if age_h > max_age_hours:
                return None
            return (float(delta), reasoning or "")
        finally:
            conn.close()
    except Exception:
        return None


def _cache_put(path: str, key: str, delta: float, reasoning: str,
               confidence: str, source: str):
    _cache_init(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO context_cache
               (key, delta, reasoning, confidence, source, cached_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 delta = excluded.delta,
                 reasoning = excluded.reasoning,
                 confidence = excluded.confidence,
                 source = excluded.source,
                 cached_at = excluded.cached_at""",
            (key, delta, reasoning, confidence, source,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _heuristic_fallback(
    word: str, base_rate: float, recent_transcripts: list[dict],
    news_headlines: Optional[list[str]] = None,
    intermeeting_speeches: Optional[list[dict]] = None,
) -> tuple[float, str]:
    """Heuristic scorer with news + intermeeting-speech awareness.

    Signals stacked (each layered for total delta):
      - transcript momentum — word in last N FOMC pressers
      - news heat — word in last 72h news headlines
      - intermeeting-speech heat — word in recent Fed governor speeches
        (STRONGEST predictor — governors coordinate messaging before pressers)
    """
    wl = word.lower()

    # 1. Transcript momentum (historical pattern)
    transcript_hits = 0
    if recent_transcripts:
        transcript_hits = sum(
            1 if re.search(rf"\b{re.escape(wl)}\b",
                           (t.get("raw_text", "") or "").lower()) else 0
            for t in recent_transcripts
        )

    # 2. News-headline match (real-time public discourse)
    news_hits = 0
    if news_headlines:
        news_hits = sum(
            1 for h in news_headlines
            if re.search(rf"\b{re.escape(wl)}\b", h.lower())
        )
        if " " in wl:
            parts = wl.split()
            if len(parts) == 2:
                news_hits += sum(
                    1 for h in news_headlines
                    if all(p in h.lower() for p in parts)
                )

    # 3. Intermeeting speech heat (Fed insider signaling)
    speech_hits = 0
    speech_title_hits = 0  # extra-strong signal if in speech TITLE
    if intermeeting_speeches:
        for s in intermeeting_speeches:
            text = (s.get("raw_text", "") or "").lower()
            if re.search(rf"\b{re.escape(wl)}\b", text):
                speech_hits += 1
                # Check if also in speech title (strongest signal)
                # Title is typically stored at start of raw_text; also check source
                # by looking at first 100 chars for theme
                if re.search(rf"\b{re.escape(wl)}\b", text[:200]):
                    speech_title_hits += 1

    # 4. Combined adjustment
    delta = 0.0
    pieces = []

    if transcript_hits == len(recent_transcripts or []) and transcript_hits >= 2:
        delta += 0.4
        pieces.append(f"transcripts:{transcript_hits}")
    elif transcript_hits == 1:
        delta += 0.2
        pieces.append("recent-transcript")

    if news_hits >= 3:
        delta += 0.6
        pieces.append(f"news-heat:{news_hits}")
    elif news_hits >= 1:
        delta += 0.3
        pieces.append(f"news-hit:{news_hits}")

    # Intermeeting speech signals — STRONG predictors
    if speech_title_hits >= 1:
        delta += 0.8  # biggest lift — word in a recent Fed speech title
        pieces.append(f"speech-title:{speech_title_hits}")
    elif speech_hits >= 3:
        delta += 0.6
        pieces.append(f"speech-heat:{speech_hits}")
    elif speech_hits >= 1:
        delta += 0.3
        pieces.append(f"speech-hit:{speech_hits}")

    # Rare + cold → slight negative
    if (base_rate < 0.10 and transcript_hits == 0
            and news_hits == 0 and speech_hits == 0):
        delta -= 0.1
        pieces.append("rare+cold")

    delta = max(-1.5, min(1.8, delta))   # headroom for strong signals

    if not pieces:
        return (0.0, f"heuristic: neutral (base={base_rate:.0%})")
    return (delta, f"heuristic: {' | '.join(pieces)}")


def _build_cache_key(word: str, base_rate: float, recent_ids: list) -> str:
    h = hashlib.sha256()
    h.update(word.lower().encode())
    h.update(f"{base_rate:.3f}".encode())
    h.update(",".join(str(i) for i in sorted(recent_ids)).encode())
    return h.hexdigest()[:32]


def score_with_claude(
    word: str,
    base_rate: float,
    recent_transcripts: list[dict],
    news_headlines: Optional[list[str]] = None,
    intermeeting_speeches: Optional[list[dict]] = None,
    cache_path: str = CACHE_PATH_DEFAULT,
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
) -> tuple[float, str]:
    """Main entry: returns (delta_logit, reasoning).

    Caches by (word, base_rate, recent_transcript_ids). Falls back to
    heuristic if anthropic SDK missing or API call fails.
    """
    recent_ids = [t.get("id", 0) for t in recent_transcripts]
    cache_key = _build_cache_key(word, base_rate, recent_ids)
    cached = _cache_get(cache_path, cache_key)
    if cached:
        return cached

    # Load .env from common locations (idempotent)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        for p in ("/root/sovereign/.env", "/root/lip-maker/.env",
                  "/opt/micro-arb-engine/.env"):
            try:
                for line in open(p):
                    if line.startswith("ANTHROPIC_API_KEY=") and "=" in line:
                        val = line.split("=", 1)[1].strip().strip("'").strip('"')
                        if val:
                            os.environ["ANTHROPIC_API_KEY"] = val
                            break
            except FileNotFoundError:
                continue

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        delta, reasoning = _heuristic_fallback(
            word, base_rate, recent_transcripts,
            news_headlines=news_headlines,
            intermeeting_speeches=intermeeting_speeches,
        )
        _cache_put(cache_path, cache_key, delta, reasoning, "none", "heuristic_no_key")
        return (delta, reasoning)

    try:
        from anthropic import Anthropic
    except ImportError:
        _log.warning("anthropic SDK not installed; using heuristic")
        delta, reasoning = _heuristic_fallback(
            word, base_rate, recent_transcripts,
            news_headlines=news_headlines,
            intermeeting_speeches=intermeeting_speeches,
        )
        _cache_put(cache_path, cache_key, delta, reasoning, "none", "no_sdk")
        return (delta, reasoning)

    # Build compact prompt
    excerpts = []
    for t in recent_transcripts[:2]:
        text = (t.get("raw_text", "") or "")[:800]
        date = t.get("event_date", "unknown")
        excerpts.append(f"[{date}]: {text}...")

    headlines_blob = "\n".join(news_headlines or [])[:1500] or "(no headlines provided)"

    prompt = f"""You are a quantitative analyst for prediction market trading. Your output MUST be valid JSON, no prose.

TASK: Estimate how much to adjust the probability that Jerome Powell mentions "{word}" at the next FOMC press conference.

INPUTS:
- Historical base rate: {base_rate:.1%} (fraction of prior pressers with this word)
- Last {len(recent_transcripts[:2])} press conference excerpts (most recent first):
{chr(10).join(excerpts)}
- Current macro headlines (last 72h):
{headlines_blob}

OUTPUT: JSON with these keys:
  "delta": float in [-2.0, +2.0] (logit adjustment — positive = more likely, negative = less likely)
  "reasoning": string ≤100 chars explaining the adjustment
  "confidence": "low" | "medium" | "high"

Rules:
- Base rate + delta = final logit. delta=0.7 roughly converts 50% → 67%.
- Larger |delta| requires stronger evidence.
- If topic is in current news AND recent transcripts mentioned it → larger positive delta.
- If historically rare AND not in recent news → small negative delta (-0.1 to -0.3).
- Novel words (e.g., "Kalshi", "Renovation") where Powell has no reason to use them → delta < 0.

JSON OUTPUT:"""

    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else ""
        # Extract JSON (Claude may wrap in markdown or prose despite instructions)
        json_match = re.search(r"\{[^}]*\"delta\"[^}]*\}", text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
        else:
            data = json.loads(text)
        delta = float(data.get("delta", 0.0))
        delta = max(-2.0, min(2.0, delta))
        reasoning = str(data.get("reasoning", "claude response"))[:200]
        confidence = data.get("confidence", "medium")
        _cache_put(cache_path, cache_key, delta, reasoning, confidence, "claude")
        return (delta, reasoning)
    except Exception as e:
        _log.warning(f"Claude call failed for '{word}': {e}; using heuristic")
        delta, reasoning = _heuristic_fallback(
            word, base_rate, recent_transcripts,
            news_headlines=news_headlines,
            intermeeting_speeches=intermeeting_speeches,
        )
        _cache_put(cache_path, cache_key, delta, reasoning, "low", f"claude_fail_{type(e).__name__}")
        return (delta, reasoning)


def make_scorer_fn(
    news_headlines: Optional[list[str]] = None,
    intermeeting_speeches: Optional[list[dict]] = None,
    cache_path: str = CACHE_PATH_DEFAULT,
    model: str = "claude-sonnet-4-6",
):
    """Factory: returns a scorer function compatible with thesis_builder's
    context_scorer_fn signature: (word, base_rate, recent_transcripts) → (delta, reasoning).

    Closure captures news_headlines + intermeeting_speeches so they feed into
    every term's scoring without threading through thesis_builder."""
    def _scorer(word, base_rate, recent):
        return score_with_claude(
            word, base_rate, recent,
            news_headlines=news_headlines,
            intermeeting_speeches=intermeeting_speeches,
            cache_path=cache_path, model=model,
        )
    return _scorer
