"""lipforge oct13 (paper): fill-drought fix. The fill-sampling group is
narrowed to cheap sides at the touch in liquid short-bucket markets
(LIP_SAMPLE_MAX_DAYS / LIP_SAMPLE_MIN_VOL_24H); KXRT denied."""
from mm.unattended import loop as L
from mm.selector import series_denylist
from tests.test_review_loop_pnl import (  # noqa: F401  (autouse env fixture)
    T0, _env, apply_policy, newloop, program, snap,
)

SHORT = "KXCHEAP-26OCT15-T5"
LONG = "KXCHEAP-26DEC31-T5"
BOOK_Y = [(5, 300), (4, 900)]
BOOK_N = [(93, 300), (92, 900)]


def _env_sample(mp, **kw):
    base = {"LIP_SAMPLE_ENABLE": "1", "LIP_SAMPLE_N": "10", "LIP_SAMPLE_MIN_TRADES_DAY": "20",
            "LIP_SAMPLE_MIN_CENTS": "1", "LIP_SAMPLE_MAX_CENTS": "9",
            "LIP_SAMPLE_MAX_DAYS": "14", "LIP_SAMPLE_MIN_VOL_24H": "500"}
    base.update(kw)
    for k, v in base.items():
        mp.setenv(k, v)


def _loop(markets):
    lp = newloop()
    for m, days, vol in markets:
        lp.on_frame(program(m, reward=50.0, close_ts=T0 + days * 86400, days_to_settle=days))
        lp.on_frame(snap(m, T0, BOOK_Y, BOOK_N))
        lp.trades_24h[m] = 100
        lp.programs[m].volume_24h = vol
    return lp


def test_sample_cfg_reads_new_knobs(monkeypatch):
    _env_sample(monkeypatch)
    cfg = L.sample_cfg()
    assert cfg["max_days"] == 14.0 and cfg["min_vol"] == 500.0 and cfg["max_cents"] == 9
    monkeypatch.delenv("LIP_SAMPLE_MAX_DAYS"); monkeypatch.delenv("LIP_SAMPLE_MIN_VOL_24H")
    cfg = L.sample_cfg()
    assert cfg["max_days"] == 0.0 and cfg["min_vol"] == 0.0     # defaults: no filter


def test_pick_sample_short_bucket_and_liquid_only(monkeypatch):
    _env_sample(monkeypatch)
    thin = "KXTHIN-26OCT15-T5"
    lp = _loop([(SHORT, 5, 2000.0), (LONG, 60, 2000.0), (thin, 5, 10.0)])
    picked = lp._pick_sample(list(lp._markets()), T0)
    assert picked == [SHORT]


def test_pick_sample_filters_off_by_default(monkeypatch):
    _env_sample(monkeypatch, LIP_SAMPLE_MAX_DAYS="0", LIP_SAMPLE_MIN_VOL_24H="0")
    lp = _loop([(SHORT, 5, 2000.0), (LONG, 60, 2000.0)])
    assert set(lp._pick_sample(list(lp._markets()), T0)) == {SHORT, LONG}


def test_sample_quotes_only_cheap_side_at_touch(monkeypatch):
    _env_sample(monkeypatch, LIP_SAMPLE_SIZE="20")
    lp = _loop([(SHORT, 5, 2000.0)])
    lp.sample_markets = {SHORT}
    lp.sample_reserve_usd = 25.0
    lp.sample_stats = {"enabled": True}
    lp._quote_sample(T0)
    r = lp.resting.get(SHORT)
    assert r is not None
    # YES quoted at the 5c touch, 20 contracts; the 93c NO side (outside 1-9c) is not
    assert r["yes"] == 20.0 and r["yes_cents"] == 5 and r["no"] == 0.0


def test_denied_series_never_sampled(monkeypatch):
    _env_sample(monkeypatch)
    monkeypatch.setenv("LIP_SERIES_DENYLIST", "KXCHEAP,KXRT")
    lp = _loop([(SHORT, 5, 2000.0)])
    assert lp._pick_sample(list(lp._markets()), T0) == []


def test_policy_denylist_includes_kxrt_and_sampling_narrowed(monkeypatch):
    apply_policy(monkeypatch, grok=True)
    assert series_denylist() >= {"KXRT", "KXHURCAT", "KXHURPATHAL", "KXTRUMPENDORSEMENTS", "KXNEXTTEAMNFL"}
    cfg = L.sample_cfg()
    assert cfg["enabled"] and cfg["max_cents"] <= 9 and cfg["max_days"] == 14.0
    # the 30-89c hard stop is still strict
    assert L.avoid_band_reduce_ok() is False
