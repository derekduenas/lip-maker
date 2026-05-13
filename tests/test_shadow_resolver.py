"""Tests for loop/shadow_resolver — paper PnL math + Brier + domain tagging."""
from __future__ import annotations

import os, sqlite3, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestPaperPnL(unittest.TestCase):
    def test_yes_win(self):
        from loop.shadow_resolver import _paper_pnl
        # bought 100 YES at 30c, settles YES → win 100*0.70 = +$70
        self.assertAlmostEqual(_paper_pnl("YES", 0.30, 100, "yes"), 70.0)

    def test_yes_loss(self):
        from loop.shadow_resolver import _paper_pnl
        # bought 100 YES at 30c, settles NO → lose 100*0.30 = -$30
        self.assertAlmostEqual(_paper_pnl("YES", 0.30, 100, "no"), -30.0)

    def test_no_win(self):
        from loop.shadow_resolver import _paper_pnl
        # bought 100 NO at 70c, settles NO → win 100*0.30 = +$30
        self.assertAlmostEqual(_paper_pnl("NO", 0.70, 100, "no"), 30.0)

    def test_zero_contracts(self):
        from loop.shadow_resolver import _paper_pnl
        self.assertEqual(_paper_pnl("YES", 0.5, 0, "yes"), 0.0)


class TestBrier(unittest.TestCase):
    def test_perfect_yes(self):
        from loop.shadow_resolver import _brier_for_yes_prob
        self.assertEqual(_brier_for_yes_prob(1.0, "yes"), 0.0)

    def test_perfect_no(self):
        from loop.shadow_resolver import _brier_for_yes_prob
        self.assertEqual(_brier_for_yes_prob(0.0, "no"), 0.0)

    def test_worst_case(self):
        from loop.shadow_resolver import _brier_for_yes_prob
        # predicted 1.0 yes, actual was no → max wrong → brier = 1.0
        self.assertEqual(_brier_for_yes_prob(1.0, "no"), 1.0)

    def test_uncertain(self):
        from loop.shadow_resolver import _brier_for_yes_prob
        # predicted 0.5, actual yes → (0.5)^2 = 0.25
        self.assertAlmostEqual(_brier_for_yes_prob(0.5, "yes"), 0.25)

    def test_none_input_returns_none(self):
        from loop.shadow_resolver import _brier_for_yes_prob
        self.assertIsNone(_brier_for_yes_prob(None, "yes"))


class TestDomainInference(unittest.TestCase):
    def test_trump_mention(self):
        from loop.shadow_resolver import _infer_domain
        self.assertEqual(_infer_domain("KXTRUMPMENTION-26MAY12-BIDE"), "trump_mention")
        self.assertEqual(_infer_domain("KXTRUMPSAYNICKNAME-26JUL01-WITC"), "trump_mention")
        self.assertEqual(_infer_domain("KXTRUMPFIRE-27-0"), "trump_mention")

    def test_fed_mention(self):
        from loop.shadow_resolver import _infer_domain
        self.assertEqual(_infer_domain("KXFEDMENTION-26MAY-INFL"), "fed_mention")

    def test_earnings(self):
        from loop.shadow_resolver import _infer_domain
        self.assertEqual(_infer_domain("KXEARNINGSMENTIONNVDA-26MAY20"), "earnings_mention")

    def test_other(self):
        from loop.shadow_resolver import _infer_domain
        self.assertEqual(_infer_domain("KXBOGUS-1"), "other")


class _TempDBMixin:
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db_path = self.tmp.name
        self._patchers = [
            patch("config.settings.DB_PATH", self.db_path),
            patch("loop.shadow_resolver.DB_PATH", self.db_path),
        ]
        for p in self._patchers:
            p.start()
        c = sqlite3.connect(self.db_path)
        c.executescript("""
        CREATE TABLE shadow_trades (
            id INTEGER PRIMARY KEY, kalshi_market_id TEXT NOT NULL,
            term TEXT NOT NULL, event_type TEXT, side TEXT NOT NULL,
            price REAL NOT NULL, contracts INTEGER NOT NULL,
            total_cost REAL NOT NULL, edge REAL, confidence_weight REAL,
            strategy TEXT, corpus_n INTEGER, estimated_prob REAL,
            market_price REAL, outcome TEXT DEFAULT 'OPEN', pnl REAL,
            placed_at TIMESTAMP, resolved_at TIMESTAMP);
        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY, kalshi_market_id TEXT,
            term TEXT, base_rate REAL, context_score REAL,
            estimated_prob REAL, market_price REAL, edge REAL,
            kelly_fraction REAL, parlay_flag BOOLEAN, detected_at TIMESTAMP);
        """)
        c.commit(); c.close()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


class TestBackfillEstimatedProb(_TempDBMixin, unittest.TestCase):
    def test_backfill_from_opportunities(self):
        from loop.shadow_resolver import _get_conn, backfill_estimated_prob
        conn = _get_conn()
        # Insert a shadow_trade with NULL estimated_prob
        conn.execute(
            """INSERT INTO shadow_trades (kalshi_market_id, term, side, price,
                                          contracts, total_cost, market_price)
               VALUES ('KXTRUMPMENTION-26MAY12-BIDE', 'biden', 'YES', 0.18,
                       378, 68.04, 0.18)"""
        )
        # And a matching opportunity with estimated_prob populated
        conn.execute(
            """INSERT INTO opportunities (kalshi_market_id, term, estimated_prob,
                                          market_price, edge, detected_at)
               VALUES ('KXTRUMPMENTION-26MAY12-BIDE', 'biden', 0.97, 0.18, 0.79,
                       '2026-05-12T18:01:00Z')"""
        )
        conn.commit()
        n = backfill_estimated_prob(conn)
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT estimated_prob FROM shadow_trades WHERE kalshi_market_id LIKE 'KXTRUMPMENTION%'"
        ).fetchone()
        self.assertAlmostEqual(row[0], 0.97)
        conn.close()


if __name__ == "__main__":
    unittest.main()
