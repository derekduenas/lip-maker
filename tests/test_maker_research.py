from decimal import Decimal as D
import pytest
from research.profit_ledger import ProfitLedger
from research.maker_replay import replay, compare, ReplayConfig
from research.market_evidence import markouts, evaluate_candidates


def receipt(**kw):
    return dict(dict(event_id='buy', source='fixture', market='M', program_id='P',
                     mode='paper', ts_ms=0, kind='buy', side='yes', quantity='2',
                     price_usd='.5', fee_usd='.02'), **kw)


def book(t=0, **kw):
    return dict(dict(event_id=f'b{t}', kind='book', market='M', ts_ms=t,
                     yes_bids=[['.4','2']], no_bids=[['.5','2']], valid=True), **kw)


def trade(t=300, **kw):
    return dict(dict(event_id=f't{t}',kind='trade',market='M',ts_ms=t,
                     side='yes',aggressor='sell',price_usd='.4',quantity='3'), **kw)


def test_receipt_idempotency_conflict_and_mode_isolation(tmp_path):
    l = ProfitLedger(tmp_path/'p.db')
    assert l.append(receipt())
    assert not l.append(receipt())
    with pytest.raises(ValueError): l.append(receipt(quantity='3'))
    assert l.append(receipt(mode='live'))
    assert len(l.events('paper',100)) == 1


def test_estimates_never_count_and_exit_costs_do(tmp_path):
    l = ProfitLedger(tmp_path/'p.db')
    l.append(receipt())
    l.append(receipt(event_id='estimate', kind='reward_estimate', amount_usd='100'))
    b = book(exit_fees_by_program={'P':'.03'})
    r = l.report('paper',0,{'M':b})['markets'][0]
    assert D(r['net_if_liquidated_usd']) == D('-.25')
    assert D(r['estimated_rewards_usd']) == 100
    assert not l.report('paper',0)['markets'][0]['valuation_complete']


@pytest.mark.parametrize('change', [{'exit_fees_by_program':{}}, {'ts_ms':-3000}, {'yes_bids':[['.4','1']]}])
def test_missing_exit_evidence_suppresses_net(tmp_path,change):
    l=ProfitLedger(tmp_path/'p.db'); l.append(receipt())
    b=book(exit_fees_by_program={'P':'0'}); b.update(change)
    assert l.report('paper',0,{'M':b})['markets'][0]['net_if_liquidated_usd'] is None


def test_realized_receipts_and_capital_hours(tmp_path):
    l=ProfitLedger(tmp_path/'p.db'); l.append(receipt())
    l.append(receipt(event_id='sell',kind='sell',ts_ms=3600000,price_usd='.6',fee_usd='.03'))
    l.append(receipt(event_id='reward',kind='reward_credit',ts_ms=3600000,amount_usd='.1'))
    r=l.report('paper',3600000)['markets'][0]
    assert D(r['net_if_liquidated_usd']) == D('.25')
    assert D(r['capital_dollar_hours']) == 1


def test_incomplete_history_rejected(tmp_path):
    l=ProfitLedger(tmp_path/'p.db'); l.append(receipt(kind='sell'))
    with pytest.raises(ValueError): l.report('paper',0)


def test_depth_not_reused_between_programs(tmp_path):
    l=ProfitLedger(tmp_path/'p.db'); l.append(receipt())
    l.append(receipt(event_id='second',program_id='Q'))
    r=l.report('paper',0,{'M':book(exit_fees_by_program={'P':'0','Q':'0'})})
    assert [x['valuation_complete'] for x in r['markets']] == [True,False]


def test_touch_alone_never_fills():
    assert not replay([book(),book(300)])['fills']


def test_latency_queue_and_fees():
    assert not replay([book(),trade(200)])['fills']
    assert not replay([book(),trade(quantity='2')])['fills']
    r=replay([book(),trade()])
    assert len(r['fills']) == 1
    assert D(r['net_before_rewards_usd']) == D('-.03')
    assert D(r['break_even_credited_reward_usd']) == D('.03')


def test_queue_stress_and_buy_aggressor():
    assert not replay([book(),trade()],ReplayConfig(queue_multiplier='2'))['fills']
    assert not replay([book(),trade(aggressor='buy')])['fills']


