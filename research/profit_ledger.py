"""Immutable receipt ledger; decimal accounting; executable inventory marks."""
import hashlib
import json
import sqlite3
from decimal import Decimal

ZERO = Decimal('0')


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('non-finite amount')
    return result


def canonical(event):
    return json.dumps(event, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ProfitLedger:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS profit_events (
                mode TEXT, event_id TEXT, ts_ms INTEGER, payload TEXT NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(mode,event_id))''')

    def append(self, event):
        e = dict(event)
        for key in ('event_id', 'source', 'market', 'program_id'):
            if not isinstance(e.get(key), str) or not e[key]:
                raise ValueError(f'missing {key}')
        if e.get('mode') not in ('paper', 'live'):
            raise ValueError('explicit paper/live mode required')
        if type(e.get('ts_ms')) is not int or e['ts_ms'] < 0:
            raise ValueError('ts_ms must be a nonnegative integer')
        kind = e.get('kind')
        if kind in ('buy', 'sell', 'settlement'):
            if e.get('side') not in ('yes', 'no'):
                raise ValueError('invalid side')
            q, p, fee = (number(e[k]) for k in ('quantity', 'price_usd', 'fee_usd'))
            if q <= 0 or not ZERO <= p <= 1 or fee < 0:
                raise ValueError('invalid execution')
            if kind == 'settlement' and p not in (ZERO, Decimal(1)):
                raise ValueError('settlement payout must be zero or one')
        elif kind in ('reward_credit', 'reward_estimate', 'operating_cost', 'reserve'):
            if number(e['amount_usd']) < 0:
                raise ValueError('negative cash receipt')
        else:
            raise ValueError('unknown event kind')
        body = canonical(e)
        digest = hashlib.sha256(body.encode()).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT sha256 FROM profit_events WHERE mode=? AND event_id=?',
                             (e['mode'], e['event_id'])).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError('conflicting receipt identity')
                return False
            db.execute('INSERT INTO profit_events VALUES (?,?,?,?,?)',
                       (e['mode'], e['event_id'], e['ts_ms'], body, digest))
        return True

    def events(self, mode, asof_ms):
        if mode not in ('paper', 'live'):
            raise ValueError('invalid mode')
        with sqlite3.connect(self.path) as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM profit_events WHERE mode=? AND ts_ms<=? ORDER BY ts_ms,rowid',
                (mode, asof_ms))]

    def report(self, mode, asof_ms, books=None, *, max_book_age_ms=2000):
        """Recorded-event scope. Caller must supply complete history from flat.

        Books: market -> ts_ms, yes/no bids [[dollars,qty]], exit_fees_by_program.
        Exit fee is an explicit aggregate estimate for this program's open lots.
        Missing depth/fee/stale marks suppress the net-liquidation estimate.
        """
        if type(asof_ms) is not int or asof_ms < 0 or max_book_age_ms < 0:
            raise ValueError("invalid report time")
        books = books or {}
        groups = {}
        for e in self.events(mode, asof_ms):
            key = (e['market'], e['program_id'])
            g = groups.setdefault(key, dict(lots={'yes': [ZERO, ZERO], 'no': [ZERO, ZERO]},
                realized=ZERO, fees=ZERO, rewards=ZERO, estimates=ZERO, costs=ZERO,
                reserved=ZERO, capital_hours=ZERO, last=e['ts_ms']))
            committed = sum(v[1] for v in g['lots'].values()) + g['reserved']
            g['capital_hours'] += committed * Decimal(e['ts_ms'] - g['last']) / 3600000
            g['last'] = e['ts_ms']
            kind = e['kind']
            if kind in ('buy', 'sell', 'settlement'):
                qty, cost = g['lots'][e['side']]
                q, p = number(e['quantity']), number(e['price_usd'])
                g['fees'] += number(e['fee_usd'])
                if kind == 'buy':
                    g['lots'][e['side']] = [qty + q, cost + q * p]
                else:
                    if q > qty:
                        raise ValueError('sale exceeds recorded inventory; history incomplete')
                    removed = cost * q / qty
                    g['realized'] += q * p - removed
                    g['lots'][e['side']] = [qty - q, cost - removed]
            else:
                field = {'reward_credit': 'rewards', 'reward_estimate': 'estimates',
                         'operating_cost': 'costs', 'reserve': 'reserved'}[kind]
                if kind == 'reserve':
                    g[field] = number(e['amount_usd'])
                else:
                    g[field] += number(e['amount_usd'])
        result = []
        # Shared book depth is consumed across programs; don't value each against
        # the same liquidity. Attribution follows recorded group order.
        remaining_books = {}
        for market, book in books.items():
            remaining_books[market] = {side: [[number(p), number(q)] for p, q in
                sorted(book.get(side + '_bids', []), key=lambda v: number(v[0]), reverse=True)]
                for side in ('yes', 'no')}
            for levels in remaining_books[market].values():
                if any(not ZERO <= p <= 1 or q < 0 for p, q in levels):
                    raise ValueError('invalid book levels')
        for (market, program), g in groups.items():
            basis = sum(v[1] for v in g['lots'].values())
            open_qty = sum(v[0] for v in g['lots'].values())
            g['capital_hours'] += (basis + g['reserved']) * Decimal(asof_ms - g['last']) / 3600000
            mark, complete, reasons = ZERO, True, []
            book = books.get(market)
            if open_qty:
                if book is None or not 0 <= asof_ms - book['ts_ms'] <= max_book_age_ms:
                    complete, reasons = False, ['missing_or_stale_exit_book']
                else:
                    for side, (qty, _) in g['lots'].items():
                        left = qty
                        for level in remaining_books[market][side]:
                            take = min(left, level[1])
                            mark += take * level[0]
                            level[1] -= take
                            left -= take
                        if left:
                            complete = False
                            reasons.append('insufficient_' + side + '_exit_depth')
                    # Per-program fee estimates avoid reusing one fee across lots.
                    fee = book.get('exit_fees_by_program', {}).get(program)
                    if fee is None:
                        complete = False
                        reasons.append('missing_exit_fee_estimate')
                    else:
                        exit_fee = number(fee)
                        if exit_fee < 0:
                            raise ValueError('negative exit fee')
                        mark -= exit_fee
            cash_net = g['realized'] + g['rewards'] - g['fees'] - g['costs']
            result.append(dict(market=market, program_id=program,
                realized_trading_pnl_usd=str(g['realized']), paid_fees_usd=str(g['fees']),
                credited_rewards_usd=str(g['rewards']), estimated_rewards_usd=str(g['estimates']),
                operating_cost_usd=str(g['costs']), realized_net_usd=str(cash_net),
                inventory_cost_usd=str(basis), inventory={k: str(v[0]) for k,v in g['lots'].items()},
                net_if_liquidated_usd=str(cash_net + mark - basis) if complete else None,
                capital_dollar_hours=str(g['capital_hours']), valuation_complete=complete,
                blockers=reasons))
        return dict(mode=mode, asof_ms=asof_ms, scope='recorded_events_from_flat_only',
                    profitability_verified=False, markets=result)
