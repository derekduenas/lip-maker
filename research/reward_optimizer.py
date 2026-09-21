"""Contract quote economics under the documented LIP model; never sends orders.

Caller supplies competitor-only books and quote-specific cost/uptime scenarios.
Those forecasts are assumptions, not measurements or exchange payout promises.
"""
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from research.profit_ledger import number as D

SOURCE = 'https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program'


def _seconds(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('program dates require timezone')
    return D(str(dt.timestamp()))


def _side(levels, price, quantity, target, discount, tick):
    depth = {}
    for p, q in levels:
        p, q = D(p), D(q)
        if not 0 < p < 1 or q < 0 or p % tick:
            raise ValueError('invalid or off-grid book')
        if q:
            depth[p] = depth.get(p, D(0)) + q
    depth[price] = depth.get(price, D(0)) + quantity
    cumulative = D(0)
    reference = cutoff = None
    for p in sorted(depth, reverse=True):
        cumulative += depth[p]
        if reference is None and cumulative >= target / 5:
            reference = p
        if cumulative >= target:
            cutoff = p
            break
    if cutoff is None:
        return D(0), None, reference
    def weight(p):
        return discount ** int(max(D(0), (reference - p) / tick))
    total = sum(q * weight(p) for p, q in depth.items() if p >= cutoff)
    ours = quantity * weight(price) if price >= cutoff else D(0)
    return ours / total, cutoff, reference


def rank_quotes(program, book, candidates, *, now, horizon_seconds, tick_usd, max_book_age_seconds=2):
    """Rank alternative quote pairs for ONE exact program, never sum alternatives.

    Each candidate: id, yes_price, no_price, size, qualified_fraction,
    trading_pnl_usd (excluding fees), fees_usd, operating_cost_usd,
    uncertainty_reserve_usd. Economics must cover exactly the requested forecast horizon.
    Program reward/cap integers are centi-cents. Existing program accrual is
    deliberately unsupported: this function evaluates a fresh participation plan.
    """
    now, horizon, tick = D(now), D(horizon_seconds), D(tick_usd)
    if horizon <= 0 or not 0 < tick < 1 or D(max_book_age_seconds) <= 0:
        raise ValueError('invalid horizon/tick/freshness')
    if book['market'] != program['market_ticker']:
        raise ValueError('contract mismatch')
    if book.get('contains_own_orders') is not False:
        raise ValueError('competitor-only book required to prevent double counting')
    if program['incentive_type'] != 'liquidity':
        raise ValueError('unsupported incentive type')
    start, end = _seconds(program['start_date']), _seconds(program['end_date'])
    if end <= start:
        raise ValueError('invalid program window')
    for field in ('period_reward', 'discount_factor_bps'):
        if type(program[field]) is not int:
            raise ValueError('integer API units required')
    pool, discount, target = D(program['period_reward']) / 10000, D(program['discount_factor_bps']) / 10000, D(program['target_size_fp'])
    cap_raw = program.get('max_reward_per_account')
    if cap_raw is not None and (type(cap_raw) is not int or cap_raw < 0):
        raise ValueError('invalid account cap')
    cap = pool if cap_raw is None else min(pool, D(cap_raw) / 10000)
    if pool <= 0 or target <= 0 or not 0 < discount <= 1:
        raise ValueError('invalid program economics')
    reasons = []
    if not start <= now < end or program.get('paid_out') is not False:
        reasons.append('inactive_program')
    if book.get('valid') is not True or not 0 <= now - D(book['ts']) <= D(max_book_age_seconds):
        reasons.append('stale_or_invalid_book')
    if book.get('status') != 'open':
        reasons.append('market_not_open')
    result = dict(status='SCENARIO_ESTIMATE_ONLY', program_id=program['id'], market=program['market_ticker'], source=SOURCE, live_eligible=False, blocked_reasons=reasons, ranked=[])
    if reasons:
        return result
    if horizon > end - now:
        raise ValueError("cost forecast horizon exceeds remaining program window")
    duration = horizon
    ids = set()
    for c in candidates:
        if c['id'] in ids:
            raise ValueError('duplicate candidate')
        ids.add(c['id'])
        y, n, q, uptime = (D(c[k]) for k in ('yes_price', 'no_price', 'size', 'qualified_fraction'))
        if not 0 < y < 1 or not 0 < n < 1 or y % tick or n % tick or y+n >= 1 or q <= 0 or not 0 <= uptime <= 1:
            raise ValueError('invalid quote')
        fees, operating, reserve = (D(c[k]) for k in ('fees_usd', 'operating_cost_usd', 'uncertainty_reserve_usd'))
        if min(fees, operating, reserve) < 0:
            raise ValueError('negative costs')
        best_y = max((D(p) for p,s in book['yes_bids'] if D(s)>0), default=D(0))
        best_n = max((D(p) for p,s in book['no_bids'] if D(s)>0), default=D(0))
        ys,ycut,yref = _side(book['yes_bids'], y,q,target,discount,tick)
        ns,ncut,nref = _side(book['no_bids'], n,q,target,discount,tick)
        qualified = ycut is not None and ncut is not None
        post_only = y + best_n < 1 and n + best_y < 1
        share = (ys+ns)/2 if qualified and post_only else D(0)
        reward = min(cap, pool * share * uptime * duration / (end-start))
        reward = reward.quantize(D('.01'), rounding=ROUND_DOWN)
        payable = reward if reward >= 1 else D(0)
        capital = q*(y+n)+fees
        net = payable + D(c['trading_pnl_usd']) - fees - operating - reserve
        rows = dict(c, qualified=qualified, post_only=post_only, modeled_share=str(share), reward_before_minimum_usd=str(reward), estimated_payable_reward_usd=str(payable), capital_usd=str(capital), conservative_scenario_net_usd=str(net), net_per_capital_hour=str(net/(capital*duration/3600)), horizon_seconds=str(duration), yes_cutoff=str(ycut), no_cutoff=str(ncut), yes_reference=str(yref), no_reference=str(nref), research_candidate=qualified and post_only and net>0)
        result['ranked'].append(rows)
    result['ranked'].sort(key=lambda r:D(r['net_per_capital_hour']),reverse=True)
    return result
