"""Frozen chronological policy comparison. No reward or independence invented."""
from research.maker_replay import compare, ReplayConfig
from research.profit_ledger import number
from research.venue_capture import digest


def attack_profitability(episodes, scenarios, cutoff_ms):
    if type(cutoff_ms) is not int or cutoff_ms<0 or not scenarios:
        raise ValueError('explicit cutoff and stress scenarios required')
    # Input hash seals all scenarios and the split. No holdout-driven tuning.
    fingerprint=digest(dict(episodes=episodes,scenarios=scenarios,cutoff_ms=cutoff_ms))
    splits={'development':[],'held_out':[]}; seen=set(); owners={}; blockers=[]
    for episode in episodes:
        eid=episode['episode_id']; underlying=episode['underlying_event_id']; events=episode['events']
        if not eid or not underlying or eid in seen or not events:
            raise ValueError('unique nonempty episode and underlying event required')
        seen.add(eid)
        if any(e.get('episode_id')!=eid for e in events):
            raise ValueError('episode identity mismatch')
        start,end=events[0]['ts_ms'],events[-1]['ts_ms']
        if start<cutoff_ms<=end:
            raise ValueError('episode straddles chronological cutoff')
        split='development' if end<cutoff_ms else 'held_out'
        if underlying in owners and owners[underlying]!=split:
            raise ValueError('same underlying event leaks across split')
        owners[underlying]=split
        outcomes=[]
        for scenario in scenarios:
            required={'maker_fee_per_contract_usd','exit_fee_per_contract_usd','latency_ms','queue_multiplier'}
            if not required<=scenario.keys():
                raise ValueError('explicit cost and execution scenarios required')
            results=compare(events,ReplayConfig(**scenario))
            outcomes.append(results)
            if any(r['capture_gap'] or not r['liquidation_complete'] for r in results.values()):
                blockers.append(eid+':incomplete_capture_or_liquidation')
        splits[split].append(dict(episode_id=eid,underlying_event_id=underlying,outcomes=outcomes))
    if not all(splits.values()):
        blockers.append('both_chronological_splits_required')
    policies=('do_nothing','join_best','spread_guard')
    def summary(rows):
        result={}
        for policy in policies:
            stressed=[]; break_even=[]
            for row in rows:
                values=[r[policy]['net_before_rewards_usd'] for r in row['outcomes']]
                if any(v is None for v in values):
                    continue
                worst=min(number(v) for v in values)
                stressed.append(worst); break_even.append(max(number(0),-worst))
            result[policy]=dict(episodes=len(rows),valued_episodes=len(stressed),
                mean_worst_scenario_net_usd=str(sum(stressed)/len(stressed)) if stressed else None,
                mean_break_even_reward_usd=str(sum(break_even)/len(break_even)) if break_even else None)
        return result
    development=summary(splits['development']); held_out=summary(splits['held_out'])
    selected=None
    if not blockers:
        selected=max(policies,key=lambda p:number(development[p]['mean_worst_scenario_net_usd']))
        if selected=='do_nothing' or number(development[selected]['mean_worst_scenario_net_usd'])<=0:
            selected=None
            blockers.append('no_positive_development_edge_before_rewards')
    if selected and number(held_out[selected]['mean_worst_scenario_net_usd'])<=0:
        blockers.append('selected_policy_failed_held_out_cost_stress')
    return dict(status='RESEARCH_ONLY',fingerprint=fingerprint,selected_from_development=selected,
                development=development,held_out=held_out,blockers=sorted(set(blockers)),
                unique_underlying_events={s:len({r['underlying_event_id'] for r in rows}) for s,rows in splits.items()},
                live_eligible=False,profitability_verified=False,
                limitations=['Episode means are not portfolio returns or evidence of independent observations.',
                             'Each episode starts flat. No capital compounding or cross-episode inventory modeled.',
                             'Actual reward credits cannot be awarded to counterfactual baseline policies.',
                             'A positive modeled result still requires source coverage and statistical validation.'])
