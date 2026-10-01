"""Patch 19: bounded compressed frame recorder + replay bench."""
import gzip
import os
import time

from mm.unattended import bookrec as R
from mm import replay_bench as B

T0 = 1_790_800_000.0
M = "KXA-26DEC-T1"


def _program(market=M):
    return {"kind": "program", "market": market, "series": market.split("-")[0],
            "period_reward_usd": 100.0, "period_seconds": 86400, "start_ts": T0 - 3600,
            "end_ts": T0 + 86400, "close_ts": T0 + 30 * 86400, "target_size": 1000,
            "days_from_close": True, "rank_score": 0.1, "exchange_index": 0}


def _snap(ts, yes, no, market=M):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": None, "ts": ts,
            "msg": {"market_ticker": market,
                    "yes_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in yes],
                    "no_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in no],
                    "yes": [[p, s] for p, s in yes], "no": [[p, s] for p, s in no]}}


def _trade(ts, tid, yes_c, count, taker, market=M):
    return {"type": "trade", "ts": ts, "trade": {
        "trade_id": tid, "market_ticker": market, "count": count, "taker_side": taker,
        "yes_price_dollars": f"{yes_c / 100:.2f}", "no_price_dollars": f"{(100 - yes_c) / 100:.2f}"}}


def _drain(rec, n, timeout=5.0):
    t = time.time()
    while rec.stats["frames"] < n and time.time() - t < timeout:
        time.sleep(0.02)


def test_recorder_writes_gzip_rotates_with_header(tmp_path):
    rec = R.FrameRecorder(str(tmp_path), max_gb=1, retention_days=14, rotate_s=3600,
                          rotate_mb=0.0005, flush_s=0, min_free_gb=0).start()
    rec.record(_program())
    for i in range(20):
        rec.record(_snap(T0 + i, [(40, 3000)], [(55, 3000)]))
    _drain(rec, 21)
    rec.stop()
    files = R.list_files(tmp_path)
    assert len(files) >= 2
    frames = list(R.iter_frames([files[-1]]))
    assert frames[0]["kind"] == "program" and frames[0]["hdr"] == 1  # header in every file
    allf = list(R.iter_frames(files))
    assert sum(1 for f in allf if f.get("type") == "orderbook_snapshot") == 20
    s = rec.summary()
    assert s["frames"] == 21 and s["dropped"] == 0 and s["files"] == len(files)


def test_retention_by_age_and_size(tmp_path):
    old = tmp_path / "frames-20260901T000000Z.jsonl.gz"
    big1 = tmp_path / "frames-20260930T000000Z.jsonl.gz"
    big2 = tmp_path / "frames-20261001T000000Z.jsonl.gz"
    for p in (old, big1, big2):
        p.write_bytes(b"x" * 1000)
    os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
    deleted = R.enforce_retention(tmp_path, max_bytes=1500, retention_s=14 * 86400)
    assert old.name in deleted and big1.name in deleted and big2.exists()


def test_iter_frames_tolerates_truncated_tail(tmp_path):
    p = tmp_path / "frames-x.jsonl.gz"
    with gzip.open(p, "wt") as fh:
        for i in range(50):
            fh.write('{"type":"clock","ts":%d}\n' % i)
    data = p.read_bytes()
    p.write_bytes(data[: len(data) - 8])  # chop the gzip trailer
    got = list(R.iter_frames([p]))
    assert len(got) == 50 or len(got) > 0


def test_replay_bench_quotes_fills_and_scores(tmp_path):
    rec = R.FrameRecorder(str(tmp_path), flush_s=0, min_free_gb=0).start()
    rec.record(_program())
    frames = [_snap(T0 + 1, [(40, 3000)], [(55, 3000)])]
    for i in range(2, 400, 5):
        frames.append(_snap(T0 + i, [(40, 3000)], [(55, 3000)]))
    # a taker buying NO prints through our 40c YES bid -> fill
    frames.append(_trade(T0 + 401, "t1", 39, 50, "no"))
    frames.append(_snap(T0 + 402, [(39, 3000)], [(55, 3000)]))
    for f in frames:
        rec.record(f)
    _drain(rec, len(frames) + 1)
    rec.stop()
    paths = R.list_files(tmp_path)
    env = {"LIP_SIZE_LADDER": "100", "LIP_HOLDING_MODEL": "carry"}
    r = B.run_one(paths, env, bankroll=5000, select_every=600, warmup_s=0)
    assert r["programs"] == 1 and r["selections"] >= 1 and r["quotes"] >= 1
    assert r["reward_raw_usd"] > 0 and r["fills"] == 1
    assert r["net_usd"] == round(r["reward_raw_usd"] + r["markout_end_usd"] - r["fees_usd"], 4)
    r2 = B.run_one(paths, dict(env, LIP_SKEW_ENABLE="1"), bankroll=5000, select_every=600, warmup_s=0)
    assert r2["skew"]["enabled"] is True and r2["fills"] == 1


def test_parse_policy_and_config(tmp_path):
    p = tmp_path / "policy.conf"
    p.write_text("[Service]\nEnvironment=LIP_A=1\n# x\nEnvironment=\"LIP_B=2,3\"\n")
    assert B.parse_policy(str(p)) == {"LIP_A": "1", "LIP_B": "2,3"}
    assert B.parse_config("skew:LIP_SKEW_ENABLE=1,LIP_SKEW_MAX_TICKS=3") == (
        "skew", {"LIP_SKEW_ENABLE": "1", "LIP_SKEW_MAX_TICKS": "3"})
