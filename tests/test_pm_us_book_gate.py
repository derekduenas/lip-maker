"""PM US quotes use the websocket book. REST is a Cloudflare cache."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket.execution.pm_book_gate import quote_book, ws_book_is_quotable


def test_rest_order_book_is_never_a_quote():
    # Live check 2026-10-01: signed GET on api.polymarket.us, cache HIT, age 15-16s.
    rest = {"best_bid": 0.48, "best_ask": 0.52, "top_bid_size": 100, "top_ask_size": 100}
    book, decision = quote_book(
        ws_book=None, ws_updated_at=None, rest_book=rest, now=100.0,
    )
    assert decision == "pull"
    assert book is None
    assert not ws_book_is_quotable(15.5)


def test_websocket_book_older_than_one_second_pulls():
    ws = {"best_bid": 0.49, "best_ask": 0.51, "top_bid_size": 20, "top_ask_size": 18}
    book, decision = quote_book(
        ws_book=ws, ws_updated_at=98.5, rest_book={"best_bid": 0.10, "best_ask": 0.90},
        now=100.0,
    )
    assert decision == "pull"
    assert book is None
    assert not ws_book_is_quotable(1.01)


def test_fresh_websocket_book_ignores_the_rest_book():
    # Median websocket age on the same live check was 0.16s.
    ws = {"best_bid": 0.49, "best_ask": 0.51, "top_bid_size": 20, "top_ask_size": 18}
    rest = {"best_bid": 0.10, "best_ask": 0.90, "top_bid_size": 1, "top_ask_size": 1}
    book, decision = quote_book(
        ws_book=ws, ws_updated_at=99.84, rest_book=rest, now=100.0,
    )
    assert decision == "quote"
    assert book["source"] == "ws"
    assert book["best_bid"] == 0.49
    assert book["best_ask"] == 0.51
    assert book["top_bid_size"] == 20
    assert book["age_s"] == pytest.approx(0.16)
    assert ws_book_is_quotable(0.16)
    assert ws_book_is_quotable(1.0)
