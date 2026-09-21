from decimal import Decimal as D
from research.maker_replay import replay,ReplayConfig
from tests.test_maker_research import book,trade


def test_matched_pair_has_terminal_value_without_double_exit_cost():
    events=[book(),book(300),trade(400),trade(500,side='no',price_usd='.5'),book(600)]
    r=replay(events,ReplayConfig())
    assert r['paired_contracts']=='1'
    assert D(r['paired_hold_net_before_rewards_usd'])==D('.08')
    assert D(r['net_before_rewards_usd'])==D('-.06')
    assert r['spent_usd']=='0.9'  # no fictional reinvestable collateral added


def test_unmatched_inventory_still_requires_observed_exit_book():
    r=replay([book(),book(300),trade(400)],ReplayConfig())
    assert r['paired_hold_net_before_rewards_usd'] is None


def test_balanced_pair_can_be_valued_without_fresh_exit_book():
    r=replay([book(),book(300),trade(400),trade(500,side='no',price_usd='.5')],ReplayConfig())
    assert r['net_before_rewards_usd'] is None
    assert D(r['paired_hold_net_before_rewards_usd'])==D('.08')
