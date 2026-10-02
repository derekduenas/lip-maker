"""Review fix: inventory = worst-case settlement loss of the unpaired legs,
not cumulative gross premium. A riskless paired book must not trip."""
import time

from mm.safety import lip_watchdog as wd


def _cfg(tmp_path, **extra):
    env = {"LIP_WD_STATE_DIR": str(tmp_path), "LIP_PAPER": "true"}
    env.update({k: str(v) for k, v in extra.items()})
    return wd.Config(env)


def _status(now, **kw):
    s = {"stage": "running", "session_elapsed_s": 3600.0, "last_frame_ts": now - 5,
         "paper_capital_usd": 100.0, "resting_n": 4, "kill": None, "live_armed": False,
         "buckets": {"short": {"markout_usd": 0.0, "premium_usd": 600.0, "raw_est_usd": 0.0}}}
    s.update(kw)
    return s


def test_paired_book_from_engine_markouts_does_not_trip(tmp_path):
    # 612 YES@49 + 612 NO@49: $600 gross premium, zero unpaired
    cfg = _cfg(tmp_path); now = time.time(); cfg.heartbeat.write_text(f"{now}\n")
    st = _status(now, markouts={"unpaired_usd": 0.0})
    h = wd.tick(cfg, now=now, status_fn=lambda _c: st)
    assert not h["latched"], h["reasons_now"]
    assert h["info"]["inventory_usd"] == 0.0


def test_positions_paired_vs_unpaired(tmp_path):
    st = {"positions": {"M1": {"yes": 612, "no": 612, "yes_cost": 299.88, "no_cost": 299.88}}}
    inv = wd.inventory_breakdown(st)
    assert inv["inventory_usd"] == 0.0
    assert abs(inv["paired_locked_usd"] - 599.76) < 1e-6
    st = {"positions": {"M1": {"yes": 1000, "no": 0, "yes_cost": 600.0, "no_cost": 0.0}}}
    assert abs(wd.inventory_breakdown(st)["inventory_usd"] - 600.0) < 1e-6
    # 100 pairs at 60+50c lock a $10 loss, plus 50 unpaired YES at 60c ($30)
    st = {"positions": {"M1": {"yes": 150, "no": 100, "yes_cost": 90.0, "no_cost": 50.0}}}
    inv = wd.inventory_breakdown(st)
    assert abs(inv["unpaired_usd"] - 30.0) < 1e-6
    assert abs(inv["paired_locked_loss_usd"] - 10.0) < 1e-6
    assert abs(inv["inventory_usd"] - 40.0) < 1e-6


def test_fills_list_used_when_present(tmp_path):
    fills = ([{"market_ticker": "A", "side": "yes", "count": 500, "price_cents": 49}]
             + [{"market_ticker": "A", "side": "no", "count": 500, "price_cents": 49}]
             + [{"market_ticker": "B", "side": "no", "count": 100, "price_cents": 30}])
    inv = wd.inventory_breakdown({"fills": fills})
    assert inv["basis"] == "fills"
    assert abs(inv["inventory_usd"] - 30.0) < 1e-6
    assert abs(inv["paired_locked_usd"] - 490.0) < 1e-6


def test_unpaired_book_still_trips(tmp_path):
    cfg = _cfg(tmp_path); now = time.time(); cfg.heartbeat.write_text(f"{now}\n")
    st = _status(now, markouts={"unpaired_usd": 650.0})
    h = wd.tick(cfg, now=now, status_fn=lambda _c: st)
    assert h["latched"] and h["trip_reasons"][0].startswith("inventory")


def test_gross_fallback_when_no_granular_data(tmp_path):
    inv = wd.inventory_breakdown({"buckets": {"s": {"premium_usd": 600.0}}})
    assert inv["basis"] == "gross_premium_upper_bound" and inv["inventory_usd"] == 600.0
    # new engine key name preferred over the (now real-P&L) pnl_usd
    inv = wd.inventory_breakdown({"premium_paid_usd": "120", "pnl_usd": "-5"})
    assert inv["inventory_usd"] == 120.0
    # old engine: pnl_usd was -premium
    inv = wd.inventory_breakdown({"pnl_usd": "-75"})
    assert inv["inventory_usd"] == 75.0
    assert wd.inventory_breakdown({}) is None
