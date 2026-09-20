"""Ordering, restart and consumer recovery regressions (no venue calls)."""
import sqlite3
from unittest.mock import MagicMock

from execution.quote_manager import QuoteManager, QuoteTarget
from tools.fill_consumers import drain_fill_consumers
from tests.test_state_consistency import db, live_qm, _order, _venue, _FakeClient, TKR


def reconcile(qm, quantity):
    qm.client = _FakeClient([_venue('o1', remaining=str(quantity))] if quantity else [])
    return qm.periodic_resync()


def ingest(qm, tid='trade', quantity=10):
    return qm.apply_fill('o1', TKR, quantity, trade_id=tid, side='yes', price_cents=50)


def test_rest_before_ws_does_not_subtract_twice(live_qm):
    live_qm.resting[TKR] = [_order('yes', 50, 100, oid='o1')]
    reconcile(live_qm, 90)
    ingest(live_qm)
    assert live_qm.resting[TKR][0].size_contracts == 90
    assert TKR in live_qm.uncertain_markets
    assert live_qm.reconcile(QuoteTarget(TKR, 50, 49, 100))['reason'] == 'ORDER_STATE_UNCERTAIN'
    reconcile(live_qm, 90)
    assert live_qm.resting[TKR][0].size_contracts == 90
    assert not live_qm.uncertain_markets


def test_ledger_before_ws_still_invalidates_local_consumer(live_qm):
    live_qm.resting[TKR] = [_order('yes', 50, 100, oid='o1')]
    live_qm._ledger_fill(trade_id='trade', order_id='o1', ticker=TKR, side='yes',
                        count=10, price_cents=50, is_taker=False, exchange_ts=None, subaccount='')
    ingest(live_qm)
    assert TKR in live_qm.uncertain_markets
    reconcile(live_qm, 90)
    assert live_qm.resting[TKR][0].size_contracts == 90


def test_authoritative_snapshot_can_repair_old_undercount(live_qm):
    live_qm.resting[TKR] = [_order('yes', 50, 80, oid='o1')]
    reconcile(live_qm, 90)
    assert live_qm.resting[TKR][0].size_contracts == 90


def test_old_snapshot_cannot_remove_new_order(live_qm):
    live_qm.client = MagicMock()
    def fetch():
        live_qm.paper = True
        live_qm._place_order(TKR, 'yes', 50, 10)
        live_qm.paper = False
        return {}
    live_qm._fetch_live_orders = fetch
    assert live_qm.periodic_resync()['retry_required']
    assert len(live_qm.resting[TKR]) == 1
    assert TKR in live_qm.uncertain_markets


def test_persistence_failure_is_retryable_and_blocks_quotes(live_qm, monkeypatch):
    writer = live_qm._ledger_fill
    monkeypatch.setattr(live_qm, '_ledger_fill', lambda **kw: False)
    ingest(live_qm)
    assert live_qm.last_fill_status == 'persistence_failed'
    assert 'trade' not in live_qm._seen_fills
    reconcile(live_qm, 90)
    assert TKR in live_qm.uncertain_markets
    monkeypatch.setattr(live_qm, '_ledger_fill', writer)
    ingest(live_qm)
    reconcile(live_qm, 90)
    assert not live_qm.uncertain_markets


def test_restart_after_ingestion_before_local_apply(live_qm, db):
    ingest(live_qm)
    fresh = QuoteManager(paper=True, db_path=db)
    fresh.paper = False
    fresh.resting[TKR] = [_order('yes', 50, 90, oid='o1')]
    ingest(fresh)
    reconcile(fresh, 90)
    assert fresh.resting[TKR][0].size_contracts == 90
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM fill_ledger').fetchone()[0] == 1


