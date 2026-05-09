"""Tests for heuristic scorer — the three-layer signal math."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.context_scorer_claude import _heuristic_fallback


def test_no_signals_returns_neutral():
    delta, _ = _heuristic_fallback("recession", 0.30, [])
    assert delta == 0.0


def test_transcript_momentum_lifts():
    recent = [
        {"raw_text": "We discussed recession risk"},
        {"raw_text": "Labor market and recession concerns"},
    ]
    delta, r = _heuristic_fallback("recession", 0.50, recent)
    assert delta > 0
    assert "transcripts" in r


def test_news_heat_lifts():
    """3+ news hits → +0.6 delta."""
    news = [
        "Fed warns of recession risk",
        "Goldman sees recession by Q4",
        "Recession odds climb",
        "No mention of this word",
    ]
    delta, r = _heuristic_fallback("recession", 0.50, [], news_headlines=news)
    assert delta >= 0.6
    assert "news-heat" in r


def test_speech_title_hit_is_strongest():
    """Word appearing in the FIRST 200 chars of an intermeeting speech should
    give the biggest lift — this is the alpha capture."""
    speeches = [
        {"raw_text": "One Transitory Shock After Another. Full speech body here with lots of content"},
    ]
    delta, r = _heuristic_fallback("transitory", 0.05, [], intermeeting_speeches=speeches)
    assert delta >= 0.8
    assert "speech-title" in r


def test_speech_body_hit_smaller_than_title():
    """Word only in speech body (not title) → smaller lift."""
    speeches = [
        {"raw_text": ("Rural Communities: Worth the Investment. "
                      + "X " * 200 + "recession in the latter part of the speech")},
    ]
    delta, r = _heuristic_fallback("recession", 0.50, [],
                                    intermeeting_speeches=speeches)
    # Only 1 speech-hit (not title) → smaller lift
    # delta should be 0.3 for speech-hit
    assert 0.2 <= delta <= 0.5


def test_multi_word_term_news_hits():
    """'Trade War' should match when 'trade' and 'war' both in headline."""
    news = ["Tariff talk escalates trade war fears"]
    delta, r = _heuristic_fallback("trade war", 0.04, [], news_headlines=news)
    assert delta > 0


def test_rare_and_cold_slight_negative():
    """Rare word with no signals gets slight downward push."""
    delta, _ = _heuristic_fallback("kalshi", 0.013, [])
    assert delta < 0


def test_stacked_signals_combine():
    """Transcript + news + speech signals all stack."""
    recent = [{"raw_text": "We discussed tariffs at length"}] * 2
    news = ["Tariff Inflation jumps", "Tariff concerns mount", "Tariffs reshape trade"]
    speeches = [{"raw_text": "Tariffs and Their Inflation Effects. Full body."}]
    delta, r = _heuristic_fallback("tariffs", 0.30, recent,
                                    news_headlines=news,
                                    intermeeting_speeches=speeches)
    # Should get transcript (+0.4) + news-heat (+0.6) + speech-title (+0.8) = ~1.8 clamped
    assert delta >= 1.5
    assert "transcripts" in r and "news" in r and "speech" in r


def test_delta_clamped_positive():
    """Max delta capped at +1.8."""
    recent = [{"raw_text": "word"}] * 5
    news = [f"word appears in headline {i}" for i in range(10)]
    speeches = [{"raw_text": "word in title. " + "word " * 100}] * 5
    delta, _ = _heuristic_fallback("word", 0.50, recent,
                                    news_headlines=news,
                                    intermeeting_speeches=speeches)
    assert delta <= 1.8


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
