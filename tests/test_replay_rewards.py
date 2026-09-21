from decimal import Decimal as D
from dataclasses import replace
import pytest
from research.maker_replay import ReplayConfig,replay
from tests.test_maker_research import book,trade


def program(**kw):
    return dict(dict(id='p',market_ticker='M',incentive_type='liquidity',start_date='1970-01-01T00:00:00Z',end_date='1970-01-01T00:00:10Z',period_reward=1000000,target_size_fp='2',discount_factor_bps=5000),**kw)


def test_resting_only_and_first_interval_unknown():
    r=replay([book(),book(300),book(1300)],ReplayConfig(),program())
    # Orders activate at 300, so only 1 second of 1/3 share is measured.
    assert D(r['reward_model']['modeled_accrual_usd'])==D(100)/30


def test_fill_reduces_forward_credit():
    cfg=replace(ReplayConfig(),latency_ms=0)
    r=replay([book(),trade(1000),book(2000)],cfg,program())
    # First second 1/3, second second only NO contribution (1/6).
    assert abs(D(r['reward_model']['modeled_accrual_usd'])-D(5))<D('1e-20')


def test_stale_does_not_earn_across_gap():
    cfg=replace(ReplayConfig(),latency_ms=0,stale_ms=1000)
    r=replay([book(),book(5000)],cfg,program())
    assert r['reward_model']['positive_share_seconds']=='1'


def test_cap_and_minimum_no_cash_credit():
    r=replay([book(),book(300),book(1300)],ReplayConfig(),program(max_reward_per_account=9999))
    assert r['reward_model']['estimated_payout_if_stop_usd']=='0'
    assert r['live_eligible'] is False


def test_program_identity_and_window():
    with pytest.raises(ValueError):replay([book()],ReplayConfig(),program(market_ticker='other'))
    r=replay([book(),book(300),book(1300)],ReplayConfig(),program(start_date='1970-01-01T00:00:05Z'))
    assert D(r['reward_model']['modeled_accrual_usd'])==0


def test_program_changes_fingerprint():
    a=replay([book()],ReplayConfig(),program())
    b=replay([book()],ReplayConfig(),program(period_reward=2000000))
    assert a['fingerprint']!=b['fingerprint']


def test_session_blocks_before_replay_on_capture_failure(monkeypatch,tmp_path):
    import asyncio
    import research.websocket_paper as w
    async def blocked(*args):return {'complete':False,'error_type':'FileNotFoundError'}
    monkeypatch.setattr(w,'capture',blocked)
    cfg=dict(capital_usd='35',maker_fee_per_contract_usd='.01',exit_fee_per_contract_usd='.02',latency_ms=250,queue_multiplier='1')
    assert asyncio.run(w.run_session(str(tmp_path/'x'),program(),cfg))['status']=='BLOCKED'
