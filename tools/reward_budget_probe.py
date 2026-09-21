"""Bounded public-data reward probe. No credentials, order endpoints or fills inferred."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal as D, ROUND_DOWN
import json
from pathlib import Path
import time
import requests
from research.reward_optimizer import _side

BASE='https://api.elections.kalshi.com/trade-api/v2'


def stamp(s):
    return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()


def quote_frontier(p,m,raw,now,budget):
    if m['status'] not in ('active','open') or m.get('price_level_structure')!='linear_cent':
        return []
    if not stamp(p['start_date'])<=now<stamp(p['end_date']):
        return []
    sides={s:raw['orderbook_fp'][s+'_dollars'] for s in ('yes','no')}
    best={s:max((D(x) for x,q in sides[s] if D(q)>0),default=D(0)) for s in sides}
    if min(best.values())<=0 or sum(best.values())>=1:
        return []
    target=D(p['target_size_fp']);discount=D(p['discount_factor_bps'])/10000
    pool=D(p['period_reward'])/10000
    cap=pool if p.get('max_reward_per_account') is None else min(pool,D(p['max_reward_per_account'])/10000)
    period=D(str(stamp(p['end_date'])-stamp(p['start_date'])))
    remaining=D(str(stamp(p['end_date'])-now))
    rows=[]
    # Fixed before observing results: join best and one/two ticks back,
    # using 1/5/10/20 contracts and the largest pair affordable with cash reserve.
    for offset in (0,1,2):
        y,n=best['yes']-D('.01')*offset,best['no']-D('.01')*offset
        if min(y,n)<=0:
            continue
        maxq=int((budget-D('5'))/(y+n))
        for q in sorted(set([1,5,10,20,maxq])):
            if q<=0 or D(q)*(y+n)>budget-D('5'):
                continue
            ys,ycut,yref=_side(sides['yes'],y,D(q),target,discount,D('.01'))
            ns,ncut,nref=_side(sides['no'],n,D(q),target,discount,D('.01'))
            share=(ys+ns)/2 if ycut is not None and ncut is not None else D(0)
            if share<=0:
                continue
            for uptime in (D('.25'),D('.50'),D('1')):
                reward=min(cap,pool*share*uptime*remaining/period).quantize(D('.01'),rounding=ROUND_DOWN)
                payable=reward if reward>=1 else D(0)
                rows.append(dict(offset_ticks=offset,size=q,yes_price=str(y),no_price=str(n),capital_usd=str(D(q)*(y+n)),qualified_fraction_assumption=str(uptime),share=str(share),remaining_seconds=str(remaining),estimated_reward_usd=str(payable),maximum_total_cost_to_break_even_usd=str(payable),net_profit_usd=None,live_eligible=False))
    return sorted(rows,key=lambda r:D(r['estimated_reward_usd'])/D(r['capital_usd']),reverse=True)


def get(path,params=None):
    r=requests.get(BASE+path,params=params,timeout=15);r.raise_for_status();return r.json()


def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--output',required=True);a.add_argument('--budget',default='40');a.add_argument('--markets',type=int,default=8)
    args=a.parse_args();budget=D(args.budget)
    if not budget.is_finite() or budget<=5 or not 1<=args.markets<=12:
        a.error('budget must exceed reserve $5; markets 1..12')
    start=time.time();catalog=get('/incentive_programs',{'status':'active','type':'liquidity','limit':10000})
    if catalog.get('next_cursor'):
        raise ValueError('incomplete catalog; refusing selection')
    programs=[p for p in catalog['incentive_programs'] if stamp(p['start_date'])<=start<stamp(p['end_date']) and p.get('target_size_fp') and p.get('discount_factor_bps') and stamp(p['end_date'])-start>=600]
    programs.sort(key=lambda p:D(p['period_reward'])/D(str(stamp(p['end_date'])-stamp(p['start_date']))),reverse=True)
    selected=[];seen=set()
    for p in programs:
        family=p['market_ticker'].split('-')[0]
        if family in seen:continue
        seen.add(family);selected.append(p)
        if len(selected)==args.markets:break
    def observe(p):
        try:
            market=get('/markets/'+p['market_ticker'])['market']
            snapshots=[]
            for _ in range(2):
                before=time.time();raw=get('/markets/'+p['market_ticker']+'/orderbook');after=time.time()
                rows=quote_frontier(p,market,raw,after,budget)
                blocked=['request_latency_exceeds_2s'] if after-before>2 else []
                snapshots.append(dict(request_started=before,received=after,request_seconds=after-before,blocked_reasons=blocked,actionable=False,raw=raw,frontier=rows))
            return dict(program=p,market=market,snapshots=snapshots)
        except Exception as e:
            return dict(market=p['market_ticker'],error=type(e).__name__+': '+str(e))
    with ThreadPoolExecutor(max_workers=4) as ex: observations=list(ex.map(observe,selected))
    result=dict(status='PUBLIC_BOOK_PAPER_PROBE_ONLY',started=start,ended=time.time(),budget_usd=str(budget),reserve_usd='5',catalog_count=len(catalog['incentive_programs']),selection='Highest pool rate, at most one per series, minimum 10 minutes remaining',observations=observations,live_eligible=False,limitations=['Not a full-market ranking or sequenced tape replay.','All public depth treated as competing liquidity for a hypothetical fresh paper account.','Snapshots are not simultaneous; receive timestamps are not exchange snapshot timestamps.','No fills inferred; no positive net-profit estimate without cost evidence.','Uptime and unchanged depth/share until expiry are scenarios, not forecasts.','Quotes are mutually exclusive alternatives; their capital and rewards must not be summed.'])
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(status=result['status'],catalog=result['catalog_count'],markets=len(observations),errors=sum('error' in x for x in observations),output=args.output)))


if __name__=='__main__':main()
