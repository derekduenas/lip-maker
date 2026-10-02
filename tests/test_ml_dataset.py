"""mm.ml.dataset / mm.ml.harvest: point-in-time features, labels, leakage."""
import copy
import gzip
import json

import pytest

from mm.ml import dataset as D
from mm.ml import harvest as H

M = "KXTEST-26OCT10-T1"
T0 = 1_790_000_000.0


def snap(ets, yes, no, m=M, lag=1.0):
    return {"type": "orderbook_snapshot", "ts": ets + lag, "exchange_ts": ets,
            "msg": {"market_ticker": m, "yes_dollars_fp": [[f"{p/100:.4f}", f"{s:.2f}"] for p, s in yes],
                    "no_dollars_fp": [[f"{p/100:.4f}", f"{s:.2f}"] for p, s in no]}}


def delta(ets, side, price, d, m=M, lag=1.0):
    return {"type": "orderbook_delta", "ts": ets + lag,
            "msg": {"market_ticker": m, "price_dollars": f"{price/100:.4f}", "delta_fp": f"{d:.2f}",
                    "side": side, "ts_ms": int(round(ets * 1000))}}


def trade(ets, taker, yes_c, count=10, m=M, lag=1.0, tid=None):
    return {"type": "trade", "ts": ets + lag,
            "trade": {"trade_id": tid or f"t{ets}", "market_ticker": m, "yes_price_dollars": f"{yes_c/100:.4f}",
                      "no_price_dollars": f"{(100-yes_c)/100:.4f}", "count_fp": f"{count:.2f}",
                      "taker_side": taker, "ts_ms": int(round(ets * 1000))}}


def program(m=M):
    return {"kind": "program", "market": m, "series": "KXTEST", "category": "Test", "period_reward_usd": 50.0,
            "period_seconds": 86400.0, "target_size": 100.0, "discount_factor": 0.5,
            "close_ts": T0 + 86400, "end_ts": T0 + 43200, "occurrence_ts": None, "fee_type": "quadratic"}


def base_stream():
    """Book 40/55 (yes bid 40, no bid 55 -> yes ask 45, yes mid 42.5)."""
    fr = [program(), snap(T0, [(40, 100), (39, 50)], [(55, 80), (54, 20)])]
    for i in range(1, 30):
        fr.append(delta(T0 + i, "yes", 39, 1))
    fr.append(trade(T0 + 100, "no", 40, 10))      # maker bought YES at 40
    return fr


def future(after, yes_bid, no_bid):
    """Frames after ``after`` that move the book (the future)."""
    fr = []
    for k in range(1, 40):
        t = after + k * 60
        fr.append(snap(t, [(yes_bid, 500)], [(no_bid, 500)]))
        fr.append(trade(t + 1, "yes", yes_bid + 1, 99, tid=f"f{t}"))
    return fr


def build(frames, **kw):
    rows, b, _snap = D.build_stream(frames, (), **kw)
    return rows, b


def proxy_row(rows, cutoff):
    return [r for r in rows if r["source"] == "trade_proxy" and abs(r["cutoff"] - cutoff) < 1e-6][0]


FEATS = list(D.FEATURES_NUM) + list(D.FEATURES_CAT)


def test_features_do_not_depend_on_the_future():
    a = base_stream() + future(T0 + 100, 20, 75)      # crash
    b = base_stream() + future(T0 + 100, 70, 25)      # rally
    ra, rb = build(a)[0], build(b)[0]
    fa, fb = proxy_row(ra, T0 + 100), proxy_row(rb, T0 + 100)
    for k in FEATS:
        assert fa.get(k) == fb.get(k), k
    # ... while the labels do see the future
    assert fa["markout_c_5m"] < 0 < fb["markout_c_5m"]
    assert fa["toxic"] == 1 and fb["toxic"] == 0


def test_point_in_time_values():
    rows = build(base_stream() + future(T0 + 100, 30, 65))[0]
    r = proxy_row(rows, T0 + 100)
    assert r["side"] == "yes" and r["price_c"] == 40.0
    assert r["side_mid_c"] == 42.5 and r["spread_c"] == 5.0 and r["dist_mid_c"] == 2.5
    assert r["own_top_sz"] == 100.0 and r["opp_top_sz"] == 80.0
    assert r["category"] == "Test" and r["venue"] == "kalshi"
    assert r["hours_to_close"] == pytest.approx((86400 - 100) / 3600.0)
    assert r["feat_max_ts"] < r["cutoff"]
    assert r["trades_5m"] == 0.0           # the print itself is not a feature


def test_delta_caused_by_print_with_same_ts_is_excluded():
    # Kalshi sends the delta the print caused first, stamped with the print's ts_ms
    fr = base_stream()[:-1]
    fr.append(delta(T0 + 100, "yes", 40, -100, lag=0.9))      # bid 40 wiped by the print
    fr.append(trade(T0 + 100, "no", 40, 100, lag=1.0))
    fr += future(T0 + 100, 30, 65)
    r = proxy_row(build(fr)[0], T0 + 100)
    assert r["own_top_sz"] == 100.0 and r["side_mid_c"] == 42.5


