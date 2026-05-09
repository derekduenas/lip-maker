"""Context Scorer — Claude API adjusts base rate using current news + recent transcripts."""

import json
import math
import os
import sqlite3
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DB_PATH

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _get_anthropic_client():
    """Return Anthropic client or None if no key."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your_anthropic_api_key_here":
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        print(f"  Warning: Could not initialize Anthropic client: {e}")
        return None


def get_recent_transcripts(event_type: str, term: str, limit: int = 2) -> list[dict]:
    """Fetch the last `limit` transcripts where this term was mentioned."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT t.event_date, t.raw_text, m.context_snippet
           FROM transcripts t
           JOIN mentions m ON m.transcript_id = t.id
           WHERE t.event_type = ? AND m.term = ? AND m.mentioned = 1
           ORDER BY t.event_date DESC
           LIMIT ?""",
        (event_type, term, limit)
    ).fetchall()
    conn.close()

    results = []
    for date, raw_text, snippets in rows:
        # Use context snippets if available, otherwise first 500 chars of raw text
        excerpt = snippets if snippets else raw_text[:500]
        results.append({"date": date, "excerpt": excerpt})
    return results


def get_frequency_stats(event_type: str, term: str) -> dict:
    """Get k (occurrences) and n (total events) for the prompt."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT occurrences, total_events FROM frequency_matrix WHERE event_type=? AND term=?",
        (event_type, term)
    ).fetchone()
    conn.close()
    if row:
        return {"k": row[0], "n": row[1]}
    return {"k": 0, "n": 0}


def _build_prompt(
    term: str,
    event_type: str,
    event_date: str,
    base_rate: float,
    k: int,
    n: int,
    recent_transcripts: list[dict],
    news_headlines: list[str],
) -> tuple[str, str]:
    """Build system + user prompts per CLAUDE.md spec."""
    system = (
        "You are a quantitative analyst specializing in prediction market probability estimation.\n"
        "You output structured JSON only. No prose."
    )

    # Format recent transcript excerpts
    if recent_transcripts:
        transcript_parts = []
        for t in recent_transcripts:
            transcript_parts.append(f"[{t['date']}]: {t['excerpt'][:600]}")
        recent_text = "\n\n".join(transcript_parts)
    else:
        recent_text = "No recent transcripts available for this term."

    # Format headlines
    if news_headlines:
        headlines_text = "\n".join(f"- {h}" for h in news_headlines[:15])
    else:
        headlines_text = "No recent headlines available."

    event_label = event_type.replace("_", " ")

    user = f"""You are estimating the probability that the term "{term}" will appear in the official
transcript of an upcoming {event_label} on {event_date}.

Historical base rate: {base_rate:.1%} (appeared in {k} of {n} past events of this type)

## Recent transcript excerpts (last 2 events, for context on recent language patterns):

{recent_text}

## Current news context (last 72 hours):

{headlines_text}

Based on whether current conditions make this term MORE or LESS likely than the
historical base rate, output a JSON object:
{{
  "context_delta": <float between -2.0 and +2.0>,
  "reasoning": "<1-2 sentences max explaining the delta direction>",
  "confidence": "<low|medium|high>"
}}

Rules:
- context_delta > 0 means MORE likely than base rate (current context pushes toward mention)
- context_delta < 0 means LESS likely than base rate
- Use 0.0 if current context is neutral relative to historical pattern
- Do NOT factor in the base rate itself — only the directional adjustment from context

Output JSON only. No markdown. No explanation outside the JSON object."""

    return system, user


def _apply_context_delta(base_rate: float, context_delta: float) -> float:
    """Apply context delta to base rate using logit transform."""
    # Clamp base rate to avoid log(0) or log(inf)
    br = max(0.001, min(0.999, base_rate))
    logit = math.log(br / (1 - br))
    adjusted_logit = logit + context_delta
    return 1 / (1 + math.exp(-adjusted_logit))


