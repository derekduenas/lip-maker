"""Final review F8: the screen's markout "base" must be the same adverse
charge selector.quote_economics subtracts (including the long-dated -1c
prior), so net - rank penalty charges the markout prior exactly once on
durable markets too."""
import pytest

from mm import selector as SEL
from mm.unattended import screen as S
from tests.test_review_venue_economics import FRAME, META, _as_in_net, _km


@pytest.fixture(autouse=True)
def _no_holding(monkeypatch):
    # carry model at 0% APR: quote_economics subtracts nothing for holding,
    # so reward - net is the adverse-selection term alone.
    monkeypatch.setenv("LIP_HOLDING_MODEL", "carry")
    monkeypatch.setenv("LIP_CARRY_APR", "0")


@pytest.mark.parametrize("days", [1.0, 30.0, 80.0])
def test_markout_prior_charged_exactly_once_short_and_long_dated(monkeypatch, days):
    monkeypatch.setenv("LIP_RANK_SHORT_K", "3")
    monkeypatch.setenv("LIP_RANK_NEWS_MULT", "2")
    km = _km(days_to_settle=days)
    fill = SEL.FILL_FRACTION_PER_DAY["event"]
    prior = abs(SEL.MARKOUT_PRIOR_CENTS["event"]) + (1.0 if days > SEL.MARKOUT_LONG_DATED_DAYS else 0.0)
    base_100 = 100 * 2 * fill * prior / 100
    assert _as_in_net(km) == pytest.approx(base_100)  # what net already charges
    vol_mult, time_mult, news_mult = 2.0, 1.0 + 3.0 / days, 2.0
    rk = S.rank_score(FRAME, META, category="Entertainment", days=days)
    # net - penalty == the full multiplied charge, base counted once
    assert _as_in_net(km) + rk["penalty"] == pytest.approx(base_100 * vol_mult * time_mult * news_mult)


def test_long_dated_without_multipliers_adds_no_penalty(monkeypatch):
    monkeypatch.setenv("LIP_RANK_SHORT_K", "0")
    km = _km(days_to_settle=30.0)
    rk = S.rank_score(FRAME, dict(META, volume_24h=0), category="Economics", days=30.0)
    assert rk["penalty"] == pytest.approx(0.0)
    assert SEL.adverse_cost_per_contract_day(km) * 100 == pytest.approx(_as_in_net(km))
