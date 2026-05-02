"""K1: capital_reaper signals immediate re-quote via shared queue.

Tests:
- enqueue() appends a JSON line per cancellation
- drain() returns + clears the queue atomically
- venue_filter only returns matching entries
- drain on empty file returns []
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch


def _patch_queue_path(tmp_path, monkeypatch):
    """Redirect the module-level QUEUE_PATH to a tmp file."""
    from cross_venue import requote_queue
    fake = tmp_path / "requote_queue.jsonl"
    monkeypatch.setattr(requote_queue, "QUEUE_PATH", fake)
    return requote_queue, fake


def test_enqueue_appends_one_line_per_cancellation(tmp_path, monkeypatch):
    rq, fake = _patch_queue_path(tmp_path, monkeypatch)
    rq.enqueue("kalshi", "KX-A")
    rq.enqueue("kalshi", "KX-B")
    rq.enqueue("pm", "tc-x")

    lines = fake.read_text().strip().split("\n")
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["venue"] == "kalshi" and parsed[0]["key"] == "KX-A"
    assert parsed[2]["venue"] == "pm" and parsed[2]["key"] == "tc-x"
    # ts present
    for p in parsed:
        assert "ts" in p


def test_drain_returns_and_clears(tmp_path, monkeypatch):
    rq, fake = _patch_queue_path(tmp_path, monkeypatch)
    rq.enqueue("kalshi", "KX-A")
    rq.enqueue("kalshi", "KX-B")

    drained = rq.drain()
    assert len(drained) == 2
    assert {d["key"] for d in drained} == {"KX-A", "KX-B"}
    # File is gone after drain
    assert not fake.exists()
    # Second drain returns empty
    assert rq.drain() == []


def test_drain_with_venue_filter(tmp_path, monkeypatch):
    rq, fake = _patch_queue_path(tmp_path, monkeypatch)
    rq.enqueue("kalshi", "KX-A")
    rq.enqueue("pm", "tc-x")
    rq.enqueue("kalshi", "KX-B")

    only_kalshi = rq.drain(venue_filter="kalshi")
    assert len(only_kalshi) == 2
    assert {d["key"] for d in only_kalshi} == {"KX-A", "KX-B"}
    # NOTE: drain() consumes the file even if filtering — this is the
    # intended single-consumer model. The pm entry is dropped if not
    # explicitly re-enqueued. Document this expectation in the test:
    assert rq.drain(venue_filter="pm") == []


def test_drain_on_missing_file_returns_empty(tmp_path, monkeypatch):
    rq, fake = _patch_queue_path(tmp_path, monkeypatch)
    assert not fake.exists()
    assert rq.drain() == []
