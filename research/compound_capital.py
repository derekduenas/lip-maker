"""Replay receipt-backed funding; estimates and unrealized gains cannot compound.

Starts with a flat account at opening_cash. This is research accounting, not an
exchange balance reconciliation or an order authorization service.
"""
from research.profit_ledger import number as D, canonical


def capital_budget(events, *, mode, opening_cash='40', reserve_cash='5', deployment_fraction='0.5', capacity_limit='20'):
    if mode not in ('paper','live'):
        raise ValueError('explicit mode required')
    cash,reserve,fraction,capacity=map(D,(opening_cash,reserve_cash,deployment_fraction,capacity_limit))
    if min(cash,reserve,capacity)<0 or not 0<=fraction<=1:
        raise ValueError('invalid capital limits')
    seen={};positions={};last=-1;credits=D(0);costs=D(0);estimated=D(0)
    for e in events:
        if e.get('mode')!=mode:
            raise ValueError('mixed account modes')
        key=e['event_id'];body=canonical(e)
        if key in seen:
            if seen[key]!=body:raise ValueError('conflicting receipt')
            continue
        if type(e['ts_ms']) is not int or e['ts_ms']<0 or e['ts_ms']<last:
            raise ValueError('unordered receipts')
        last=e['ts_ms'];seen[key]=body
        kind=e['kind']
        if kind in ('buy','sell','settlement'):
            side=e['side']
            if side not in ('yes','no'):raise ValueError('invalid side')
            k=(e['market'],side)
            q,p,fee=map(D,(e['quantity'],e['price_usd'],e['fee_usd']))
            if q<=0 or not 0<=p<=1 or fee<0:raise ValueError('invalid fill')
            if kind=='buy':
                cash-=q*p+fee;positions[k]=positions.get(k,D(0))+q
            else:
                if q>positions.get(k,D(0)):raise ValueError('sale exceeds inventory; opening account must be flat')
                positions[k]-=q;cash+=q*p-fee
            costs+=fee
        elif kind in ('reward_credit','operating_cost','reward_estimate'):
            amount=D(e['amount_usd'])
            if amount<0:raise ValueError('negative receipt')
            if kind=='reward_credit':cash+=amount;credits+=amount
            elif kind=='operating_cost':cash-=amount;costs+=amount
            else:estimated+=amount
        else:
            raise ValueError('unsupported receipt; open-order reservations require separate reconciliation')
        if cash<0:raise ValueError('receipts overspend available cash')
    deployable=min(max(D(0),cash-reserve)*fraction,capacity)
    return dict(status='RECEIPT_CAPITAL_REPLAY_ONLY',mode=mode,cash_usd=str(cash),paid_rewards_usd=str(credits),excluded_reward_estimates_usd=str(estimated),fees_and_operating_cost_usd=str(costs),new_capital_budget_usd=str(deployable),open_inventory=[dict(market=k[0],side=k[1],quantity=str(q)) for k,q in positions.items() if q],live_eligible=False,limitations=['Opening account must be flat; external transfers are unsupported.','Source receipts must be reconciled; this function does not authenticate them.','Open order reservations and exchange collateral rules are not included; output cannot authorize orders.','Capacity limit and deployment fraction are explicit research controls, not validated profitable allocations.'])
