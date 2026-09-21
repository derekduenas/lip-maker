import pytest
from research.maker_replay import replay,ReplayConfig
from research.program_experiment import run_program
from tests.test_maker_research import book
from tests.test_replay_rewards import program


def test_reward_depth_requires_program():
    with pytest.raises(ValueError):replay([book()],ReplayConfig(policy='reward_depth'))


def test_reward_depth_uses_own_size_to_qualify():
    p=program(target_size_fp='3')
    r=replay([book(),book(300)],ReplayConfig(policy='reward_depth'),p)
    assert len(r['decisions'])==2
    assert all(d['price_usd'] is not None for d in r['decisions'])


def test_reward_depth_prefix_is_causal():
    cfg=ReplayConfig(policy='reward_depth')
    a=replay([book(),book(300)],cfg,program())
    b=replay([book(),book(300),book(1000)],cfg,program())
    assert b['decisions'][:len(a['decisions'])]==a['decisions']


def test_incomplete_program_is_not_promoted():
    r=run_program([book(),book(300)],program(),{'capital_usd':'5000'},[1,2])
    assert len(r['results'])==8
    assert not r['program_span_present'] and not r['full_program_validated']
    assert r['selected_policy'] is None
