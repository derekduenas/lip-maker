"""P1 regressions — estimated rewards must never be treated as paid
(2026-09-21).

The defect: tools/settlement_reconciler.py computed an estimated LIP rebate
from our own snapshot model, wrote it into settlement_log.rebate_earned_usd
and net_outcome_usd, and fed it to calibration_ewma.update(actual_usd=...)
commented "what we actually earned". The model calibrated itself, and
tools/go_live_check.py sums net_outcome_usd to authorize live trading.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine import calibration_ewma, reward_provenance as prov

TKR = "KXBRENTD-26JUN0117-T100"


def _settlement_db(tmp_path, *, rebate_earned=4.0, realized=-1.0):
    """A settlement_log in the PRE-fix shape: an estimate sitting in the
    column whose name claims it was earned."""
    path = tmp_path / "settle.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE settlement_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT UNIQUE,
            series_prefix TEXT, close_time TEXT, our_realized_usd REAL,
            rebate_earned_usd REAL, net_outcome_usd REAL, recorded_at TEXT
        );
        CREATE TABLE lip_programs (
            id TEXT PRIMARY KEY, market_ticker TEXT, start_date TEXT,
            reward_per_day_usd REAL
        );
    """)
    conn.execute("INSERT INTO settlement_log (ticker, series_prefix, close_time, "
                 "our_realized_usd, rebate_earned_usd, net_outcome_usd, recorded_at) "
                 "VALUES (?, 'KXBRENTD', '2026-09-01', ?, ?, ?, '2026-09-01')",
                 (TKR, realized, rebate_earned, realized + rebate_earned))
    conn.execute("INSERT INTO lip_programs VALUES ('p1', ?, '2026-09-01', 20.0)", (TKR,))
    conn.commit(); conn.close()
    return str(path)


def _row(db, *cols):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            f"SELECT {', '.join(cols)} FROM settlement_log WHERE ticker = ?",
            (TKR,)).fetchone()
    finally:
        conn.close()


class TestMigration:
    def test_legacy_estimate_is_quarantined_not_deleted(self, tmp_path):
        db = _settlement_db(tmp_path, rebate_earned=4.0, realized=-1.0)
        assert prov.migrate_estimates(db)["migrated"] == 1
        est, paid, earned, net, net_est, provenance = _row(
            db, "rebate_estimated_usd", "rebate_paid_usd", "rebate_earned_usd",
            "net_outcome_usd", "net_outcome_estimated_usd", "reward_provenance")
        assert est == 4.0                    # preserved, not destroyed
        assert paid is None                  # nothing was ever paid
        assert earned == 0                   # the deprecated alias is paid-only now
        assert net == -1.0                   # cash = realized trading only
        assert net_est == 3.0                # realized + estimate, kept separately
        assert provenance == prov.PROV_ESTIMATE

    def test_migration_is_idempotent(self, tmp_path):
        db = _settlement_db(tmp_path)
        assert prov.migrate_estimates(db)["migrated"] == 1
        assert prov.migrate_estimates(db)["migrated"] == 0
        assert _row(db, "rebate_estimated_usd")[0] == 4.0

    def test_migration_does_not_invent_paid_evidence(self, tmp_path):
        db = _settlement_db(tmp_path, rebate_earned=99.0)
        prov.migrate_estimates(db)
        assert prov.paid_total(db) == 0.0
        assert prov.has_paid_evidence(db) is False

    def test_schema_absent_is_tolerated(self, tmp_path):
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        assert prov.ensure_schema(str(empty)) is False
        assert prov.migrate_estimates(str(empty))["migrated"] == 0


