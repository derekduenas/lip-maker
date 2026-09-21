from decimal import Decimal as D
from research.maker_replay import replay,ReplayConfig
from tests.test_maker_research import book,trade
from tests.test_replay_rewards import program


def cfg():return ReplayConfig(policy='reward_inventory',latency_ms=0,max_mid_move_usd='.1')


def test_one_sided_fill_stops_heavy_side_and_completes_pair():
    r=replay([book(),trade(100),book(200),trade(300,side='no',price_usd='.5'),book(400)],cfg(),program())
    assert r['veto_counts']['inventory_pair_completion']==1
    assert r['paired_contracts']=='1'
    assert D(r['paired_hold_net_before_rewards_usd'])==D('.08')
    assert any(d['ts_ms']==200 and d['side']=='yes' and d['price_usd'] is None for d in r['decisions']) is False # already fully filled; no order to cancel


def test_hedge_ceiling_accounts_for_both_fees_and_margin():
    r=replay([book(),trade(100),book(200,no_bids=[['.59','2']])],cfg(),program())
    hedge=[d for d in r['decisions'] if d['ts_ms']==200 and d['side']=='no']
    assert hedge and D(hedge[0]['price_usd'])==D('.57')