def test_event_time_reordering():
    fr = base_stream()[:-1]
    # arrives AFTER the print but happened before it: included
    late_past = delta(T0 + 99, "no", 55, 20, lag=5.0)
    # arrives BEFORE the print but happened after it: excluded
    early_future = delta(T0 + 101, "yes", 40, 900, lag=-0.5)
    fr += [early_future, trade(T0 + 100, "no", 40, 10, lag=1.0), late_past]
    fr += future(T0 + 101, 30, 65)
    fr.sort(key=lambda f: f.get("ts", 0))                          # receive order
    r = proxy_row(build(fr)[0], T0 + 100)
    assert r["opp_top_sz"] == 100.0      # 80 + 20 from the late-arriving past delta
    assert r["own_top_sz"] == 100.0      # +900 future delta not seen


def test_label_horizons_use_state_as_of_due_time():
    fr = base_stream()
    fr.append(snap(T0 + 100 + 59, [(46, 10)], [(50, 10)]))   # yes mid 48 at +59 s
    fr.append(snap(T0 + 100 + 61, [(10, 10)], [(85, 10)]))   # +61 s: after the 1 m mark
    fr.append(snap(T0 + 100 + 299, [(30, 10)], [(60, 10)]))  # mid 35 for 5 m
    fr.append(snap(T0 + 100 + 1799, [(44, 10)], [(54, 10)])) # mid 45 for 30 m
    fr.append(snap(T0 + 100 + 1900, [(44, 10)], [(54, 10)]))
    r = proxy_row(build(fr)[0], T0 + 100)
    assert r["markout_c_1m"] == 8.0
    assert r["markout_c_5m"] == -5.0 and r["toxic"] == 1
    assert r["markout_c_30m"] == 5.0
    assert r["markout_usd_5m"] == pytest.approx(-0.5)


def test_settlement_before_horizon_labels_at_settlement_value():
    fr = base_stream() + [{"kind": "settlement", "ts": T0 + 130, "market": M, "result": "no"},
                          snap(T0 + 3000, [(99, 1)], [(1, 1)])]
    r = proxy_row(build(fr)[0], T0 + 100)
    assert r["markout_c_1m"] == -40.0 and r["markout_c_30m"] == -40.0


def test_unlabeled_when_data_ends():
    r = proxy_row(build(base_stream() + [snap(T0 + 200, [(40, 1)], [(55, 1)])])[0], T0 + 100)
    assert r["markout_c_1m"] is not None and r["markout_c_5m"] is None and r["toxic"] is None


def test_live_fill_matched_to_print_uses_exchange_time():
    fr = base_stream() + future(T0 + 100, 30, 65)
    live = [{"ts": T0 + 101.0, "market": M, "side": "yes", "price_cents": 40, "count": 5, "synthetic": False}]
    rows = build(fr, live_fills=live)[0]
    lr = [r for r in rows if r["source"] == "live"][0]
    assert lr["cutoff_mode"] == "matched_print" and lr["cutoff"] == pytest.approx(T0 + 100)
    pr = proxy_row(rows, T0 + 100)
    for k in FEATS:
        if k != "count":
            assert lr.get(k) == pr.get(k), k
    assert lr["markout_usd_5m"] == pytest.approx(5 * lr["markout_c_5m"] / 100)


def test_unmatched_live_fill_uses_conservative_cutoff():
    fr = base_stream() + future(T0 + 100, 30, 65)
    live = [{"ts": T0 + 400.0, "market": M, "side": "no", "price_cents": 55, "count": 2, "synthetic": False}]
    lr = [r for r in build(fr, live_fills=live)[0] if r["source"] == "live"][0]
    assert lr["cutoff_mode"] == "conservative" and lr["cutoff"] == pytest.approx(T0 + 400 - D.LIVE_CONSERVATIVE_S)
    assert lr["feat_max_ts"] < lr["cutoff"]


def test_pmus_synthetic_print_uses_previous_poll():
    pm = "PMUS:abc-x"

    def poll(t, yb, nb):
        return {"type": "orderbook_snapshot", "ts": t, "venue": "pmus",
                "msg": {"market_ticker": pm, "yes_dollars_fp": [[f"{yb/100:.4f}", "10"]],
                        "no_dollars_fp": [[f"{nb/100:.4f}", "10"]]}}
    syn = {"type": "trade", "ts": T0 + 10.2, "venue": "pmus", "synthetic": True,
           "trade": {"trade_id": "pmus:abc-x:1", "ticker": pm, "count": 5.0, "yes_price_dollars": "0.4000",
                     "no_price_dollars": "0.6000", "taker_side": "no", "synthetic": True}}
    fr = [poll(T0, 40, 55), poll(T0 + 10.1, 30, 65), syn] + [poll(T0 + 10 + k * 30, 30, 65) for k in range(1, 70)]
    r = proxy_row(build(fr)[0], T0 + 10.2)
    assert r["synthetic"] is True and r["side_mid_c"] == 42.5     # poll before the print
    assert r["feat_max_ts"] == T0