def _heuristic_context_delta(term: str, base_rate: float, news_headlines: list[str]) -> dict:
    """
    Fallback heuristic when Claude API is unavailable.
    Uses simple keyword matching on headlines to estimate direction.
    """
    if not news_headlines:
        return {
            "context_delta": 0.0,
            "reasoning": "No news context available — using base rate as-is (heuristic fallback).",
            "confidence": "low",
        }

    headlines_text = " ".join(news_headlines).lower()
    term_lower = term.lower()

    # Count direct mentions of the term in headlines
    term_mentions = headlines_text.count(term_lower)

    # Contextual keyword boosts. When a term appears in recent headlines or
    # is semantically implied by news cycle language, nudge base_rate up.
    # This fires only when Claude API is unavailable — it's a safety net.
    positive_keywords = {
        # ── FOMC / macro Fed-speak ──────────────────────────────────────
        "disinflation":        ["prices falling", "cooling", "cpi down", "deflation", "easing"],
        "soft landing":        ["goldilocks", "soft landing", "no recession", "resilient growth"],
        "stagflation":         ["stagflation", "slowdown inflation", "growth slowing prices rising"],
        "recession":           ["recession", "contraction", "gdp negative", "downturn", "layoffs surge"],
        "transitory":          ["temporary", "transitory", "one-time", "supply-side"],
        "restrictive":         ["tight policy", "restrictive", "high rates", "hawkish"],
        "rate cut":            ["rate cut", "easing", "dovish", "lower rates", "cut rates"],
        "rate hike":           ["rate hike", "raising rates", "tightening", "higher rates"],
        "pause":               ["fed pause", "on hold", "hold rates", "skip hike"],
        "data dependent":      ["data dependent", "data-dependent", "wait and see"],
        "pivot":               ["pivot", "policy shift", "reversal", "changing course"],
        "inflation":           ["cpi", "ppi", "price pressure", "inflation", "core inflation"],
        "unemployment":        ["unemployment", "jobs report", "nonfarm payrolls", "layoffs", "jobless claims"],
        "labor market":        ["labor market", "jobs market", "hiring", "wage growth", "wages rising"],
        "neutral rate":        ["neutral rate", "r-star", "long-run rate"],
        "balance sheet":       ["balance sheet", "qt", "quantitative tightening", "bond runoff", "reserves"],
        "quantitative tightening": ["quantitative tightening", "qt", "bond runoff", "balance sheet reduction"],
        "qt":                  ["quantitative tightening", "qt ", "bond runoff"],
        "uncertainty":         ["uncertainty", "uncertain outlook", "mixed signals", "unclear"],
        "confident":           ["confident", "optimistic", "constructive"],
        "gradual":             ["gradual", "measured", "patient", "incremental"],
        "housing":             ["housing", "mortgage rates", "home prices", "real estate"],
        "credit":              ["credit conditions", "credit markets", "lending", "bank credit", "default rates"],
        "financial conditions": ["financial conditions", "tightening conditions", "easing conditions"],
        "headwinds":           ["headwind", "slowdown", "weakness", "softness"],
        "resilient":           ["resilient", "robust", "strong growth", "holding up"],
        "accommodation":       ["accommodation", "accommodative", "loose policy", "supportive policy"],

        # ── Trump-speech / political catchphrases ───────────────────────
        "tariffs":             ["tariff", "trade war", "import duties", "china trade", "trade policy"],
        "china":               ["china", "xi jinping", "beijing", "chinese"],
        "biden":               ["biden", "former president biden", "sleepy joe"],
        "obama":               ["obama", "barack obama"],
        "maga":                ["maga", "make america great", "america first"],
        "beautiful":           ["big beautiful bill", "beautiful"],
        "tremendous":          ["tremendous", "incredible", "record-breaking"],
        "drill baby drill":    ["drill baby drill", "domestic oil", "energy dominance", "unleash american energy"],
        "drill":               ["drill", "oil drilling", "energy production", "ANWR"],
        "border":              ["border", "southern border", "border security", "border crossings"],
        "wall":                ["border wall", "wall construction"],
        "immigrant":           ["immigrant", "migrant", "immigration"],
        "illegal":             ["illegal immigration", "illegal alien", "deportation"],
        "deport":              ["deport", "deportation", "remove illegals", "ice raids"],
        "tax":                 ["tax cuts", "no tax on tips", "no tax on overtime", "tax break"],
        "tips":                ["no tax on tips", "tip exemption"],
        "oil":                 ["oil prices", "crude oil", "opec", "energy prices"],
        "gas":                 ["gas prices", "gasoline", "pump prices"],
        "iran":                ["iran", "iranian regime", "tehran", "nuclear deal"],
        "israel":              ["israel", "netanyahu", "idf", "gaza"],
        "hormuz":              ["strait of hormuz", "hormuz", "iran naval"],
        "crypto":              ["crypto", "bitcoin", "digital assets", "stablecoin"],
        "bitcoin":             ["bitcoin", "btc", "crypto reserve"],
        "elon":                ["elon musk", "musk", "doge", "department of government efficiency"],
        "doge":                ["department of government efficiency", "doge", "government cuts"],
        "fake news":           ["fake news", "mainstream media", "msm"],
        "epstein":             ["epstein", "epstein files", "epstein list"],
        "pelosi":              ["pelosi", "nancy pelosi"],

        # ── Earnings-call language ──────────────────────────────────────
        "ai":                  ["ai", "artificial intelligence", "generative ai", "llm", "machine learning"],
        "guidance":            ["guidance", "forecast", "outlook", "raised guidance", "lowered guidance"],
        "margin":              ["margin expansion", "margin compression", "operating margin", "gross margin"],
        "tailwind":            ["tailwind", "favorable conditions", "boost", "momentum"],
        "demand":              ["strong demand", "weak demand", "demand growth", "demand softness"],
        "growth":              ["growth", "revenue growth", "double-digit growth"],
        "acquisition":         ["acquisition", "m&a", "deal", "takeover", "buyout"],
        "dividend":            ["dividend", "dividend increase", "dividend cut", "capital return"],
        "buyback":             ["buyback", "share repurchase", "return to shareholders"],
        "macro":                ["macro", "macroeconomic", "economic backdrop", "economic conditions"],
        "supply chain":        ["supply chain", "logistics", "inventory", "shortage"],
        "fsd":                 ["full self driving", "fsd", "autonomy", "autonomous"],
        "gigafactory":         ["gigafactory", "new factory", "factory expansion"],
        "cybertruck":          ["cybertruck", "pickup truck"],
        "roadster":            ["roadster", "new vehicle"],
    }

    boost = 0.0
    if term_mentions > 0:
        boost += min(term_mentions * 0.3, 1.0)

    kws = positive_keywords.get(term_lower, [])
    for kw in kws:
        if kw in headlines_text:
            boost += 0.4

    # Cap the delta
    delta = max(-2.0, min(2.0, boost))

    reasoning = f"Heuristic: term '{term}' found {term_mentions}x in headlines"
    if boost > 0.3:
        reasoning += f", positive context keywords detected (+{delta:.1f})"
    else:
        reasoning += ", neutral context"

    return {
        "context_delta": delta,
        "reasoning": reasoning,
        "confidence": "low",
    }


