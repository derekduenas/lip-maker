from copy import deepcopy
from decimal import Decimal
import pytest
from research.reward_optimizer import rank_quotes, _side
from engine.lip_scorer import _score_bids
from execution.kalshi_ws import BookLevel


def inputs():
    p=dict(id='p',market_ticker='m',incentive_type='liquidity',start_date='2026-09-21T00:00:00Z',end_date='2026-09-21T01:00:00Z',period_reward=1000000,discount_factor_bps=5000,target_size_fp='100',paid_out=False)
    b=dict(market='m',ts=1789948800,valid=True,status='open',contains_own_orders=False,yes_bids=[['.50','1'],['.49','99']],no_bids=[['.40','1'],['.39','99']])
    c=dict(id='a',yes_price='.49',no_price='.39',size='100',qualified_fraction='1',trading_pnl_usd='-2',fees_usd='1',operating_cost_usd='0',uncertainty_reserve_usd='1')
    return p,b,c


def run(p,b,c,**kw):
    return rank_quotes(p,b,[c],now=1789948800,horizon_seconds=3600,tick_usd='.01',**kw)


def test_reference_is_depth_based_and_better_prices_get_no_bonus():
    p,b,c=inputs();r=run(p,b,c)['ranked'][0]
    assert r['yes_reference']=='0.49'
    assert Decimal(r['modeled_share'])==Decimal('.5')
    assert Decimal(r['estimated_payable_reward_usd'])==50
    assert Decimal(r['conservative_scenario_net_usd'])==46
    ours,total=_score_bids([BookLevel(50,1),BookLevel(49,99)],[BookLevel(50,1)],49,.5,49)
    assert ours==1 and total==100


def test_cap_units_and_minimum():
    p,b,c=inputs();p['max_reward_per_account']=9999
    r=run(p,b,c)['ranked'][0]
    assert r['reward_before_minimum_usd']=='0.99'
    assert r['estimated_payable_reward_usd']=='0'
    assert not r['research_candidate']


def test_own_depth_changes_cutoff_and_reference():
    p,b,c=inputs();b['yes_bids']=[];b['no_bids']=[]
    r=run(p,b,c)['ranked'][0]
    assert r['qualified'] and r['modeled_share']=='1'


def test_exclusion_uptime_scales_reward_once():
    p,b,c=inputs();c['qualified_fraction']='.8'
    assert run(p,b,c)['ranked'][0]['estimated_payable_reward_usd']=='40.00'


@pytest.mark.parametrize('change',[{'valid':False},{'ts':1789948700},{'status':'closed'}])
def test_bad_book_blocks(change):
    p,b,c=inputs();b.update(change)
    assert run(p,b,c)['blocked_reasons']


def test_contract_and_own_depth_rejected():
    p,b,c=inputs();b['market']='other'
    with pytest.raises(ValueError):run(p,b,c)
    b['market']='m';b['contains_own_orders']=True
    with pytest.raises(ValueError):run(p,b,c)


def test_postonly_cross_not_candidate():
    p,b,c=inputs();c['yes_price']='.65';c['no_price']='.3'
    r=run(p,b,c)['ranked'][0]
    assert not r['post_only'] and not r['research_candidate']


def test_rank_by_net_capital_efficiency_and_reject_duplicate():
    p,b,c=inputs();other=dict(c,id='b',trading_pnl_usd='-100')
    r=rank_quotes(p,b,[other,c],now=b['ts'],horizon_seconds=3600,tick_usd='.01')
    assert r['ranked'][0]['id']=='a'
    with pytest.raises(ValueError):rank_quotes(p,b,[c,c],now=b['ts'],horizon_seconds=3600,tick_usd='.01')


def test_cost_forecast_cannot_extend_past_program():
    p,b,c=inputs()
    with pytest.raises(ValueError):
        rank_quotes(p,b,[c],now=b['ts'],horizon_seconds=3601,tick_usd='.01')


def test_crossed_public_book_blocks_even_non_crossing_proposal():
    p,b,c=inputs();b['yes_bids']=[['.7','100']];b['no_bids']=[['.4','100']]
    c.update(yes_price='.5',no_price='.2')
    assert run(p,b,c)['blocked_reasons']==['crossed_or_locked_book']


def test_nonqualifying_quotes_cannot_use_positive_pnl_to_pass():
    p,b,c=inputs();c.update(yes_price='.1',no_price='.1',trading_pnl_usd='100')
    r=run(p,b,c)['ranked'][0]
    assert r['qualified']  # Public snapshot is valid, but our levels earn nothing.
    assert r['modeled_share']=='0'
    assert not r['research_candidate']


def test_ineligible_high_forecast_does_not_outrank_valid_quote():
    p,b,c=inputs();bad=dict(c,id='bad',yes_price='.65',no_price='.3',trading_pnl_usd='10000')
    r=rank_quotes(p,b,[bad,c],now=b['ts'],horizon_seconds=3600,tick_usd='.01')
    assert r['ranked'][0]['id']=='a'


def test_empty_candidates_does_not_bypass_book_validation():
    p,b,c=inputs();b['yes_bids']=[['.50','-1']]
    with pytest.raises(ValueError):
        rank_quotes(p,b,[],now=b['ts'],horizon_seconds=3600,tick_usd='.01')


def test_zero_qualified_time_is_not_a_reward_candidate():
    p,b,c=inputs();c.update(qualified_fraction='0',trading_pnl_usd='100')
    assert not run(p,b,c)['ranked'][0]['research_candidate']
