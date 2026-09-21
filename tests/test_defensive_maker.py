from dataclasses import replace
from decimal import Decimal as D
import pytest
from research.maker_replay import ReplayConfig,replay,compare_challenger
from tests.test_maker_research import book,trade


def cfg(**kw):
    return replace(ReplayConfig(),policy='defensive_maker',qualification_target='3',**kw)


def deep(t=0,**kw):
    return book(t,yes_bids=[['.4','2'],['.39','2']],no_bids=[['.5','2'],['.49','2']],**kw)


def test_quotes_inside_cutoff_and_one_tick_back():
    r=replay([deep(),deep(300)],cfg())
    assert [x['price_usd'] for x in r['decisions']]==['0.39','0.49']


def test_refuses_target_unreachable_and_cutoff_above_price():
    assert not replay([book()],cfg())['decisions']
    assert not replay([deep()],replace(cfg(),qualification_target='2'))['decisions']


def test_movement_veto_cancels_and_does_not_use_future_data():
    start=[deep(),deep(300)]
    changed=book(400,yes_bids=[['.5','2'],['.49','2']],no_bids=[['.4','2'],['.39','2']])
    a=replay(start,cfg());b=replay(start+[changed],cfg())
    assert b['decisions'][:len(a['decisions'])]==a['decisions']
    assert b['veto_counts']['rapid_mid_movement']==1
    assert all(x['price_usd'] is None for x in b['decisions'][-2:])


def test_inventory_limit_accounts_for_existing_fills():
    events=[deep(),deep(300),trade(400,price_usd='.39',quantity='5'),deep(500)]
    r=replay(events,cfg(max_net_contracts='1'))
    assert D(r['inventory']['yes'])==1
    assert r['veto_counts']['inventory_limit']==1
    assert all(x['price_usd'] is None for x in r['decisions'][-1:])


def test_program_window_blocks_placement_and_late_activation():
    r=replay([deep(),deep(300)],cfg(program_start_ms=1000,program_end_ms=2000))
    assert not r['decisions']
    r=replay([deep(),trade(300,price_usd='.39',quantity='10')],cfg(program_end_ms=200))
    assert not r['fills']


def test_comparison_preserves_three_baselines():
    r=compare_challenger([deep(),deep(300)],cfg())
    assert set(r)=={'do_nothing','join_best','spread_guard','defensive_maker'}
    assert all(x['live_eligible'] is False for x in r.values())


@pytest.mark.parametrize('change',[{'tick_usd':'0'},{'quote_offset_ticks':-1},{'max_net_contracts':'0'},{'program_start_ms':500,'program_end_ms':100}])
def test_invalid_parameters_fail(change):
    with pytest.raises(ValueError):replay([],cfg(**change))


def test_expiry_cancels_without_another_book_after_latency():
    events=[deep(),deep(300),trade(760,price_usd='.39',quantity='10')]
    assert not replay(events,cfg(program_end_ms=500))['fills']
    # An order remains exposed while cancellation is in flight.
    events[-1]=trade(600,price_usd='.39',quantity='10')
    assert replay(events,cfg(program_end_ms=500))['fills']
