from decimal import Decimal as D
from tools.reward_budget_probe import quote_frontier
from tests.test_reward_optimizer import inputs


def test_budget_and_no_fabricated_profit():
    p,b,c=inputs()
    raw={'orderbook_fp':{'yes_dollars':b['yes_bids'],'no_dollars':b['no_bids']}}
    rows=quote_frontier(p,{'status':'active','price_level_structure':'linear_cent'},raw,b['ts'],D(40))
    assert rows
    assert all(D(r['capital_usd'])<=35 and r['net_profit_usd'] is None and not r['live_eligible'] for r in rows)
    assert all(D(r['maximum_total_cost_to_break_even_usd'])==D(r['estimated_reward_usd']) for r in rows)


def test_closed_and_unsupported_grid_skip():
    p,b,c=inputs()
    for m in ({'status':'closed','price_level_structure':'linear_cent'},{'status':'active','price_level_structure':'deci_cent'}):
        assert quote_frontier(p,m,{},b['ts'],D(40))==[]


def test_no_reward_claim_after_expiry():
    p,b,c=inputs()
    assert quote_frontier(p,{'status':'active','price_level_structure':'linear_cent'},{},b['ts']+3600,D(40))==[]