class TestPaymentRecording:
    def test_payment_populates_paid_columns(self, tmp_path):
        db = _settlement_db(tmp_path, rebate_earned=4.0, realized=-1.0)
        prov.migrate_estimates(db)
        res = prov.record_payment(TKR, 2.5, source="kalshi_statement", db_path=db)
        assert res["updated"] == 1
        paid, earned, net, provenance, src = _row(
            db, "rebate_paid_usd", "rebate_earned_usd", "net_outcome_usd",
            "reward_provenance", "reward_source")
        assert paid == 2.5 and earned == 2.5
        assert net == 1.5                    # realized -1.0 + paid 2.5
        assert provenance == prov.PROV_PAID and src == "kalshi_statement"
        assert prov.has_paid_evidence(db) is True

    def test_estimate_does_not_overwrite_a_payment(self, tmp_path):
        db = _settlement_db(tmp_path)
        prov.migrate_estimates(db)
        prov.record_payment(TKR, 2.5, source="kalshi_statement", db_path=db)
        conn = sqlite3.connect(db)
        prov.record_estimate(conn, TKR, 9.9, realized_usd=-1.0)
        conn.commit(); conn.close()
        paid, provenance = _row(db, "rebate_paid_usd", "reward_provenance")
        assert paid == 2.5 and provenance == prov.PROV_PAID   # payment wins
        assert _row(db, "rebate_estimated_usd")[0] == 9.9     # estimate still tracked

    @pytest.mark.parametrize("bad", ["model_estimate", "our_model", "", "guess"])
    def test_non_independent_sources_rejected(self, tmp_path, bad):
        db = _settlement_db(tmp_path)
        with pytest.raises(ValueError, match="not an independent payment record"):
            prov.record_payment(TKR, 2.5, source=bad, db_path=db)

    def test_negative_payment_rejected(self, tmp_path):
        db = _settlement_db(tmp_path)
        with pytest.raises(ValueError):
            prov.record_payment(TKR, -1.0, source="kalshi_api", db_path=db)


class TestCalibrationRejectsEstimates:
    def test_update_without_provenance_is_rejected(self, tmp_path):
        db = str(tmp_path / "c.db")
        calibration_ewma.ensure_schema(db)
        assert calibration_ewma.update("KXBRENTD", 10.0, 2.5, db_path=db) is None

    def test_update_with_estimate_provenance_is_rejected(self, tmp_path):
        db = str(tmp_path / "c.db")
        calibration_ewma.ensure_schema(db)
        assert calibration_ewma.update("KXBRENTD", 10.0, 2.5,
                                       provenance="estimate", db_path=db) is None

    def test_update_with_paid_provenance_is_accepted(self, tmp_path):
        db = str(tmp_path / "c.db")
        calibration_ewma.ensure_schema(db)
        c = calibration_ewma.update("KXBRENTD", 10.0, 2.5,
                                    provenance="paid", db_path=db)
        assert c == pytest.approx(0.25)

    def test_preexisting_rows_are_quarantined_and_ignored(self, tmp_path, monkeypatch):
        """A DB written before the fix holds estimate-derived calibration.
        It must not steer sizing or ranking."""
        db = str(tmp_path / "c.db")
        conn = sqlite3.connect(db)
        conn.executescript(calibration_ewma.SCHEMA_DDL)
        conn.execute("INSERT INTO market_calibration (key, calibration, n_samples, "
                     "updated_at) VALUES ('KXBRENTD', 0.9, 50, '2026-09-01')")
        conn.commit(); conn.close()
        calibration_ewma.ensure_schema(db)          # migration marks it
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT provenance FROM market_calibration "
                            "WHERE key='KXBRENTD'").fetchone()[0] == \
            calibration_ewma.PROV_CONTAMINATED
        conn.close()
        monkeypatch.setattr(settings, "PER_MARKET_CALIB_ENABLED", True)
        # 0.9 was the contaminated value; consumers must see the prior instead.
        assert calibration_ewma.calib_for("KXBRENTD", fallback=0.25, db_path=db) == 0.25

    def test_paid_observation_reseeds_rather_than_blending_contaminated(self, tmp_path, monkeypatch):
        db = str(tmp_path / "c.db")
        conn = sqlite3.connect(db)
        conn.executescript(calibration_ewma.SCHEMA_DDL)
        conn.execute("INSERT INTO market_calibration (key, calibration, n_samples, "
                     "updated_at) VALUES ('KXBRENTD', 0.9, 50, '2026-09-01')")
        conn.commit(); conn.close()
        calibration_ewma.ensure_schema(db)
        c = calibration_ewma.update("KXBRENTD", 10.0, 2.5,
                                    provenance="paid", db_path=db)
        # Seeded from the payment (0.25), NOT blended toward the old 0.9.
        assert c == pytest.approx(0.25)
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT n_samples FROM market_calibration "
                            "WHERE key='KXBRENTD'").fetchone()[0] == 1
        conn.close()

    def test_premigration_db_yields_fallback(self, tmp_path, monkeypatch):
        """No provenance column at all → refuse every row."""
        db = str(tmp_path / "c.db")
        conn = sqlite3.connect(db)
        conn.executescript(calibration_ewma.SCHEMA_DDL)
        conn.execute("INSERT INTO market_calibration (key, calibration, n_samples, "
                     "updated_at) VALUES ('KXBRENTD', 0.9, 50, '2026-09-01')")
        conn.commit(); conn.close()
        monkeypatch.setattr(settings, "PER_MARKET_CALIB_ENABLED", True)
        assert calibration_ewma.calib_for("KXBRENTD", fallback=0.25, db_path=db) == 0.25


