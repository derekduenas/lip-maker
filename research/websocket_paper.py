"""Bounded authenticated market-data capture followed by reward-aware paper replay."""
import json
from pathlib import Path
from research.venue_capture import capture, export_capture
from research.maker_replay import replay, ReplayConfig


async def run_session(output, program, config, seconds=60):
    required={'maker_fee_per_contract_usd','exit_fee_per_contract_usd','latency_ms','queue_multiplier','capital_usd'}
    if not required <= config.keys():
        raise ValueError('explicit capital and execution assumptions required')
    cfg=ReplayConfig(**config)
    from research.profit_ledger import number
    if not 0<number(cfg.capital_usd)<=5000:
        raise ValueError('paper experiment cap is $5000')
    manifest=await capture(output,program['market_ticker'],seconds)
    if manifest.get('complete') is not True:
        return dict(status='BLOCKED',capture=manifest,live_eligible=False)
    try:
        events=export_capture(output,Path(output).name)
        result=replay(events,cfg,reward_program=program)
    except (ValueError,KeyError) as exc:
        return dict(status='BLOCKED',error_type=type(exc).__name__,live_eligible=False)
    result['capture']=manifest
    result['event_count']=len(events)
    result['trade_count']=sum(e['kind']=='trade' for e in events)
    result['session_mode']='CAPTURE_THEN_REPLAY_NOT_CONTINUOUS_TRADER'
    result['limitations'].append('Reward estimates are not credits; market-open state is assumed for this book/trade capture and must be independently verified.')
    Path(str(output)+'.paper.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
