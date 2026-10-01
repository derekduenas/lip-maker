"""Risk caps skip not kill, budget, sequencer, carry-forward scoring, compaction, rank."""
from decimal import Decimal

from engine.lip_accrual import SecondMark
from mm.risk import RiskDecision
from mm.unattended import loop as L
from mm.unattended import screen as S
from mm.selector import min_close_hours

T0 = 1_790_800_000.0


def _prog(loop, market, series="KXA", close=4e9):
    loop.add_program({"market": market, "series": series, "period_reward_usd": 100,
                      "period_seconds": 86400, "start_ts": T0 - 3600, "end_ts": 4e9, "close_ts": close,
                      "target_size": 100, "days_from_close": True, "rank_score": 0.1})


def test_cap_denial_skips_without_kill(monkeypatch):
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-1")
    monkeypatch.setattr(loop.risk, "check_quote",
                        lambda **kw: RiskDecision(False, "per_venue kalshi 1600 > 1500"))
    assert loop._quote("KXA-1", 40, 55, 10, 1000.0) is False
    assert loop.kill is None and loop.cap_skips and "KXA-1" not in loop.resting


def test_genuine_breach_still_kills(monkeypatch):
    loop = L.RunLoop(mode="paper", bankroll=5000)
    _prog(loop, "KXA-1")
    monkeypatch.setattr(loop.risk, "check_quote",
                        lambda **kw: RiskDecision(False, "daily_loss -300 <= -250", cancel_all=True))
    loop._quote("KXA-1", 40, 55, 10, 1000.0)
    assert loop.kill is not None and loop.kill["reason"].startswith("daily_loss")


def test_limits_follow_bankroll():
    loop = L.RunLoop(mode="paper", bankroll=5000)
    assert float(loop.risk.limits.per_venue_usd) == 1500.0


def test_alloc_cap_fraction_env(monkeypatch):
    assert L.alloc_cap_fraction() == 0.95
    monkeypatch.setenv("LIP_ALLOC_CAP_FRACTION", "0.5")
    assert L.alloc_cap_fraction() == 0.5


def test_sequencer_per_sid():
    q = L.SidSequencer()
    assert q.check({"sid": 1, "seq": 1}) == "ok"
    assert q.check({"sid": 1, "seq": 2}) == "ok"
    assert q.check({"sid": 2, "seq": 1}) == "ok"
    assert q.check({"sid": 1, "seq": 2}) == "dup"
    assert q.check({"sid": 1, "seq": 5}) == "gap"
    assert q.check({"sid": None, "seq": 9}) == "ok"
    assert issubclass(L.SequenceGap, ConnectionError)
    assert L.SequenceGap in L.readonly_transient_types() or issubclass(L.SequenceGap, L.readonly_transient_types())


def _snap(market, ts):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": None, "ts": ts,
            "msg": {"market_ticker": market, "yes_dollars_fp": [["0.40", "50"]],
                    "no_dollars_fp": [["0.55", "50"]], "yes": [[40, 50]], "no": [[55, 50]]}}


def test_carry_forward_scores_quiet_seconds_only_when_resting():
    for carry, expect_more in ((True, True), (False, False)):
        loop = L.RunLoop(mode="paper", bankroll=5000, carry_forward=carry)
        _prog(loop, "KXA-1")
        loop.on_frame({"type": "clock", "ts": T0})
        loop.on_frame(_snap("KXA-1", T0 + 0.2))
        loop.resting["KXA-1"] = {"yes": 10.0, "no": 10.0, "yes_cents": 40, "no_cents": 55}
        loop.accruals["KXA-1"].set_resting(loop._orders("KXA-1", loop.resting["KXA-1"]))
        loop.on_frame({"type": "clock", "ts": T0 + 30})
        marks = loop.accruals["KXA-1"].marks
        counted = sum(1 for m in marks if m.counted)
        if expect_more:
            assert counted >= 29
        else:
            assert counted <= 1


def test_compaction_preserves_estimate():
    loop = L.RunLoop(mode="paper", bankroll=5000, carry_forward=True)
    _prog(loop, "KXA-1")
    acc = loop.accruals["KXA-1"]
    for i in range(500):
        acc.marks.append(SecondMark(i, "scored" if i % 3 else "missed", bool(i % 3),
                                    0.25 if i % 3 else None, 1.5 if i % 3 else 0.0, i % 7 == 0))
    before = acc.estimate()
    assert acc.compact(keep=50) == 450 and len(acc.marks) == 50
    after = acc.estimate()
    for field in ("known_seconds", "unknown_seconds", "forfeited_seconds", "intra_second_seconds",
                  "sum_snapshot_score"):
        assert getattr(before, field) == getattr(after, field), field
    assert abs(Decimal(before.raw_usd) - Decimal(after.raw_usd)) < Decimal("1e-20")


