import pytest
from research.compound_capital import capital_budget


def e(i,kind,**kw):return dict(event_id=str(i),ts_ms=i,mode='paper',kind=kind,market='m',**kw)


def test_paid_only_compounds_and_capacity_caps():
    r=capital_budget([e(1,'reward_estimate',amount_usd='800'),e(2,'reward_credit',amount_usd='10')],mode='paper')
    assert r['cash_usd']=='50' and r['new_capital_budget_usd']=='20'
    assert r['excluded_reward_estimates_usd']=='800'


def test_fills_and_losses_reduce_next_budget():
    events=[e(1,'buy',side='yes',quantity='10',price_usd='.5',fee_usd='.1'),e(2,'sell',side='yes',quantity='10',price_usd='.3',fee_usd='.1')]
    r=capital_budget(events,mode='paper')
    assert r['cash_usd']=='37.8' and r['new_capital_budget_usd']=='16.40'


def test_unsold_inventory_not_spendable():
    r=capital_budget([e(1,'buy',side='yes',quantity='20',price_usd='.5',fee_usd='0')],mode='paper')
    assert r['cash_usd']=='30.0' and len(r['open_inventory'])==1


def test_duplicates_not_compounded_twice():
    x=e(1,'reward_credit',amount_usd='5')
    assert capital_budget([x,x],mode='paper')['cash_usd']=='45'
    with pytest.raises(ValueError):capital_budget([x,dict(x,amount_usd='6')],mode='paper')


@pytest.mark.parametrize('events',[[e(1,'buy',side='yes',quantity='100',price_usd='.5',fee_usd='0')],[e(1,'sell',side='yes',quantity='1',price_usd='.5',fee_usd='0')],[dict(e(1,'reward_credit',amount_usd='5'),mode='live')]])
def test_invalid_funding_fails(events):
    with pytest.raises(ValueError):capital_budget(events,mode='paper')
