"""Replayable consumers of durable fills, independent of REST/WS ingestion.

Quote totals are recomputed, never incremented. Diagnostics have unique sink
keys and separate completion receipts. No executable hedge path is called.
"""
import logging
import sqlite3
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


def drain_fill_consumers(db_path: str, *, trade_id=None, limit=500):
    with sqlite3.connect(db_path, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS fill_consumer_receipts (
            trade_id TEXT, consumer TEXT, completed_at TEXT,
            PRIMARY KEY(trade_id, consumer))""")
        db.execute("""CREATE TABLE IF NOT EXISTS fill_consumer_attempts (
            trade_id TEXT PRIMARY KEY, attempted_at TEXT)""")
        rows = db.execute("""SELECT f.* FROM fill_ledger f
            LEFT JOIN fill_consumer_attempts a USING(trade_id)
            WHERE (? IS NULL OR f.trade_id=?) AND
            (SELECT COUNT(*) FROM fill_consumer_receipts r
             WHERE r.trade_id=f.trade_id AND r.consumer IN ('quotes','markout','hedge_diagnostic')) < 3
            ORDER BY a.attempted_at, f.created_at LIMIT ?""",
            (trade_id, trade_id, limit)).fetchall()
    quote_updates = 0
    for row in rows:
        f = dict(row)
        tid = f['trade_id']
        with sqlite3.connect(db_path, timeout=5) as db:
            db.execute("INSERT OR REPLACE INTO fill_consumer_attempts VALUES (?,?)",
                       (tid, datetime.now(timezone.utc).isoformat()))
        for consumer in ('quotes', 'markout', 'hedge_diagnostic'):
            with sqlite3.connect(db_path, timeout=5) as db:
                if db.execute("SELECT 1 FROM fill_consumer_receipts WHERE trade_id=? AND consumer=?",
                              (tid, consumer)).fetchone():
                    continue
            try:
                if consumer == 'quotes':
                    # Effect and receipt commit together; replay cannot double count.
                    with sqlite3.connect(db_path, timeout=5) as db:
                        db.execute('BEGIN IMMEDIATE')
                        n, price, ts = db.execute("""SELECT SUM(COALESCE(count_real,count)),
                            SUM(COALESCE(count_real,count) * CASE WHEN side='yes'
                                THEN yes_price_cents ELSE no_price_cents END)
                                / NULLIF(SUM(COALESCE(count_real,count)),0), MAX(created_at)
                            FROM fill_ledger WHERE order_id=?""", (f['order_id'],)).fetchone()
                        result = db.execute("""UPDATE quotes SET fill_size=?, fill_price_cents=?,
                            filled_at=?, status=CASE WHEN ? >= size_contracts THEN 'filled' ELSE status END
                            WHERE order_id=?""", (n, price, ts, n, f['order_id']))
                        if result.rowcount == 0:
                            continue  # order row may arrive after the fill
                        db.execute("INSERT OR IGNORE INTO fill_consumer_receipts VALUES (?,?,?)",
                                   (tid, consumer, datetime.now(timezone.utc).isoformat()))
                    quote_updates += result.rowcount
                    continue
                side = f['side']
                price = f['yes_price_cents'] if side == 'yes' else f['no_price_cents']
                if price is None:
                    continue
                count = f.get('count_real')
                count = f['count'] if count is None else count
                ts = datetime.fromisoformat(f['created_at'].replace('Z', '+00:00')).timestamp()
                args = dict(fill_id=tid, ticker=f['ticker'], side=side,
                            fill_price_c=price, fill_size=count, fill_ts=ts, db_path=db_path)
                if consumer == 'markout':
                    from monitor.markout_logger import ensure_schema, compute_markouts_for_fill
                    ensure_schema(db_path)
                    if not compute_markouts_for_fill(**args):
                        continue
                else:
                    from cross_venue.hedger import ensure_schema, decide, persist
                    ensure_schema(db_path)
                    persist(decide(**args), db_path=db_path)
                    # persist catches errors internally: confirm the effect exists.
                    with sqlite3.connect(db_path) as db:
                        if not db.execute('SELECT 1 FROM hedge_log WHERE fill_id=? AND kalshi_ticker=?',
                                          (tid, f['ticker'])).fetchone():
                            continue
                with sqlite3.connect(db_path, timeout=5) as db:
                    db.execute("INSERT OR IGNORE INTO fill_consumer_receipts VALUES (?,?,?)",
                               (tid, consumer, datetime.now(timezone.utc).isoformat()))
            except Exception as exc:
                _log.warning('Fill consumer %s pending for %s: %s', consumer, tid, exc)
    return quote_updates
