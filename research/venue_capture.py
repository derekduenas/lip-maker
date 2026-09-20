"""Bounded read-only WS recorder and strict, decimal market-data normalization.

Runs separately from the trader. No order client or private fill subscription.
Price convention is pinned to unified YES prices in the subscription.
"""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from research.profit_ledger import canonical, number


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalize(records, market, episode_id):
    books, sequences, seen_trades = {}, {}, {}
    result = []
    last = -1
    for i, record in enumerate(records):
        now = record['received_ms']
        if type(now) is not int or now < 0 or now < last:
            raise ValueError('capture clock moved backward')
        last = now
        msg = record['message']; kind = msg.get('type')
        if kind == 'error':
            raise ValueError('venue subscription error')
        if kind not in ('orderbook_snapshot','orderbook_delta','trade'):
            continue
        sid, seq = msg.get('sid'), msg.get('seq')
        if type(sid) is not int or type(seq) is not int:
            raise ValueError('missing subscription sequence')
        if sid in sequences and seq != sequences[sid]+1:
            raise ValueError('subscription sequence discontinuity')
        sequences[sid] = seq
        m = msg['msg']; ticker = m['market_ticker']
        if ticker != market:
            raise ValueError('unexpected market in single-market capture')
        base = dict(event_id=f'{episode_id}:{i}',market=ticker,episode_id=episode_id,ts_ms=now)
        if kind == 'orderbook_snapshot':
            levels = {}
            for side in ('yes','no'):
                levels[side] = {}
                for p,q in m[side+'_dollars_fp']:
                    p,q = number(p),number(q)
                    if not 0 <= p <= 1 or q < 0:
                        raise ValueError('invalid snapshot')
                    p = p if side == 'yes' else 1-p
                    if p in levels[side]:
                        raise ValueError('duplicate snapshot price')
                    levels[side][p] = q
            books[sid] = levels
        elif kind == 'orderbook_delta':
            if sid not in books:
                raise ValueError('delta before snapshot')
            side=m['side']; p=number(m['price_dollars']); delta=number(m['delta_fp'])
            if side not in ('yes','no') or not 0 <= p <= 1:
                raise ValueError('invalid delta')
            p=p if side=='yes' else 1-p
            q=books[sid][side].get(p,number(0))+delta
            if q<0:
                raise ValueError('negative reconstructed depth')
            books[sid][side][p]=q
        else:
            if m.get('is_block_trade') is not False:
                raise ValueError('block-trade classification missing or unsupported')
            direction=m.get('taker_outcome_side')
            if direction not in ('yes','no'):
                raise ValueError('canonical trade direction missing')
            if m.get('taker_book_side', 'bid' if direction=='yes' else 'ask') != ('bid' if direction=='yes' else 'ask'):
                raise ValueError('conflicting trade direction')
            p,q=number(m['yes_price_dollars']),number(m['count_fp'])
            if not 0<=p<=1 or q<=0:
                raise ValueError('invalid trade')
            if 'no_price_dollars' in m and number(m['no_price_dollars']) != 1-p:
                raise ValueError('noncomplementary trade prices')
            tid=m['trade_id']
            if not isinstance(tid,str) or not tid:
                raise ValueError('trade identity missing')
            if tid in seen_trades:
                if seen_trades[tid] != canonical(m):
                    raise ValueError('conflicting trade identity')
                continue
            seen_trades[tid]=canonical(m)
            exchange=m['ts_ms']
            if type(exchange) is not int or exchange<0 or exchange>now:
                raise ValueError('invalid exchange timestamp / clock skew')
            # A long-NO aggressor sells into YES bids; long-YES hits NO bids.
            side='no' if direction=='yes' else 'yes'
            result.append(dict(base,kind='trade',side=side,aggressor='sell',
                               price_usd=str(1-p if side=='no' else p),quantity=str(q),exchange_ts_ms=exchange))
            continue
        levels=books[sid]
        result.append(dict(base,kind='book',valid=True,**{
            side+'_bids':[[str(p),str(q)] for p,q in sorted(levels[side].items(),reverse=True) if q>0]
            for side in ('yes','no')}))
    return result


async def capture(path, market, seconds=60):
    if not 0<seconds<=3600 or not market:
        raise ValueError('capture requires a market and 0 < seconds <= 3600')
    from execution.kalshi_ws import KalshiWS
    path=Path(path)
    # Exclusive files prevent accidentally overwriting another capture.
    manifest_path=Path(str(path)+'.manifest.json')
    manifest=dict(schema=1,market=market,price_convention='unified_yes',complete=False,
                  started_ms=time.time_ns()//1000000,records=0,final_hash='',status='STARTING')
    with manifest_path.open('x') as f:
        f.write(canonical(manifest))
    ws=None; chain=''; count=0
    try:
        with path.open('x') as output:
            ws=KalshiWS()
            await ws.connect()
            await ws._ws.send(json.dumps(dict(id=1,cmd='subscribe',params=dict(
                channels=['orderbook_delta'],market_tickers=[market],use_yes_price=True))))
            await ws._ws.send(json.dumps(dict(id=2,cmd='subscribe',params=dict(
                channels=['trade'],market_tickers=[market]))))
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                try:
                    raw=await asyncio.wait_for(ws._ws.recv(),deadline-time.monotonic())
                except asyncio.TimeoutError:
                    break
                received_ms=time.time_ns()//1000000
                payload=dict(n=count,previous_hash=chain,received_ms=received_ms,
                             received_monotonic_ns=time.monotonic_ns(),message=json.loads(raw))
                chain=digest(payload)
                output.write(canonical(dict(payload,sha256=chain))+'\n')
                output.flush()
                count+=1
                if payload['message'].get('type')=='error':
                    raise ValueError('venue returned subscription error')
            os.fsync(output.fileno())
        manifest.update(complete=True,status='CAPTURED_NOT_VALIDATED')
    except Exception as exc:
        manifest.update(status='BLOCKED',error_type=type(exc).__name__)
    finally:
        if ws is not None:
            await ws.close()
        manifest.update(records=count,final_hash=chain,ended_ms=time.time_ns()//1000000)
        temp=Path(str(manifest_path)+'.tmp')
        temp.write_text(canonical(manifest)); os.replace(temp,manifest_path)
    return manifest


def export_capture(path, episode_id):
    path=Path(path); manifest=json.loads(Path(str(path)+'.manifest.json').read_text())
    if manifest.get('complete') is not True or manifest.get('price_convention')!='unified_yes':
        raise ValueError('incomplete or unsupported capture')
    records=[]; chain=''
    with path.open() as f:
        for i,line in enumerate(f):
            row=json.loads(line); checksum=row.pop('sha256')
            if row['n']!=i or row['previous_hash']!=chain or checksum!=digest(row):
                raise ValueError('capture integrity failure')
            chain=checksum; records.append(row)
    if len(records)!=manifest['records'] or chain!=manifest['final_hash']:
        raise ValueError('capture truncated')
    events=normalize(records,manifest['market'],episode_id)
    if not any(e['kind']=='book' for e in events):
        raise ValueError('no book observations')
    # Require both subscription ACKs even when there were no trades.
    channels={r['message'].get('msg',{}).get('channel') for r in records if r['message'].get('type')=='subscribed'}
    if not {'orderbook_delta','trade'}<=channels:
        raise ValueError('both channel subscriptions were not acknowledged')
    return events
