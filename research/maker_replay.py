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


def replay(events, config=ReplayConfig()):
    cfg = asdict(config)
    size, budget = number(config.size), number(config.capital_usd)
    maker_fee, exit_fee = number(config.maker_fee_per_contract_usd), number(config.exit_fee_per_contract_usd)
    operating, queue_mult = number(config.operating_cost_usd), number(config.queue_multiplier)
    if size <= 0 or budget < 0 or min(maker_fee,exit_fee,operating) < 0 or queue_mult < 1:
        raise ValueError('invalid replay economics')
    if config.latency_ms < 0 or config.stale_ms <= 0 or config.policy not in ('join_best','spread_guard','do_nothing'):
        raise ValueError('invalid policy/timing')
    if not 0 <= number(config.max_spread_usd) <= 1:
        raise ValueError("invalid spread threshold")
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
    book = None
    orders, pending = {}, {}
    positions = {'yes': number(0), 'no': number(0)}
    spent = fees = number(0)
    fills, decisions = [], []
    had_gap = False

    def fresh(now):
        return book is not None and book.get('valid', True) and 0 <= now-book['ts_ms'] <= config.stale_ms

    def bid(side):
        return max((number(p) for p,q in book.get(side+'_bids', []) if number(q)>0), default=None)

    def advance(now):
        # Book silence triggers cancellation after stale threshold + latency.
        if book is not None and now > book['ts_ms'] + config.stale_ms + config.latency_ms:
            orders.clear()
            pending.clear()
        for side in list(pending):
            due, price = pending[side]
            if due > now:
                continue
            orders.pop(side, None)
            pending.pop(side)
            if price is None or not fresh(now):
                continue
            other = bid('no' if side == 'yes' else 'yes')
            if other is None or price + other >= 1:
                continue  # post-only reject at simulated activation
            reserved = sum(o['remaining']*(o['price']+maker_fee) for o in orders.values())
            if spent + fees + reserved + size*(price+maker_fee) > budget:
                continue
            ahead = sum(number(q) for p,q in book.get(side+'_bids', []) if number(p)>=price)
            orders[side] = dict(price=price, remaining=size, ahead=ahead*queue_mult)

    for e in timeline:
        now = e['ts_ms']
        advance(now)
        if e['kind'] == 'gap':
            had_gap = True
            book = None
            pending = {s:(now+config.latency_ms,None) for s in orders}
            continue
        if e['kind'] == 'book':
            # Validate all levels, including crossed/empty detection below.
            for side in positions:
                exit_value(e, side, size)
            book = e
            if not e.get('valid',True):
                had_gap = True
            y,n = bid('yes'), bid('no')
            allowed = fresh(now) and y is not None and n is not None and y+n<1
            if config.policy == 'spread_guard' and allowed:
                allowed = 1-y-n <= number(config.max_spread_usd)
            if config.policy == 'do_nothing':
                allowed = False
            for side in positions:
                desired = bid(side) if allowed else None
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
            # Explicit aggressor side is required; never guess from price alone.
            if e.get('aggressor') not in ('buy','sell') or e.get('side') not in positions:
                raise ValueError('trade needs token side and aggressor')
            p, q = number(e['price_usd']), number(e['quantity'])
            if q <= 0 or not 0 <= p <= 1:
                raise ValueError('invalid trade')
            o = orders.get(e['side'])
            if not o or e['aggressor'] != 'sell' or p > o['price']:
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
        value = exit_value(book, side, q) if fresh(end) else None
        if value is None:
            complete = False
        else:
            proceeds += value-q*exit_fee
    pnl = proceeds-spent-fees-operating if complete else None
    fingerprint = hashlib.sha256(json.dumps(dict(config=cfg, events=timeline), sort_keys=True).encode()).hexdigest()
    return dict(status='MODELED_RESEARCH_ONLY', fingerprint=fingerprint, config=cfg,
                fills=fills, decisions=decisions, inventory={k:str(v) for k,v in positions.items()},
                spent_usd=str(spent), maker_fees_usd=str(fees), liquidation_complete=complete,
                net_before_rewards_usd=str(pnl) if pnl is not None else None,
                break_even_credited_reward_usd=str(max(number(0),-pnl)) if pnl is not None else None,
                capture_gap=had_gap, live_eligible=False,
                limitations=['Observed tape is a counterfactual approximation; no market impact modeled.',
                             'Fees are scenario assumptions. No simulated reward is credited.',
                             'Missing queue cancellations never improve our queue position.'])


def compare(events, config=ReplayConfig()):
    return {policy: replay(events, ReplayConfig(**dict(asdict(config), policy=policy)))
            for policy in ('do_nothing','join_best','spread_guard')}
