"""Predeclared alternative policies on one program; incomplete evidence stays explicit."""
from dataclasses import replace
from research.maker_replay import replay, ReplayConfig
from research.reward_optimizer import _seconds


def run_program(events, program, config, sizes):
    start=int(_seconds(program['start_date'])*1000)
    end=int(_seconds(program['end_date'])*1000)
    # A timestamp span alone does not establish full market-state coverage.
    span=bool(events) and events[0]['ts_ms']<=start and events[-1]['ts_ms']>=end
    results=[]
    for size in sizes:
        for policy in ('do_nothing','join_best','reward_depth','reward_inventory'):
            cfg=replace(ReplayConfig(**config),size=str(size),policy=policy,program_start_ms=start,program_end_ms=end)
            result=replay(events,cfg,program)
            results.append(dict(size=str(size),policy=policy,result=result))
    return dict(status='PROGRAM_EXPERIMENT_RESEARCH_ONLY',program_id=program['id'],
                program_span_present=span,full_program_validated=False,
                selected_policy=None,live_eligible=False,results=results,
                limitations=['Alternatives each receive the same hypothetical capital; do not sum returns.',
                'Reward-depth optimizes current reward share per funded dollar, not a calibrated net-profit forecast.',
                'Inventory caps stop new risk; active inventory hedging/unwinding is not implemented.',
                'Market lifecycle, complete scoring snapshots and realized credits remain unverified.',
                'No policy is selected from these development results.'])
