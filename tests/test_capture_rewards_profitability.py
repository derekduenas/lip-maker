import asyncio
import csv
import json
import sqlite3
from decimal import Decimal as D
import pytest
from research.venue_capture import normalize, capture, export_capture, digest
from research.reward_reconciliation import reconcile_rewards, REQUIRED
from research.profitability import attack_profitability
from research.profit_ledger import ProfitLedger
from tests.test_maker_research import receipt, book, trade, candidate
from research.market_evidence import evaluate_candidates
from research.maker_replay import replay


def raw(kind, seq, msg, sid=1, received=1000):
    return dict(received_ms=received,message=dict(type=kind,sid=sid,seq=seq,msg=msg))


def snapshot():
    return raw('orderbook_snapshot',1,dict(market_ticker='M',yes_dollars_fp=[['.4','2']],no_dollars_fp=[['.5','2']]))


def public_trade(**kw):
    return raw('trade',1,dict(dict(market_ticker='M',trade_id='T',yes_price_dollars='.4',no_price_dollars='.6',count_fp='3',taker_outcome_side='no',taker_book_side='ask',is_block_trade=False,ts_ms=999),**kw),sid=2)


def test_unified_price_and_canonical_direction():
    s=snapshot();s['message']['msg']['no_dollars_fp']=[['.6','2']]
    events=normalize([s,public_trade()],'M','E')
    assert events[0]['no_bids']==[['0.4','2']]
    assert events[1]['side']=='yes' and events[1]['price_usd']=='0.4'
    t=public_trade(taker_outcome_side='yes',taker_book_side='bid')
    assert normalize([t],'M','E')[0]['side']=='no'
    assert normalize([t],'M','E')[0]['price_usd']=='0.6'


def test_delta_sequence_and_precision():
    delta=raw('orderbook_delta',2,dict(market_ticker='M',side='no',price_dollars='.505',delta_fp='1.5'))
    events=normalize([snapshot(),delta],'M','E')
    assert ['0.495','1.5'] in events[-1]['no_bids']
    delta['message']['seq']=3
    with pytest.raises(ValueError,match='sequence'): normalize([snapshot(),delta],'M','E')


@pytest.mark.parametrize('change',[{'is_block_trade':True},{'taker_outcome_side':'invalid'},{'taker_book_side':'bid'},{'count_fp':'-1'},{'no_price_dollars':'.7'},{'ts_ms':2000}])
def test_bad_trade_rejected(change):
    with pytest.raises(ValueError): normalize([public_trade(**change)],'M','E')


def test_delayed_trade_cannot_fill_order_not_yet_active():
    r=replay([book(),trade(exchange_ts_ms=100)])
    assert not r['fills']


def test_invalid_book_and_false_string_no_longer_accepted(tmp_path):
    l=ProfitLedger(tmp_path/'p.db');l.append(receipt())
    r=l.report('paper',0,{'M':book(valid=False,exit_fees_by_program={'P':'0'})})
    assert r['markets'][0]['net_if_liquidated_usd'] is None
    assert not evaluate_candidates([candidate(rules_verified='false')],'10','10')['selected']
    with pytest.raises(ValueError): evaluate_candidates([candidate(),candidate()],'10','10')


def statement(path, rows):
    with path.open('w') as f:
        w=csv.DictWriter(f,fieldnames=REQUIRED);w.writeheader();w.writerows(rows)


def credit(**kw):
    return dict(dict(credit_id='C1',market='M',program_id='P',ts_ms='1000',amount_usd='1.00',account_id='A',status='posted',kind='liquidity_reward'),**kw)


def reconcile(db,path,total='1'):
    return reconcile_rewards(db,path,{k:k for k in REQUIRED},'A',total)


def test_rewards_reexport_idempotent_and_estimates_separate(tmp_path):
    db=tmp_path/'p.db';path=tmp_path/'s.csv';statement(path,[credit()])
    ledger=ProfitLedger(db);ledger.append(receipt(event_id='est',kind='reward_estimate',mode='live',amount_usd='3'))
    assert reconcile(db,path)['inserted']==1
    statement(path,[credit(amount_usd='1.0')])
    r=reconcile(db,path)
    assert r['inserted']==0 and D(r['attribution'][0]['credited_minus_estimated_usd'])==-2


@pytest.mark.parametrize('row,total',[(credit(status='pending'),'1'),(credit(account_id='B'),'1'),(credit(kind='pool_paid_out'),'1'),(credit(amount_usd='-1'),'-1'),(credit(),'2')])
def test_bad_statement_no_credits(tmp_path,row,total):
    db=tmp_path/'p.db';path=tmp_path/'s.csv';statement(path,[row])
    with pytest.raises(ValueError): reconcile(db,path,total)
    assert not ProfitLedger(db).events('live',2000)


