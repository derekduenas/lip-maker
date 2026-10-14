"""lipforge oct14 bug fix (paper; freeze-safe): KeyError 'fees_usd' in
_drop_settled. A paper exit created bucket_closed[bucket] = {"markout_usd": x};
pruning a settled position later did agg["fees_usd"] += ... on that bare row."""
import json

from mm.unattended import loop as L
from tests.test_review_loop_pnl import T0, _env, newloop  # noqa: F401

M = "KXOLD-26OCT01-T1"


def _old_settled(lp, bucket="short"):
    lp.position[M] = {"yes": 10.0, "no": 0.0, "yes_cost": 4.0, "no_cost": 0.0, "fees": 0.1, "venue": "kalshi"}
    lp.bucket_pos.setdefault(bucket, {})[M] = {"yes": 10.0, "no": 0.0, "yes_cost": 4.0, "no_cost": 0.0,
                                               "fees": 0.1, "fills_n": 3.0}
    lp.settled[M] = {"result": "yes", "ts": T0 - 30 * 86400, "source": "ws_lifecycle"}


def test_drop_settled_after_bare_paper_exit_row_does_not_raise():
    lp = newloop()
    lp.bucket_closed = {"short": {"markout_usd": 1.5}}      # exactly what live state held
    _old_settled(lp)
    lp._drop_settled(T0)                                    # raised KeyError: 'fees_usd'
    agg = lp.bucket_closed["short"]
    assert M not in lp.settled and M not in lp.position and M not in lp.bucket_pos["short"]
    assert agg["markout_usd"] == 1.5 + 6.0
    assert agg["fees_usd"] == 0.1 and agg["premium_usd"] == 4.0 and agg["fills_n"] == 3.0
    assert set(L.BUCKET_CLOSED_KEYS) <= set(agg)


def test_paper_exit_aggregate_row_is_complete():
    lp = newloop()
    agg = lp._bucket_closed_agg("durable")
    agg["markout_usd"] += 2.0
    assert set(L.BUCKET_CLOSED_KEYS) <= set(lp.bucket_closed["durable"])


def test_state_load_normalizes_bare_rows(tmp_path):
    lp = newloop()
    path = tmp_path / "engine_state.json"
    lp.attach_state(str(path))
    lp.save_state(force=True)
    data = json.loads(path.read_text())
    data["bucket_closed"] = {"short": {"markout_usd": 56.2}, "durable": {"markout_usd": -22.9}}
    data["bucket_pos"] = {"short": {M: {"yes": 10.0, "no": 0.0, "yes_cost": 4.0, "no_cost": 0.0}}}
    data["position"] = {M: {"yes": 10.0, "no": 0.0, "yes_cost": 4.0, "no_cost": 0.0}}
    data["settled"] = {M: {"result": "yes", "ts": T0 - 30 * 86400}}
    data["series_acc"] = {"KXOLD": {"fills": 3, "fees": 0.1}}   # older schema: no mk5/settled keys
    path.write_text(json.dumps(data))
    lp2 = newloop()
    lp2.attach_state(str(path))
    assert set(L.BUCKET_CLOSED_KEYS) <= set(lp2.bucket_closed["short"])
    assert lp2.bucket_closed["short"]["markout_usd"] == 56.2
    assert set(L.BUCKET_POS_KEYS) <= set(lp2.bucket_pos["short"][M])
    assert lp2.series_acc["KXOLD"]["fills"] == 3 and lp2.series_acc["KXOLD"]["mk5_n"] == 0
    lp2._drop_settled(T0)
    assert M not in lp2.settled and lp2.bucket_closed["short"]["premium_usd"] == 4.0
