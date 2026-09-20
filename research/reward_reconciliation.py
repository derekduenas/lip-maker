"""Import posted reward statement rows atomically, retaining source hashes.

This is an explicit CSV adapter, not an invented Kalshi reward API. Mapping
must name the actual statement columns. Pool paid_out is never a receipt.
"""
import csv
import hashlib
import io
import sqlite3
from pathlib import Path
from research.profit_ledger import ProfitLedger, number, canonical

REQUIRED=('credit_id','market','program_id','ts_ms','amount_usd','account_id','status','kind')


def reconcile_rewards(db_path, statement_path, mapping, account_id, expected_total_usd):
    if not isinstance(account_id,str) or not account_id or not set(REQUIRED)<=mapping.keys():
        raise ValueError('account and explicit statement column mapping required')
    if len({mapping[k] for k in REQUIRED})!=len(REQUIRED):
        raise ValueError('statement column mapping is ambiguous')
    raw=Path(statement_path).read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)) or not set(mapping.values())<=set(reader.fieldnames):
        raise ValueError('statement columns missing')
    total=number(0); events=[]; seen=set()
    for row in reader:
        r={key:row[column] for key,column in mapping.items()}
        if any(not isinstance(r[k],str) or not r[k].strip() for k in REQUIRED):
            raise ValueError('blank statement field')
        if r['account_id']!=account_id or r['status']!='posted' or r['kind']!='liquidity_reward':
            raise ValueError('statement must contain posted liquidity rewards for the selected account only')
        if r['credit_id'] in seen:
            raise ValueError('duplicate credit within statement')
        seen.add(r['credit_id'])
        amount=number(r['amount_usd'])
        if amount<0 or not r['ts_ms'].isdigit():
            raise ValueError('unsupported reversal or invalid timestamp')
        total+=amount
        # A re-export with different formatting maps to the same economic ID.
        identity=hashlib.sha256(canonical([account_id,r['credit_id']]).encode()).hexdigest()
        events.append(dict(event_id='reward:'+identity,source='statement:'+account_id,
            market=r['market'],program_id=r['program_id'],mode='live',kind='reward_credit',
            ts_ms=int(r['ts_ms']),amount_usd=str(amount.normalize())))
    if total!=number(expected_total_usd):
        raise ValueError('statement total does not match independent declared control total')
    ledger=ProfitLedger(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS reward_statement_sources (
            sha256 TEXT, account_id TEXT, mapping TEXT, total_usd TEXT,
            receipt_count INTEGER, PRIMARY KEY(sha256,account_id))''')
        accounts={r[0] for r in db.execute('SELECT DISTINCT account_id FROM reward_statement_sources')}
        if accounts and accounts!={account_id}:
            raise ValueError('use a separate profit database per account')
        metadata=(sha,account_id,canonical(mapping),str(total.normalize()),len(events))
        old=db.execute('SELECT * FROM reward_statement_sources WHERE sha256=? AND account_id=?',metadata[:2]).fetchone()
        if old and old!=metadata:
            raise ValueError('statement remapped after prior reconciliation')
        inserted=sum(ledger.append(e,_connection=db) for e in events)
        db.execute('INSERT OR IGNORE INTO reward_statement_sources VALUES (?,?,?,?,?)',metadata)
    cutoff=max((e['ts_ms'] for e in events),default=0)
    groups={}
    for e in ledger.events('live',cutoff):
        if e['kind'] not in ('reward_credit','reward_estimate'):
            continue
        key=(e['market'],e['program_id'])
        group=groups.setdefault(key,{'reward_credit':number(0),'reward_estimate':number(0)})
        group[e['kind']]+=number(e['amount_usd'])
    attribution=[]
    for key in sorted({(e['market'],e['program_id']) for e in events}):
        row=groups[key]
        attribution.append(dict(market=key[0],program_id=key[1],asof_ms=cutoff,
            ledger_credited_usd=str(row['reward_credit']),ledger_estimated_usd=str(row['reward_estimate']),
            credited_minus_estimated_usd=str(row['reward_credit']-row['reward_estimate'])))
    return dict(status='STATEMENT_ROWS_RECONCILED',source_sha256=sha,rows=len(events),inserted=inserted,
                statement_total_usd=str(total),attribution=attribution,live_eligible=False,
                limitations=['CSV authenticity and coverage require independent account statement verification.',
                             'Pool paid_out is not account payment; no API credit was inferred.',
                             'Reversals require an explicit correction workflow and are rejected.'])
