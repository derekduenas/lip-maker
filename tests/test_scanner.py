"""Tests for engine/scanner.py — market classification and demo data."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.scanner import classify_market, _get_demo_mention_markets, KalshiClient


def test_classify_fomc_market():
    market = {"title": 'Will Powell say "disinflation" at FOMC press conference on May 7?',
              "ticker": "KXFOMCMENTION-DISINFLATION-26MAY07", "subtitle": "FOMC 2026-05-07"}
    result = classify_market(market)
    assert result["event_type"] == "fomc_presser"
    assert result["speaker"] == "powell"
    assert result["term"] == "disinflation"


def test_classify_trump_market():
    market = {"title": 'Will Trump say "tariffs" in State of the Union?',
              "ticker": "KXTRUMP-TARIFFS", "subtitle": "Trump speech"}
    result = classify_market(market)
    assert result["event_type"] == "trump_speech"
    assert result["speaker"] == "trump"
    assert result["term"] == "tariffs"


def test_classify_tesla_market():
    market = {"title": 'Will Elon Musk mention "autonomy" on Tesla earnings call?',
              "ticker": "KXTSLA-AUTONOMY", "subtitle": "Tesla TSLA earnings"}
    result = classify_market(market)
    assert result["event_type"] == "tsla_earnings"
    assert result["speaker"] == "musk"


def test_classify_unknown_market():
    market = {"title": "Something completely unrelated", "ticker": "KXOTHER", "subtitle": ""}
    result = classify_market(market)
    assert result["event_type"] == "other"


def test_demo_markets_have_required_fields():
    markets = _get_demo_mention_markets()
    assert len(markets) >= 10
    for m in markets:
        assert "ticker" in m
        assert "title" in m
        assert "yes_price" in m
        assert 0 < m["yes_price"] < 1
        assert "rules" in m


def test_classify_extracts_date():
    market = {"title": 'Will Powell say "inflation" at FOMC on 2026-05-07?',
              "ticker": "KXFOMCMENTION", "subtitle": "FOMC 2026-05-07"}
    result = classify_market(market)
    assert result["event_date"] == "2026-05-07"


def test_client_no_auth_returns_demo():
    client = KalshiClient()
    if not client.authenticated:
        markets = client.get_mention_markets()
        assert len(markets) > 0
        assert all("ticker" in m for m in markets)