def test_min_hours_alias(monkeypatch):
    monkeypatch.setenv("LIP_MIN_HOURS_TO_CLOSE", "48")
    assert min_close_hours() == 48.0


def test_rank_prefers_durable_and_penalizes_news():
    frame = {"market": "KXA-1", "series": "KXA", "period_reward_usd": 50, "period_seconds": 86400,
             "target_size": 100}
    meta = {"yes_bid": 0.40, "yes_ask": 0.45, "yes_bid_size": 200, "yes_ask_size": 200,
            "volume_24h": 1000}
    long_ = S.rank_score(frame, meta, category="Economics", days=30)["score"]
    short = S.rank_score(frame, meta, category="Economics", days=2.5)["score"]
    news = S.rank_score(frame, meta, category="Entertainment", days=30)["score"]
    assert long_ > short and long_ > news


def test_sequencer_snapshot_skip_is_resync_not_gap():
    q = L.SidSequencer()
    assert q.check({"sid": 1, "seq": 105, "type": "orderbook_delta"}) == "ok"
    assert q.check({"sid": 1, "seq": 106, "type": "orderbook_delta"}) == "ok"
    assert q.check({"sid": 1, "seq": 110, "type": "orderbook_snapshot"}) == "ok"
    assert q.gaps == 0 and q.resyncs == 1
    assert q.check({"sid": 1, "seq": 111, "type": "orderbook_delta"}) == "ok"
    assert q.check({"sid": 1, "seq": 115, "type": "orderbook_delta"}) == "gap"


def test_rank_live_score_penalizes_markout():
    assert L.rank_live_score(10.0, 4.0, 100.0) == 0.06
    assert L.rank_live_score(10.0, 12.0, 100.0) < 0
    assert L.rank_live_score(10.0, 0.0, 0.0) == 0.0


def test_payable_floor_hides_small_raw_accrual():
    from decimal import Decimal
    from engine.lip_accrual import period_payout
    assert period_payout(Decimal("0.12")) == 0
    assert period_payout(Decimal("1.234")) == Decimal("1.23")


def test_carry_holding_model_does_not_crush_long_dated(monkeypatch):
    from mm import selector as SEL
    from mm.selector import KalshiMarket, quote_economics
    m = KalshiMarket(market="KXGADATACENTERS-26DEC31-T310", series="KXGADATACENTERS",
                     period_reward_usd=30.8, period_seconds=86400, seconds_left=86400,
                     discount_factor=0.5, target_size=100,
                     yes_bids=[(40, 300.0)], no_bids=[(44, 300.0)],
                     days_to_settle=91.6)
    monkeypatch.delenv("LIP_HOLDING_MODEL", raising=False)
    legacy = quote_economics(m, 100)[0]
    monkeypatch.setenv("LIP_HOLDING_MODEL", "carry")
    carry = quote_economics(m, 100)[0]
    assert carry > legacy + 20


def test_split_bucket_budgets_and_flow():
    assert L.split_bucket_budgets(1000, 0.5, True, True) == {"durable": 500.0, "short": 500.0}
    assert L.split_bucket_budgets(1000, 0.5, False, True) == {"durable": 0.0, "short": 1000.0}
    assert L.split_bucket_budgets(1000, 0.5, True, False) == {"durable": 1000.0, "short": 0.0}
    assert L.split_bucket_budgets(1000, 0.0, True, True) == {"durable": 0.0, "short": 1000.0}


def test_daily_summary_bucket_lines():
    from mm.unattended.health import render_daily_summary
    txt = render_daily_summary(day="d", fills=1, pnl_usd=0, rewards_usd=0, buckets={
        "durable": {"selected_n": 2, "capital_usd": 100, "raw_est_usd": 0.5, "fills_n": 1,
                    "markout_usd": -0.1, "pnl_usd": 0.4}})
    assert "bucket durable selected 2 capital_usd 100.00 raw_est_rewards_usd 0.5000 fills 1" in txt
