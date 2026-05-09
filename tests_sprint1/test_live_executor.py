"""Tests for prep/live_executor.py — paper mode, order persistence, batch ordering."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.live_executor import (
    PlacedOrder, place_event_batch, place_order,
)
from prep.risk_gates import OrderCandidate


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _good_candidate(**overrides):
    base = dict(
        ticker="KXFEDMENTION-26APR-TEST",
        side="no",
        price_cents=50,
        contracts=10,
        bankroll_usd=5000.0,
        event_id="fomc_20260430",
        event_exposure_usd=0.0,
        thesis_generated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return OrderCandidate(**base)


def test_paper_mode_returns_paper_order(monkeypatch):
    """Default: no SOV_LIVE → paper mode, order logged with PAPER- prefix."""
    monkeypatch.delenv("SOV_LIVE", raising=False)
    monkeypatch.setenv("SOV_LIVE", "true")  # enable gates to pass
    db = _tmp_db()
    try:
        po = place_order(_good_candidate(), db_path=db, dry_run=True,
                         thesis_payload={"test": "data"})
        assert po.status == "paper"
        assert po.order_id.startswith("PAPER-")
        assert po.paper is True
    finally:
        os.unlink(db)


def test_order_persisted_to_db(monkeypatch):
    monkeypatch.setenv("SOV_LIVE", "true")
    db = _tmp_db()
    try:
        po = place_order(_good_candidate(), db_path=db, dry_run=True,
                         thesis_payload={"word": "iran"})

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT ticker, side, contracts, status FROM sovereign_orders"
        ).fetchone()
        conn.close()
        assert row[0] == "KXFEDMENTION-26APR-TEST"
        assert row[3] == "paper"
    finally:
        os.unlink(db)


def test_rejected_order_still_logged(monkeypatch):
    """Even rejected orders get persisted (for audit)."""
    monkeypatch.setenv("SOV_LIVE", "true")
    db = _tmp_db()
    try:
        # Tiny size → G8 reject
        po = place_order(_good_candidate(contracts=1), db_path=db, dry_run=True)
        assert po.status == "rejected"
        assert "MIN_SIZE" in po.reason

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT status, reason FROM sovereign_orders"
        ).fetchone()
        conn.close()
        assert row[0] == "rejected"
    finally:
        os.unlink(db)


def test_batch_tracks_exposure(monkeypatch):
    """Successive orders in a batch should see growing event_exposure → G5 kicks in."""
    monkeypatch.setenv("SOV_LIVE", "true")
    db = _tmp_db()
    try:
        # 10 theses @ $200 each (= 8 events cost $1600 > $1500 G5 cap).
        # bankroll $5000 → G4 per-position cap = 8% = $400. $200 passes G4.
        # G5 event cap = 30% = $1500. 8th order (cumulative $1600) → rejects.
        theses = [
            {
                "ticker": f"KXFEDMENTION-26APR-W{i:02d}",
                "word": f"word{i:02d}",
                "best_side": "no",
                "yes_ask": 50,
                "recommended_usd": 200.0,
                "conviction": "HOMERUN",
            }
            for i in range(10)
        ]
        placed = place_event_batch(
            theses, event_id="fomc_20260430", bankroll=5000.0,
            db_path=db, dry_run=True,
        )
        # 7 should succeed (total $1400), 3 rejected on G5 (event cap).
        paper_n = len([p for p in placed if p.status == "paper"])
        rej_n = len([p for p in placed if p.status == "rejected"])
        assert paper_n == 7, f"expected 7 paper, got {paper_n}"
        assert rej_n == 3, f"expected 3 rejected, got {rej_n}"
        assert all("PER_EVENT" in p.reason for p in placed if p.status == "rejected")
    finally:
        os.unlink(db)


def test_batch_skips_conviction_skip(monkeypatch):
    """Theses with conviction=SKIP are bypassed."""
    monkeypatch.setenv("SOV_LIVE", "true")
    db = _tmp_db()
    try:
        theses = [
            {"ticker": "KXT-1", "word": "w1", "best_side": "yes", "yes_ask": 50,
             "recommended_usd": 100.0, "conviction": "SKIP"},
            {"ticker": "KXT-2", "word": "w2", "best_side": "yes", "yes_ask": 50,
             "recommended_usd": 100.0, "conviction": "HOMERUN"},
        ]
        placed = place_event_batch(
            theses, event_id="ev1", bankroll=5000.0, db_path=db, dry_run=True,
        )
        assert len(placed) == 1  # only HOMERUN placed


    finally:
        os.unlink(db)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