def test_old_order_can_fill_during_cancel_latency():
    r=replay([book(),book(300,yes_bids=[['.3','2']]),trade(400)])
    assert r['fills'][0]['price_usd'] == '0.4'


def test_stale_book_cancels_and_missing_exit_suppresses_profit():
    assert not replay([book(),trade(3000)])['fills']
    r=replay([book(),trade(),dict(event_id='g',kind='gap',market='M',ts_ms=400)])
    assert r['capture_gap'] and r['net_before_rewards_usd'] is None


def test_duplicate_trade_cannot_fill_twice():
    t=trade(quantity='2.5')
    r=replay([book(),t,t])
    assert r['inventory']['yes'] == '0.5'
    with pytest.raises(ValueError): replay([book(),t,dict(t,quantity='3')])


def test_chronology_episode_and_budget():
    with pytest.raises(ValueError): replay([book(500),trade(300)])
    with pytest.raises(ValueError): replay([book(),trade(episode_id='another')])
    assert not replay([book(),trade()],ReplayConfig(capital_usd='.1'))['fills']
    assert not compare([book(),trade()])['do_nothing']['fills']


def test_markout_horizon_missingness_and_gap():
    f=receipt(episode_id='E')
    labels=markouts([f],[book(1000),book(70000)])
    assert D(labels[0]['labels']['1000']['adverse_exit_cost_per_contract_usd']) == D('.1')
    assert labels[0]['labels']['60000']['adverse_exit_cost_per_contract_usd'] is None
    labels=markouts([f],[book(500,valid=False),book(1000)])
    assert labels[0]['labels']['1000']['adverse_exit_cost_per_contract_usd'] is None


def candidate(**kw):
    return dict(dict(market='M',underlying_event_id='NFL1',rules_verified=True,
                     reward_receipts_reconciled=True,evaluation_split='held_out',independent_episodes=30,
                     capital_usd='5',horizon_hours='1',reward_lower_bound_usd='.2',
                     trading_pnl_lower_bound_usd='-.1',operating_cost_usd='0',uncertainty_allowance_usd='.01'),**kw)


def test_evaluation_evidence_and_event_caps():
    r=evaluate_candidates([candidate(),candidate(market='N'),candidate(rules_verified=False)],'20','5')
    assert len(r['selected']) == 1 and len(r['rejected']) == 2
    assert not r['live_eligible']
    assert not evaluate_candidates([candidate(trading_pnl_lower_bound_usd='-1')],'20','5')['selected']
    with pytest.raises(ValueError): evaluate_candidates([candidate(operating_cost_usd='-1')],'20','5')


def test_markout_does_not_use_another_episode():
    f=receipt(episode_id='E')
    labels=markouts([f],[book(1000,episode_id='OTHER')])
    assert labels[0]['labels']['1000']['adverse_exit_cost_per_contract_usd'] is None


def test_invalid_deeper_level_not_hidden_by_sufficient_top_depth():
    with pytest.raises(ValueError):
        replay([book(yes_bids=[['.4','2'],['.3','-1']])])


def test_cli_round_trip_and_explicit_fee_requirement(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    events=tmp_path/'events.json'; events.write_text(json.dumps([receipt()]))
    db=tmp_path/'profit.db'
    def run(*args):
        return subprocess.run([sys.executable,'-m','tools.maker_research',*map(str,args)],cwd=root,capture_output=True,text=True)
    result=run('ingest','--db',db,'--events',events)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['inserted'] == 1
    result=run('report','--db',db,'--mode','paper','--asof-ms',0)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['markets'][0]['net_if_liquidated_usd'] is None
    events.write_text(json.dumps([book(),trade()]))
    cfg=tmp_path/'config.json'; cfg.write_text('{}')
    assert run('replay','--events',events,'--config',cfg).returncode != 0
    cfg.write_text(json.dumps(dict(maker_fee_per_contract_usd='.01',exit_fee_per_contract_usd='.02',latency_ms=250,queue_multiplier='1')))
    result=run('replay','--events',events,'--config',cfg)
    assert result.returncode == 0, result.stderr
    assert D(json.loads(result.stdout)['join_best']['net_before_rewards_usd']) == D('-.03')