def quote_row(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO quotes (order_id,market_ticker,side,size_contracts,status) VALUES ('o1',?,'yes',100,'resting')", (TKR,))


def test_ws_first_consumers_replay_without_duplicate_effects(live_qm, db, monkeypatch):
    quote_row(db)
    ingest(live_qm, quantity=10.5)
    # No market-history data yet: markout remains pending independently.
    import monitor.markout_logger as markout
    compute = MagicMock(return_value=False)
    monkeypatch.setattr(markout, 'compute_markouts_for_fill', compute)
    drain_fill_consumers(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT fill_size,status FROM quotes').fetchone() == (10.5, 'resting')
        assert conn.execute('SELECT COUNT(*) FROM hedge_log').fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM fill_consumer_receipts WHERE consumer='markout'").fetchone()
    compute.return_value = True
    drain_fill_consumers(db)
    drain_fill_consumers(db)
    assert compute.call_count == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT fill_size FROM quotes').fetchone()[0] == 10.5
        assert conn.execute('SELECT COUNT(*) FROM hedge_log').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM fill_consumer_receipts').fetchone()[0] == 3


def test_quote_row_arriving_after_fill_is_recovered(live_qm, db):
    ingest(live_qm)
    drain_fill_consumers(db)
    quote_row(db)
    drain_fill_consumers(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT fill_size FROM quotes').fetchone()[0] == 10


def test_rest_sync_drains_preexisting_ws_rows(live_qm, db, monkeypatch):
    from tools import fills_sync
    quote_row(db)
    ingest(live_qm, quantity=10.5)
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE inventory (market_ticker TEXT PRIMARY KEY, net_yes_contracts REAL, gross_usd REAL, avg_yes_entry REAL, avg_no_entry REAL, last_updated TEXT)')
    client = MagicMock()
    client.get.return_value = {'fills': []}
    monkeypatch.setattr(fills_sync, 'KalshiClient', lambda: client)
    fills_sync.sync_fills(db)
    fills_sync.sync_fills(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT fill_size FROM quotes').fetchone()[0] == 10.5
        assert conn.execute('SELECT net_yes_contracts FROM inventory').fetchone()[0] == 10.5


def test_quote_effect_and_receipt_rollback_together(live_qm, db):
    quote_row(db)
    ingest(live_qm)
    # Initialize consumer schema without processing the target fill.
    drain_fill_consumers(db, trade_id='absent')
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TRIGGER fail_quote_receipt BEFORE INSERT ON fill_consumer_receipts
            WHEN NEW.consumer='quotes' BEGIN SELECT RAISE(ABORT,'injected crash'); END""")
    drain_fill_consumers(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT fill_size FROM quotes').fetchone()[0] is None
        conn.execute('DROP TRIGGER fail_quote_receipt')
    drain_fill_consumers(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT fill_size FROM quotes').fetchone()[0] == 10


def test_hedge_diagnostic_replay_after_receipt_failure_never_executes(live_qm, db, monkeypatch):
    from cross_venue import hedger
    execute = MagicMock(side_effect=AssertionError('No live hedges from replay'))
    monkeypatch.setattr(hedger, '_execute_if_enabled', execute)
    ingest(live_qm)
    drain_fill_consumers(db, trade_id='absent')
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TRIGGER fail_hedge_receipt BEFORE INSERT ON fill_consumer_receipts
            WHEN NEW.consumer='hedge_diagnostic' BEGIN SELECT RAISE(ABORT,'injected crash'); END""")
    drain_fill_consumers(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM hedge_log').fetchone()[0] == 1
        conn.execute('DROP TRIGGER fail_hedge_receipt')
    drain_fill_consumers(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM hedge_log').fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fill_consumer_receipts WHERE consumer='hedge_diagnostic'").fetchone()[0] == 1
    execute.assert_not_called()


def test_other_successful_fill_does_not_clear_failed_ingestion(live_qm, monkeypatch):
    writer = live_qm._ledger_fill
    monkeypatch.setattr(live_qm, '_ledger_fill', lambda **kw: False)
    ingest(live_qm, tid='failed')
    monkeypatch.setattr(live_qm, '_ledger_fill', writer)
    ingest(live_qm, tid='different')
    reconcile(live_qm, 80)
    assert TKR in live_qm.uncertain_markets


def test_no_fill_with_only_yes_price_retains_side_cost():
    from execution.kalshi_ws import KalshiWS
    ev = KalshiWS._parse_fill(dict(trade_id='t', order_id='o', market_ticker=TKR,
                                  side='no', count_fp='10.50', yes_price_dollars='0.7500'))
    assert ev.price_cents_exact == 25