class TestGoLiveGate:
    def test_gate_fails_without_any_reconciled_payment(self, tmp_path):
        from tools.go_live_check import _gate_paid_reward_evidence
        db = _settlement_db(tmp_path, rebate_earned=500.0)   # big ESTIMATE
        prov.migrate_estimates(db)
        g = _gate_paid_reward_evidence(db)
        assert not g.passed and g.insufficient_data
        assert "model estimate" in g.detail

    def test_gate_passes_once_a_payment_is_reconciled(self, tmp_path):
        from tools.go_live_check import _gate_paid_reward_evidence
        db = _settlement_db(tmp_path)
        prov.migrate_estimates(db)
        prov.record_payment(TKR, 3.0, source="kalshi_statement", db_path=db)
        g = _gate_paid_reward_evidence(db)
        assert g.passed and g.observed == 3.0

    def test_estimate_no_longer_inflates_the_pnl_series(self, tmp_path):
        """The Sharpe/drawdown gates sum net_outcome_usd. After migration it
        must reflect realized trading cash only."""
        from tools.go_live_check import _daily_pnl_series
        db = _settlement_db(tmp_path, rebate_earned=500.0, realized=-2.0)
        before = _daily_pnl_series(db, "2026-01-01")
        assert before[0][1] == pytest.approx(498.0)    # the contaminated view
        prov.migrate_estimates(db)
        after = _daily_pnl_series(db, "2026-01-01")
        assert after[0][1] == pytest.approx(-2.0)      # honest view: a loss


class TestReconcilerWritesEstimatesOnly:
    def test_backfill_writes_estimated_columns_not_paid(self, tmp_path, monkeypatch):
        from tools import settlement_reconciler as sr
        db = _settlement_db(tmp_path, rebate_earned=0.0, realized=-1.0)
        prov.migrate_estimates(db)
        monkeypatch.setattr(sr, "_estimate_rebate", lambda conn, tkr: 7.5)
        res = sr.backfill_rebates(db_path=db, force=True)
        assert res["rows_updated"] == 1
        est, paid, earned, net = _row(
            db, "rebate_estimated_usd", "rebate_paid_usd",
            "rebate_earned_usd", "net_outcome_usd")
        assert est == 7.5          # estimate recorded
        assert paid is None        # but never as a payment
        assert earned == 0
        assert net == -1.0         # cash untouched by the estimate
        assert prov.has_paid_evidence(db) is False