def score_term_context(
    term: str,
    event_type: str,
    event_date: str,
    base_rate: float,
    recent_transcripts: list[str] = None,
    news_headlines: list[str] = None,
) -> dict:
    """
    Calls Claude API with structured prompt.
    Returns: { context_delta: float, reasoning: str, adjusted_prob: float, confidence: str }

    Falls back to heuristic if no API key configured.
    """
    # Get recent transcripts from DB if not provided
    if recent_transcripts is None:
        recent_transcripts = get_recent_transcripts(event_type, term)

    if news_headlines is None:
        news_headlines = []

    # Get frequency stats
    stats = get_frequency_stats(event_type, term)

    client = _get_anthropic_client()

    if client is None:
        # Heuristic fallback
        result = _heuristic_context_delta(term, base_rate, news_headlines)
    else:
        # Build prompt
        system_prompt, user_prompt = _build_prompt(
            term=term,
            event_type=event_type,
            event_date=event_date,
            base_rate=base_rate,
            k=stats["k"],
            n=stats["n"],
            recent_transcripts=recent_transcripts if isinstance(recent_transcripts, list) and len(recent_transcripts) > 0 and isinstance(recent_transcripts[0], dict) else get_recent_transcripts(event_type, term),
            news_headlines=news_headlines,
        )

        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            # Parse JSON response
            text = response.content[0].text.strip()
            # Remove markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            result = json.loads(text)

        except json.JSONDecodeError as e:
            print(f"  Warning: Claude returned non-JSON for '{term}': {e}")
            result = _heuristic_context_delta(term, base_rate, news_headlines)
        except Exception as e:
            print(f"  Warning: Claude API call failed for '{term}': {e}")
            result = _heuristic_context_delta(term, base_rate, news_headlines)

    # Clamp context_delta
    context_delta = max(-2.0, min(2.0, float(result.get("context_delta", 0.0))))

    # Compute adjusted probability
    adjusted_prob = _apply_context_delta(base_rate, context_delta)

    return {
        "context_delta": context_delta,
        "reasoning": result.get("reasoning", ""),
        "confidence": result.get("confidence", "low"),
        "adjusted_prob": adjusted_prob,
        "base_rate": base_rate,
    }


if __name__ == "__main__":
    # Quick test with a known term
    from engine.frequency import get_base_rate

    test_terms = ["disinflation", "stagflation", "soft landing", "tariffs", "restrictive"]
    print("CONTEXT SCORER — TEST RUN")
    print("=" * 60)

    for term in test_terms:
        base_rate = get_base_rate("fomc_presser", term)
        if base_rate is None:
            print(f"  {term}: insufficient data (n < 5)")
            continue

        result = score_term_context(
            term=term,
            event_type="fomc_presser",
            event_date="2026-05-07",
            base_rate=base_rate,
            news_headlines=[
                "Fed holds rates steady amid trade uncertainty",
                "Tariffs drive inflation fears higher",
                "Powell signals patience on rate cuts",
                "Labor market remains resilient despite slowdown",
                "CPI shows continued disinflation trend",
            ],
        )
        print(f"\n  {term}:")
        print(f"    Base rate:     {base_rate:.1%}")
        print(f"    Context delta: {result['context_delta']:+.2f}")
        print(f"    Adjusted prob: {result['adjusted_prob']:.1%}")
        print(f"    Reasoning:     {result['reasoning']}")
        print(f"    Confidence:    {result['confidence']}")