def test_conflicting_batch_rolls_back_all_new_credits(tmp_path):
    db=tmp_path/'p.db';path=tmp_path/'s.csv';statement(path,[credit()]);reconcile(db,path)
    statement(path,[credit(credit_id='C2'),credit(amount_usd='2')])
    with pytest.raises(ValueError): reconcile(db,path,'3')
    assert len(ProfitLedger(db).events('live',2000))==1


def test_capture_end_to_end_and_integrity(monkeypatch,tmp_path):
    from execution import kalshi_ws
    messages=[dict(type='subscribed',msg=dict(channel='orderbook_delta')),
              dict(type='subscribed',msg=dict(channel='trade')),snapshot()['message'],public_trade()['message']]
    class Fake:
        def __init__(self): self._ws=self;self.sent=[]
        async def connect(self): pass
        async def close(self): pass
        async def send(self,s): self.sent.append(json.loads(s))
        async def recv(self):
            if messages:return json.dumps(messages.pop(0))
            await asyncio.sleep(1)
    fake=Fake();monkeypatch.setattr(kalshi_ws,'KalshiWS',lambda:fake)
    path=tmp_path/'capture.jsonl'
    result=asyncio.run(capture(path,'M',.01))
    assert result['complete']
    assert fake.sent[0]['params']['use_yes_price'] is True
    assert all(m['cmd']=='subscribe' for m in fake.sent)
    assert len(export_capture(path,'E'))==2
    path.write_text(path.read_text().replace('0.4','0.3')+'{}\n')
    with pytest.raises((ValueError,KeyError)):export_capture(path,'E')


def test_capture_failure_manifest_is_not_eligible(monkeypatch,tmp_path):
    from execution import kalshi_ws
    def failed():raise FileNotFoundError('no key')
    monkeypatch.setattr(kalshi_ws,'KalshiWS',failed)
    path=tmp_path/'capture.jsonl'
    result=asyncio.run(capture(path,'M',.01))
    assert result['status']=='BLOCKED' and not result['complete']
    with pytest.raises(ValueError): export_capture(path,'E')


def episode(name,offset,event):
    events=[book(offset),trade(offset+300),book(offset+400)]
    for e in events:e['episode_id']=name
    return dict(episode_id=name,underlying_event_id=event,events=events)


SCENARIOS=[dict(maker_fee_per_contract_usd='.01',exit_fee_per_contract_usd='.02',latency_ms=250,queue_multiplier='1')]


def test_attack_rejects_cost_loss_and_leakage():
    episodes=[episode('dev',0,'GAME1'),episode('test',2000,'GAME2')]
    r=attack_profitability(episodes,SCENARIOS,1000)
    assert r['selected_from_development'] is None
    assert D(r['held_out']['join_best']['mean_break_even_reward_usd'])==D('.03')
    episodes[1]['underlying_event_id']='GAME1'
    with pytest.raises(ValueError,match='leaks'): attack_profitability(episodes,SCENARIOS,1000)


def test_attack_requires_both_splits_and_no_gaps():
    r=attack_profitability([],SCENARIOS,1000)
    assert 'both_chronological_splits_required' in r['blockers']
    episodes=[episode('dev',0,'GAME1'),episode('test',2000,'GAME2')]
    episodes[1]['events'].append(dict(event_id='gap',kind='gap',market='M',episode_id='test',ts_ms=2500))
    r=attack_profitability(episodes,SCENARIOS,1000)
    assert r['selected_from_development'] is None and r['blockers']


def test_signatures_match_documented_digest_length():
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from execution.kalshi_auth import KalshiClient
    from execution.kalshi_ws import KalshiWS
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    client=KalshiClient.__new__(KalshiClient);client._private_key=key;client.api_key='test'
    ws=KalshiWS.__new__(KalshiWS);ws._private_key=key;ws.api_key='test'
    for headers,path in [(client._sign('GET','/portfolio/fills?limit=1'),'/trade-api/v2/portfolio/fills'),
                         (ws._build_auth_headers(),'/trade-api/ws/v2')]:
        payload=(headers['KALSHI-ACCESS-TIMESTAMP']+'GET'+path).encode()
        key.public_key().verify(base64.b64decode(headers['KALSHI-ACCESS-SIGNATURE']),payload,
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),hashes.SHA256())


def test_other_account_cannot_mix_into_reconciled_database(tmp_path):
    db=tmp_path/'p.db';path=tmp_path/'s.csv';statement(path,[credit()]);reconcile(db,path)
    statement(path,[credit(account_id='B')])
    with pytest.raises(ValueError,match='separate profit database'):
        reconcile_rewards(db,path,{k:k for k in REQUIRED},'B','1')
    assert len(ProfitLedger(db).events('live',2000))==1


def test_exit_cannot_reuse_book_observed_before_trade():
    r=replay([book(),trade()])
    assert len(r['fills'])==1
    assert not r['liquidation_complete']
    assert r['net_before_rewards_usd'] is None
