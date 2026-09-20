"""Post-fill measurements with explicit missingness and episode aggregation."""
import math
from collections import defaultdict
from statistics import mean, stdev
from research.profit_ledger import number


def exit_value(book, side, quantity):
    """Depth-weighted executable gross proceeds; None for insufficient depth."""
    remaining, value = number(quantity), number(0)
    if remaining < 0:
        raise ValueError('negative quantity')
    levels = [(number(p), number(q)) for p, q in book.get(side + '_bids', [])]
    if side not in ('yes', 'no') or any(not 0 <= p <= 1 or q < 0 for p, q in levels):
        raise ValueError('invalid book')
    for price, size in sorted(levels, reverse=True):
        take = min(remaining, size)
        value += take * price
        remaining -= take
        if remaining == 0:
            return value
    return value if remaining == 0 else None


def markouts(fills, books, *, horizons_ms=(1000, 10000, 60000), tolerance_ms=250):
    """Positive cost = adverse movement/exit spread. Not additional realized P&L.

    Labels use the first valid book at/after a horizon, never a distant future
    observation. Gaps between fill and label invalidate the label.
    """
    if tolerance_ms < 0 or any(type(h) is not int or h <= 0 for h in horizons_ms):
        raise ValueError("invalid measurement horizon")
    by_market = defaultdict(list)
    for b in books:
        by_market[b['market']].append(b)
    for rows in by_market.values():
        rows.sort(key=lambda b: b['ts_ms'])
    output = []
    for f in fills:
        q, price = number(f['quantity']), number(f['price_usd'])
        if q <= 0 or not 0 <= price <= 1 or f['side'] not in ('yes', 'no'):
            raise ValueError('invalid fill')
        result = dict(fill_id=f['event_id'], market=f['market'], episode_id=f['episode_id'], labels={})
        for h in horizons_ms:
            due = f['ts_ms'] + h
            rows = [b for b in by_market[f['market']]
                    if b.get('episode_id', f['episode_id']) == f['episode_id']]
            candidates = [b for b in rows if due <= b['ts_ms'] <= due + tolerance_ms]
            book = candidates[0] if candidates else None
            broken = any(not b.get('valid', True) for b in rows
                         if f['ts_ms'] <= b['ts_ms'] <= (book['ts_ms'] if book else due))
            value = exit_value(book, f['side'], q) if book and not broken and book.get('valid', True) else None
            result['labels'][str(h)] = dict(
                adverse_exit_cost_per_contract_usd=str(price - value/q) if value is not None else None,
                measured_ts_ms=book['ts_ms'] if book else None,
                reason='ok' if value is not None else 'missing_or_invalid_depth_or_gap')
        output.append(result)
    return output


def summarize_markouts(rows, horizon_ms=60000):
    episodes = defaultdict(list)
    missing = 0
    for row in rows:
        v = row['labels'][str(horizon_ms)]['adverse_exit_cost_per_contract_usd']
        if v is None:
            missing += 1
        else:
            episodes[row['episode_id']].append(float(v))
    values = [mean(v) for v in episodes.values()]
    return dict(episode_count=len(values), fill_count=len(rows), missing_labels=missing,
                mean_episode_adverse_cost_usd=mean(values) if values else None,
                descriptive_upper_95_usd=(mean(values) + 1.96*stdev(values)/math.sqrt(len(values)))
                    if len(values)>1 else None,
                caveat='Descriptive cluster summary; not proof of independence or future edge.')


def evaluate_candidates(candidates, budget_usd, event_cap_usd):
    """Research-only marginal economics. Costs must share the same horizon.

    No strategy receives a positive recommendation from an unverified reward
    forecast or an in-sample result. Heuristics do not authorize live orders.
    """
    ranked, rejected = [], []
    for c in candidates:
        reasons = []
        if not c.get('rules_verified') or not c.get('reward_receipts_reconciled'):
            reasons.append('unverified_reward_economics')
        if c.get('evaluation_split') != 'held_out' or c.get('independent_episodes', 0) < 30:
            reasons.append('insufficient_held_out_evidence')
        capital, hours = number(c['capital_usd']), number(c['horizon_hours'])
        if capital <= 0 or hours <= 0:
            raise ValueError('positive capital and horizon required')
        if min(number(c["operating_cost_usd"]), number(c["uncertainty_allowance_usd"])) < 0:
            raise ValueError("negative cost assumption")
        # Trading P&L must already include spread, adverse selection, fees and unwind.
        net = number(c['reward_lower_bound_usd']) + number(c['trading_pnl_lower_bound_usd']) \
            - number(c['operating_cost_usd']) - number(c['uncertainty_allowance_usd'])
        if net <= 0:
            reasons.append('nonpositive_conservative_net')
        row = dict(c, conservative_net_usd=str(net),
                   net_per_dollar_hour=str(net/capital/hours), live_eligible=False)
        if reasons:
            rejected.append(dict(row, reasons=reasons))
        else:
            ranked.append(row)
    budget, event_cap = number(budget_usd), number(event_cap_usd)
    if budget < 0 or event_cap < 0:
        raise ValueError('negative allocation cap')
    selected, exposure = [], defaultdict(lambda: number(0))
    for row in sorted(ranked, key=lambda c: number(c['net_per_dollar_hour']), reverse=True):
        capital = number(row['capital_usd'])
        event = row['underlying_event_id']
        if capital > budget or exposure[event] + capital > event_cap:
            rejected.append(dict(row, reasons=['capital_or_correlated_event_cap']))
        else:
            selected.append(row)
            budget -= capital
            exposure[event] += capital
    return dict(selected=selected, rejected=rejected, unused_budget_usd=str(budget),
                live_eligible=False, status='RESEARCH_ONLY')
