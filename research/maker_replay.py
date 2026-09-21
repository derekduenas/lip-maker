"""Chronological, single-episode buy-maker simulation with explicit queue assumptions.

A book touch never fills. Only recorded sell-aggressor trades consume queue.
Cancellation/replacement has latency; old orders can fill during that latency.
No rewards are fabricated: report break-even credited reward required instead.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
from research.profit_ledger import number
from research.market_evidence import exit_value


@dataclass(frozen=True)
class ReplayConfig:
    size: str = '1'
    capital_usd: str = '10'
    latency_ms: int = 250
    stale_ms: int = 2000
    maker_fee_per_contract_usd: str = '0.01'
    exit_fee_per_contract_usd: str = '0.02'
    operating_cost_usd: str = '0'
    # These are explicit stress assumptions, NOT the exchange fee schedule.
    queue_multiplier: str = '1'
    policy: str = 'join_best'
    max_spread_usd: str = '0.10'
    # Defensive challenger: explicit research limits, not venue entitlements.
    tick_usd: str = '0.01'
    quote_offset_ticks: int = 1
    max_inventory_contracts: str = '4'
    max_net_contracts: str = '2'
    movement_window_ms: int = 5000
    max_mid_move_usd: str = '0.03'
    qualification_target: str = '300'
    program_start_ms: int | None = None
    program_end_ms: int | None = None


def replay(events, config=ReplayConfig(), reward_program=None):
    cfg = asdict(config)
    size, budget = number(config.size), number(config.capital_usd)
    maker_fee, exit_fee = number(config.maker_fee_per_contract_usd), number(config.exit_fee_per_contract_usd)
    operating, queue_mult = number(config.operating_cost_usd), number(config.queue_multiplier)
    if size <= 0 or budget < 0 or min(maker_fee,exit_fee,operating) < 0 or queue_mult < 1:
        raise ValueError('invalid replay economics')
    if config.latency_ms < 0 or config.stale_ms <= 0 or config.policy not in ('join_best','spread_guard','do_nothing','defensive_maker'):
        raise ValueError('invalid policy/timing')
    if not 0 <= number(config.max_spread_usd) <= 1:
        raise ValueError("invalid spread threshold")
    tick=number(config.tick_usd)
    gross_cap,net_cap=number(config.max_inventory_contracts),number(config.max_net_contracts)
    target=number(config.qualification_target)
    if not 0<tick<=1 or min(gross_cap,net_cap,target)<=0 or number(config.max_mid_move_usd)<0:
        raise ValueError('invalid defensive limits')
    if type(config.quote_offset_ticks) is not int or config.quote_offset_ticks<0 or config.movement_window_ms<=0:
        raise ValueError('invalid defensive timing/offset')
    for value in (config.program_start_ms,config.program_end_ms):
        if value is not None and (type(value) is not int or value<0):
            raise ValueError('invalid program window')
    if config.program_start_ms is not None and config.program_end_ms is not None and config.program_start_ms>=config.program_end_ms:
        raise ValueError('inverted program window')
    history=[]
    vetoes={}
    def veto(reason):
        vetoes[reason]=vetoes.get(reason,0)+1
    def in_window(now):
        return (config.program_start_ms is None or now>=config.program_start_ms) and (config.program_end_ms is None or now<config.program_end_ms)
    seen, timeline, last = {}, [], -1
    episodes = set()
    markets = set()
    for e in events:
        body = json.dumps(e, sort_keys=True, allow_nan=False)
        eid = e['event_id']
        if eid in seen:
            if seen[eid] != body:
                raise ValueError('conflicting event identity')
            continue
        if type(e['ts_ms']) is not int or e['ts_ms'] < last:
            raise ValueError('events must be chronological integer timestamps')
        seen[eid], last = body, e['ts_ms']
        if e['kind'] not in ('book','trade','gap'):
            raise ValueError('unknown replay event')
        markets.add(e['market'])
        episodes.add(e.get('episode_id', 'episode'))
        timeline.append(e)
    if len(markets) > 1 or len(episodes) > 1:
        raise ValueError('replay one market episode at a time')
    reward_total = number(0)
    reward_seconds = number(0)
    reward_start = reward_end = reward_pool = reward_cap = None
    if reward_program is not None:
        from research.reward_optimizer import _seconds
        if markets != {reward_program['market_ticker']} or reward_program['incentive_type'] != 'liquidity':
            raise ValueError('reward program does not match replay')
        reward_start = _seconds(reward_program['start_date']) * 1000
        reward_end = _seconds(reward_program['end_date']) * 1000
        reward_pool = number(reward_program['period_reward']) / 10000
        reward_cap = reward_pool if reward_program.get('max_reward_per_account') is None else min(reward_pool, number(reward_program['max_reward_per_account']) / 10000)
        if reward_end <= reward_start or reward_pool <= 0 or reward_cap < 0:
            raise ValueError('invalid reward economics')
        if not 0 < number(reward_program['discount_factor_bps']) <= 10000 or number(reward_program['target_size_fp']) <= 0:
            raise ValueError('invalid reward scoring parameters')
    book = None
    orders, pending = {}, {}
    positions = {'yes': number(0), 'no': number(0)}
    spent = fees = number(0)
    fills, decisions = [], []
    had_gap = False
    exit_book_observed = True

    def fresh(now):
        return book is not None and book.get('valid', True) and 0 <= now-book['ts_ms'] <= config.stale_ms

    def bid(side):
        return max((number(p) for p,q in book.get(side+'_bids', []) if number(q)>0), default=None)

    def advance(now):
        # Book silence triggers cancellation after stale threshold + latency.
        if book is not None and now > book['ts_ms'] + config.stale_ms + config.latency_ms:
            orders.clear()
            pending.clear()
        if config.program_end_ms is not None and now >= config.program_end_ms + config.latency_ms:
            orders.clear()
            pending.clear()
        for side in list(pending):
            due, price = pending[side]
            if due > now:
                continue
            orders.pop(side, None)
            pending.pop(side)
            if price is None or not fresh(now) or not in_window(now):
                continue
            other = bid('no' if side == 'yes' else 'yes')
            if other is None or price + other >= 1:
                continue  # post-only reject at simulated activation
            if config.policy=='defensive_maker':
                gross=sum(positions.values())+sum(o['remaining'] for o in orders.values())+size
                net=positions[side]-positions['no' if side=='yes' else 'yes']+size
                if gross>gross_cap or net>net_cap:
                    veto('activation_inventory_limit')
                    continue
            reserved = sum(o['remaining']*(o['price']+maker_fee) for o in orders.values())
            if spent + fees + reserved + size*(price+maker_fee) > budget:
                continue
            ahead = sum(number(q) for p,q in book.get(side+'_bids', []) if number(p)>=price)
            orders[side] = dict(price=price, remaining=size, ahead=ahead*queue_mult, activated_ms=now)

    previous_time = None
    def accrue_until(now):
        nonlocal reward_total, reward_seconds
        if reward_program is None or previous_time is None or book is None or not book.get('valid', True):
            return
        from research.reward_optimizer import _side
        # Stop at the earliest state deadline. Unobserved activation segments
        # are not credited retroactively when the next market event arrives.
        left = max(number(previous_time), reward_start)
        right = min(number(now), reward_end, number(book['ts_ms'] + config.stale_ms))
        if pending:
            right = min(right, number(min(due for due, _ in pending.values())))
        if right <= left:
            return
        y, n = bid('yes'), bid('no')
        if y is None or n is None or y+n >= 1:
            return
        shares = []
        for side in ('yes', 'no'):
            order = orders.get(side)
            price = order['price'] if order else bid(side)
            quantity = order['remaining'] if order else number(0)
            share, cutoff, _ = _side(book[side+'_bids'], price, quantity,
                number(reward_program['target_size_fp']), number(reward_program['discount_factor_bps'])/10000, tick)
            if cutoff is None:
                return
            shares.append(share)
        share = sum(shares)/2
        reward_total += reward_pool * share * (right-left)/(reward_end-reward_start)
        if share > 0:
            reward_seconds += (right-left)/1000

    for e in timeline:
        now = e['ts_ms']
        accrue_until(now)
        previous_time = now
        advance(now)
        if e['kind'] == 'gap':
            had_gap = True
            history.clear()
            book = None
            pending = {s:(now+config.latency_ms,None) for s in orders}
            continue
        if e['kind'] == 'book':
            if type(e.get('valid', True)) is not bool:
                raise ValueError('book validity must be boolean')
            # Validate all levels, including crossed/empty detection below.
            for side in positions:
                exit_value(e, side, size)
            book = e
            exit_book_observed = True
            if not e.get('valid',True):
                had_gap = True
            y,n = bid('yes'), bid('no')
            allowed = fresh(now) and in_window(now) and y is not None and n is not None and y+n<1
            if config.policy == 'spread_guard' and allowed:
                allowed = 1-y-n <= number(config.max_spread_usd)
            desired_prices={side:bid(side) if allowed else None for side in positions}
            if config.policy=='defensive_maker' and allowed:
                mid=(y+1-n)/2
                history=[(t,m) for t,m in history if now-t<=config.movement_window_ms]
                history.append((now,mid))
                if max(m for _,m in history)-min(m for _,m in history)>number(config.max_mid_move_usd):
                    allowed=False;veto('rapid_mid_movement')
                if 1-y-n>number(config.max_spread_usd):
                    allowed=False;veto('wide_spread')
                # Require capacity for both intended bids, even if only one fills.
                if sum(positions.values())+2*size>gross_cap or abs(positions['yes']-positions['no'])+size>net_cap:
                    allowed=False;veto('inventory_limit')
                for side in positions:
                    price=((bid(side)-config.quote_offset_ticks*tick)//tick)*tick
                    cumulative=number(0);cutoff=None
                    for p,q in sorted(book.get(side+'_bids',[]),key=lambda x:number(x[0]),reverse=True):
                        cumulative+=number(q)
                        if cumulative>=target:
                            cutoff=number(p);break
                    if cutoff is None or price<cutoff or price<=0:
                        allowed=False;veto('outside_modeled_qualification')
                    desired_prices[side]=price
            if config.policy == 'do_nothing':
                allowed = False
            for side in positions:
                desired = desired_prices[side] if allowed else None
                if side in pending:
                    continue  # no overlapping cancel/replace commands
                if side in orders and orders[side]['price'] == desired:
                    continue  # retain queue position
                if desired is not None or side in orders:
                    pending[side] = (now+config.latency_ms, desired)
                    decisions.append(dict(ts_ms=now, side=side,
                                          price_usd=str(desired) if desired is not None else None))
            advance(now)
        elif e['kind'] == 'trade':
            # A trade may have consumed the displayed exit depth. Require a
            # subsequent book observation before marking open inventory.
            exit_book_observed = False
            # Explicit aggressor side is required; never guess from price alone.
            if e.get('aggressor') not in ('buy','sell') or e.get('side') not in positions:
                raise ValueError('trade needs token side and aggressor')
            p, q = number(e['price_usd']), number(e['quantity'])
            if q <= 0 or not 0 <= p <= 1:
                raise ValueError('invalid trade')
            o = orders.get(e['side'])
            if not o or e['aggressor'] != 'sell' or p > o['price'] or e.get('exchange_ts_ms', now) < o['activated_ms']:
                continue
            consumed = min(q, o['ahead'])
            o['ahead'] -= consumed
            q -= consumed
            take = min(q, o['remaining'])
            if take:
                o['remaining'] -= take
                positions[e['side']] += take
                spent += take*o['price']
                fees += take*maker_fee
                fills.append(dict(event_id='replay:'+e['event_id'], ts_ms=now,
                                  market=e['market'], episode_id=e.get('episode_id', 'episode'),
                                  side=e['side'], price_usd=str(o['price']), quantity=str(take)))
                if o['remaining']==0:
                    orders.pop(e['side'])
    end = timeline[-1]['ts_ms'] if timeline else 0
    proceeds, complete = number(0), True
    for side, q in positions.items():
        if not q:
            continue
        value = exit_value(book, side, q) if fresh(end) and exit_book_observed else None
        if value is None:
            complete = False
        else:
            proceeds += value-q*exit_fee
    pnl = proceeds-spent-fees-operating if complete else None
    fingerprint = hashlib.sha256(json.dumps(dict(config=cfg, events=timeline, reward_program=reward_program), sort_keys=True).encode()).hexdigest()
    reward_result = None
    if reward_program is not None:
        from decimal import ROUND_DOWN
        floored = min(reward_total, reward_cap).quantize(number('.01'), rounding=ROUND_DOWN)
        payable = floored if floored >= 1 else number(0)
        reward_result = dict(status='MODELED_NOT_CREDITED', program_id=reward_program['id'],
            modeled_accrual_usd=str(reward_total), estimated_payout_if_stop_usd=str(payable),
            positive_share_seconds=str(reward_seconds),
            modeled_net_if_stop_usd=str(pnl+payable) if pnl is not None else None)
    return dict(reward_model=reward_result, status='MODELED_RESEARCH_ONLY' , fingerprint=fingerprint, config=cfg,
                fills=fills, decisions=decisions, inventory={k:str(v) for k,v in positions.items()},
                spent_usd=str(spent), maker_fees_usd=str(fees), liquidation_complete=complete,
                net_before_rewards_usd=str(pnl) if pnl is not None else None,
                break_even_credited_reward_usd=str(max(number(0),-pnl)) if pnl is not None else None,
                capture_gap=had_gap, live_eligible=False, veto_counts=vetoes,
                limitations=['Observed tape is a counterfactual approximation; no market impact modeled.',
                             'Fees are scenario assumptions. No simulated reward is credited.',
                             'Missing queue cancellations never improve our queue position.'])


def compare(events, config=ReplayConfig()):
    return {policy: replay(events, ReplayConfig(**dict(asdict(config), policy=policy)))
            for policy in ('do_nothing','join_best','spread_guard')}


def compare_challenger(events, config=ReplayConfig()):
    """Preserve the original baselines and add one explicitly named challenger."""
    result=compare(events,config)
    result['defensive_maker']=replay(events,ReplayConfig(**dict(asdict(config),policy='defensive_maker')))
    return result