def test_replay_journal_rows_and_quote_features():
    fr = base_stream() + future(T0 + 100, 30, 65)
    rep = [{"source": "replay", "market": M, "side": "yes", "price": 40.0, "count": 3.0, "cutoff": T0 + 100,
            "quote_ts": T0 + 50, "quote_best0": [40, 49], "inventory": 2.0, "synthetic": False, "jid": "r1"},
           {"source": "quote_bg", "market": M, "side": "no", "price": 54.0, "count": 3.0, "cutoff": T0 + 80,
            "quote_ts": T0 + 20, "quote_best0": [40, 55], "inventory": 0.0, "filled_60s": 0, "jid": "b1"}]
    rows = build(fr, replay_rows=rep)[0]
    rr = [r for r in rows if r["source"] == "replay"][0]
    assert rr["quote_age_s"] == 50.0 and rr["inventory_side"] == 2.0 and rr["fast_move_quote"] == 1.0
    bg = [r for r in rows if r["source"] == "quote_bg"][0]
    assert bg["filled_60s"] == 0 and bg["fast_move_quote"] == 0.0


def test_leakage_guard_rejects_samples_after_market_data():
    b = D.Builder()
    for f in base_stream():
        b.feed(f)
    b.drain(D.INF)
    n_open = len(b.open)
    b._sample(T0 + 20, {"source": "live", "market": M, "side": "yes", "price": 40, "count": 1})
    assert b.stats["late_request"] == 1 and len(b.open) == n_open


def test_every_row_feature_time_strictly_before_cutoff():
    fr = base_stream() + future(T0 + 100, 30, 65)
    rows = build(fr)[0]
    assert len(rows) > 10
    assert all(r["feat_max_ts"] is None or r["feat_max_ts"] < r["cutoff"] for r in rows)


def _write(path, frames):
    with gzip.open(path, "wt") as fh:
        for f in frames:
            fh.write(json.dumps(f) + "\n")


def test_incremental_build_with_lookahead_and_checkpoint(tmp_path):
    rec, out = tmp_path / "rec", tmp_path / "out"
    rec.mkdir()
    s = base_stream()
    f2 = future(T0 + 100, 30, 65)
    _write(rec / "frames-20261001T000000Z.jsonl.gz", s)
    _write(rec / "frames-20261001T010000Z.jsonl.gz", f2[:40])
    _write(rec / "frames-20261001T020000Z.jsonl.gz", f2[40:])
    res = D.build(str(rec), str(out), log=lambda *_: None)
    assert res["files_processed"] == 2            # newest file is look-ahead only
    rows = D.load_samples(str(out))
    r = proxy_row(rows, T0 + 100)
    assert r["markout_c_5m"] is not None
    assert D.build(str(rec), str(out), log=lambda *_: None)["files_processed"] == 0
    keys = [(x["market"], x["cutoff"]) for x in rows]
    assert len(keys) == len(set(keys))


def test_readiness_gate():
    def row(day, toxic=0, syn=False, src="live"):
        return {"source": src, "synthetic": syn, "toxic": toxic, "day": day, "venue": "kalshi", "category": "X"}
    rows = [row(f"2026-10-0{1 + i % 7}") for i in range(299)]
    assert D.readiness(rows)["status"] == "NOT_READY"
    assert D.readiness(rows + [row("2026-10-01", 1)])["status"] == "READY"
    few_days = [row("2026-10-01") for _ in range(400)]
    assert D.readiness(few_days)["status"] == "NOT_READY"
    proxy = [row(f"2026-10-0{1 + i % 7}", src="trade_proxy") for i in range(1000)]
    syn = [row(f"2026-10-0{1 + i % 7}", syn=True) for i in range(1000)]
    rep = D.readiness(proxy + syn)
    assert rep["status"] == "NOT_READY" and rep["groups"]["live_synthetic"]["n"] == 1000


def test_harvest_log_parse_and_dedupe():
    line = ("2026-10-02 21:19:19,963 INFO lip.risk paper fill KXRT-VER-35 yes 12@41c mid 42.5 unpaired_yes 12")
    r = H.parse_log_line(line)
    assert r["market"] == "KXRT-VER-35" and r["count"] == 12 and r["price_cents"] == 41 and r["mid0"] == 42.5
    assert r["synthetic"] is False and r["ts_source"] == "log"
    st = H.from_status({"fills_detail": [{"market": "KXRT-VER-35", "side": "yes", "price_cents": 41.0,
                                          "count": 12.0, "ts": r["ts"] + 0.4, "synthetic": False}]})
    added = H.merge([], [r] + st)
    assert len(added) == 1 and added[0]["ts_source"] == "status"
    assert H.merge(added, [r]) == []
    pm = H.parse_log_line("2026-10-02 21:19:20,000 INFO lip.risk paper fill PMUS:abc no 3@60c mid None unpaired_yes -3")
    assert pm["synthetic"] is True and pm["mid0"] is None
