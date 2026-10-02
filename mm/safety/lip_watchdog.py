"""lip-watchdog: independent kill switch for the lip-maker engine (patch 17).

Runs as its own systemd service (``lip-watchdog``), separate from
``lip-unattended``. Standard library only for the checks; the live
cancel-all path lazily imports ``cryptography`` to sign Kalshi requests.

Every ``LIP_WD_INTERVAL_S`` (30s) it checks:
  * heartbeat file age            > LIP_WD_HEARTBEAT_STALE_S (90)
  * /status unreachable           LIP_WD_STATUS_FAILS (2) consecutive failures
  * market-data feed stale        now - status.last_frame_ts > LIP_WD_FEED_STALE_S (120)
                                  (skipped for LIP_WD_GRACE_S (240) after an engine session starts)
  * daily P&L                     < -abs(LIP_WD_DAILY_LOSS) (50). P&L = sum over buckets of
                                  markout_usd - fees_usd + rebates_usd (MTM of fills net of Kalshi
                                  maker fees and PM US rebates; rewards excluded unless
                                  LIP_WD_PNL_INCLUDE_REWARDS=1),
                                  re-based at each UTC day and on engine restart.
  * inventory                     worst-case settlement loss of unpaired legs (+ locked loss on
                                  pairs costing > $1) > LIP_WD_MAX_INVENTORY_USD (500); paired
                                  YES+NO is riskless and not counted. Source, most granular first:
                                  status positions / fills / markouts.unpaired_usd, else gross
                                  premium as an upper bound (see inventory_breakdown)
  * capital                       paper_capital_usd > LIP_WD_MAX_CAPITAL_USD (1500 if unset; must be
                                  >= the engine's max planned budget -- deploy/apex/watchdog.env.example
                                  sets 1600 against the engine's pinned LIP_BANKROLL=1500); per venue
                                  LIP_WD_MAX_CAPITAL_KALSHI_USD (1600) / LIP_WD_MAX_CAPITAL_PMUS_USD (400)
  * resting orders                resting_n > LIP_WD_MAX_RESTING (200)
  * engine-reported kill          status.kill set (alert only, not a trip by default)
  * live mismatch                 status says live_armed (or mode live) but this watchdog's own
                                  config is not armed -> TRIP + loud alert (it could not cancel)

On trip (latched until ``--reset``):
  1. write the kill file (LIP_KILL_FILE, /var/lib/lip-maker/KILL); the engine
     polls it (mm/unattended/service.py) and latches external_kill, cancelling
     all quotes. If the kill file cannot be written, run LIP_WD_STOP_CMD
     (default ``systemctl stop lip-unattended``; needs a polkit rule or
     sudoers entry for user lip, see deploy/apex/README.md) every tick until
     it can, with TRIP alerts on both outcomes -- never a silent continue;
  2. cancel-all resting orders via the Kalshi API -- ONLY when the watchdog's
     own config is armed (LIP_WD_LIVE_ARMED=true and LIP_PAPER!=true in its
     env) AND one of: status says live_armed, the persisted
     ``engine_seen_live`` flag is set (any earlier status showed live; cleared
     only by --reset; a corrupt state file counts as set), or /status is
     unreachable (dead/hung engine). Config paper/unarmed -> never a real API
     write, logged as a no-op. Scope LIP_WD_CANCEL_SCOPE=ours (default:
     only client_order_id starting with LIP_WD_COID_PREFIX, "LIP-"; the
     account is shared with the Weather engine) or all. Uses Cancel Order V2
     with each order's market_ticker/exchange_index. Idempotent; retried
     every tick until the API shows zero in-scope resting orders
     (fail-closed: latch stays, alerts repeat);
  3. alert (alerts.log + alert JSON; optional ntfy push), rate-limited.

Watchdog self-failure: a corrupt state file loads as latched (and as
engine_seen_live). An exception in a tick alerts (WARN); LIP_WD_TICK_FAILS (3)
consecutive failures latch the watchdog itself (watchdog_tick_failing) and run
the same kill-file / stop / cancel-all actions. systemd restarts the process
if it dies (Restart=always).

Reset:  python -m mm.safety.lip_watchdog --reset   (then restart lip-unattended)
        Clears the latch, kill file and the persisted engine_seen_live flag.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("lip.watchdog")

PROD_REST = "https://api.elections.kalshi.com/trade-api/v2"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _num(env, key, default):
    try:
        return float(env.get(key, default))
    except (TypeError, ValueError):
        return float(default)


class Config:
    def __init__(self, env=None):
        env = dict(os.environ if env is None else env)
        self.env = env
        sd = Path(env.get("LIP_WD_STATE_DIR", "/var/lib/lip-maker"))
        self.state_dir = sd
        self.heartbeat = Path(env.get("LIP_WD_HEARTBEAT", sd / "heartbeat"))
        self.status_url = env.get("LIP_WD_STATUS_URL", "http://127.0.0.1:8765/status")
        self.kill_file = Path(env.get("LIP_KILL_FILE", sd / "KILL"))
        self.state_file = Path(env.get("LIP_WD_STATE_FILE", sd / "watchdog_state.json"))
        self.health_file = Path(env.get("LIP_WD_HEALTH_FILE", sd / "watchdog_health.json"))
        self.alerts_log = Path(env.get("LIP_WD_ALERTS_LOG", sd / "alerts.log"))
        self.alert_json = Path(env.get("LIP_WD_ALERT_JSON", sd / "alert.json"))
        self.interval_s = _num(env, "LIP_WD_INTERVAL_S", 30)
        self.hb_stale_s = _num(env, "LIP_WD_HEARTBEAT_STALE_S", 90)
        self.status_fails = int(_num(env, "LIP_WD_STATUS_FAILS", 2))
        self.status_timeout_s = _num(env, "LIP_WD_STATUS_TIMEOUT_S", 5)
        self.feed_stale_s = _num(env, "LIP_WD_FEED_STALE_S", 120)
        self.grace_s = _num(env, "LIP_WD_GRACE_S", 240)
        self.daily_loss = -abs(_num(env, "LIP_WD_DAILY_LOSS", 50))
        self.include_rewards = _truthy(env.get("LIP_WD_PNL_INCLUDE_REWARDS"))
        self.max_inventory = _num(env, "LIP_WD_MAX_INVENTORY_USD", 500)
        self.max_capital = _num(env, "LIP_WD_MAX_CAPITAL_USD", 1500)
        self.max_resting = _num(env, "LIP_WD_MAX_RESTING", 200)
        # Patch 21: per-venue coverage (status `venues` / `pmus`).
        self.max_capital_kalshi = _num(env, "LIP_WD_MAX_CAPITAL_KALSHI_USD", 1600)
        self.max_capital_pmus = _num(env, "LIP_WD_MAX_CAPITAL_PMUS_USD", 400)
        self.pmus_stale_s = _num(env, "LIP_WD_PMUS_STALE_S", 120)
        self.trip_on_engine_kill = _truthy(env.get("LIP_WD_TRIP_ON_ENGINE_KILL"))
        self.alert_min_interval_s = _num(env, "LIP_WD_ALERT_MIN_INTERVAL_S", 600)
        self.alert_max_per_hour = int(_num(env, "LIP_WD_ALERT_MAX_PER_HOUR", 12))
        self.ntfy_topic = (env.get("LIP_WD_NTFY_TOPIC") or "").strip()
        self.ntfy_server = env.get("LIP_WD_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        # The watchdog's OWN arming config. LIP_PAPER here comes from the
        # watchdog's env file, not the engine's unit drop-in: the two can
        # disagree, which evaluate() trips on (engine_live_watchdog_unarmed).
        self.live_flag = _truthy(env.get("LIP_WD_LIVE_ARMED"))
        self.paper_env = _truthy(env.get("LIP_PAPER", "true"))
        self.kalshi_rest = env.get("LIP_WD_KALSHI_REST", PROD_REST).rstrip("/")
        self.kalshi_key_id = env.get("LIP_WD_KALSHI_KEY_ID", "")
        self.kalshi_key_path = env.get("LIP_WD_KALSHI_KEY_PATH", "")
        # The Kalshi key/account is shared with the Weather engine
        # (execution/kalshi_auth.py). "ours" cancels only orders whose
        # client_order_id starts with one of these prefixes (the engine's
        # Kalshi adapter and quote manager use "LIP-"); "all" cancels
        # every resting order on the account.
        self.coid_prefixes = tuple(p.strip() for p in env.get("LIP_WD_COID_PREFIX", "LIP-").split(",")
                                   if p.strip()) or ("LIP-",)
        scope = str(env.get("LIP_WD_CANCEL_SCOPE", "ours")).strip().lower()
        if scope not in ("ours", "all"):
            log.warning("LIP_WD_CANCEL_SCOPE=%r invalid; using 'ours'", scope)
            scope = "ours"
        self.cancel_scope = scope
        # Fallback when the kill file cannot be written. Needs privileges the
        # `lip` user does not have by default: see deploy/apex/README.md.
        self.stop_cmd = env.get("LIP_WD_STOP_CMD", "systemctl stop lip-unattended")
        self.tick_fails = max(1, int(_num(env, "LIP_WD_TICK_FAILS", 3)))


# ----------------------------------------------------------------- io helpers
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_state(cfg: Config) -> dict:
    try:
        return json.loads(cfg.state_file.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        # Corrupt state: fail closed -- treat as latched so a human looks, and
        # assume the engine may have been live (the persisted flag is lost).
        return {"latched": True, "reasons": ["watchdog_state_corrupt"], "tripped_at": time.time(),
                "engine_seen_live": "unknown:state_corrupt"}


def save_state(cfg: Config, state: dict) -> None:
    _atomic_write(cfg.state_file, json.dumps(state, indent=1, sort_keys=True, default=str))


def fetch_status(cfg: Config):
    req = urllib.request.Request(cfg.status_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=cfg.status_timeout_s) as r:
        return json.loads(r.read().decode("utf-8"))


def read_heartbeat(cfg: Config):
    try:
        return float(cfg.heartbeat.read_text().strip().split()[0])
    except Exception:
        return None


# ----------------------------------------------------------------- trip logic
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def session_pnl(status: dict, include_rewards: bool = False):
    """MTM P&L of the current engine session from status buckets:
    markout_usd - fees_usd + rebates_usd per bucket (fee/rebate fields are
    used when present; older engines report markout only), plus raw_est_usd
    with ``include_rewards``."""
    b = status.get("buckets")
    if isinstance(b, dict) and b:
        tot = 0.0
        for row in b.values():
            if not isinstance(row, dict):
                continue
            tot += _f(row.get("markout_usd")) or 0.0
            tot -= _f(row.get("fees_usd")) or 0.0
            tot += _f(row.get("rebates_usd")) or 0.0
            if include_rewards:
                tot += _f(row.get("raw_est_usd")) or 0.0
        return tot
    return None


def _market_worst_case(yes, no, yes_cost, no_cost):
    """(unpaired_usd, paired_locked_usd, paired_locked_loss_usd) for one market.

    ``paired`` = min(yes, no) contracts pay exactly $1 per pair at settlement
    whichever side wins, so they are riskless apart from a locked loss when
    the pair cost more than $1. The unpaired leg loses its whole cost if it
    settles against us. Costs are split at each leg's average price.
    """
    paired = min(yes, no)
    avg_y = yes_cost / yes if yes > 0 else 0.0
    avg_n = no_cost / no if no > 0 else 0.0
    locked = paired * (avg_y + avg_n)
    locked_loss = max(0.0, locked - paired)
    unpaired = (yes - paired) * avg_y + (no - paired) * avg_n
    return unpaired, locked, locked_loss


def inventory_breakdown(status: dict):
    """Worst-case settlement loss of held inventory, most granular source first.

    1. ``positions`` {market: {yes, no, yes_cost, no_cost}} (per-market legs);
    2. ``fills`` (full fill list; buys, cost = count * price_cents / 100);
    3. ``markouts.unpaired_usd`` -- the engine's own per-market unpaired
       contracts x that leg's average cost (same definition, computed from
       its full position; paired capital is not reported there);
    4. otherwise gross premium of every fill (``premium_paid_usd``, bucket
       ``premium_usd``, or legacy ``-pnl_usd`` from engines before
       premium_paid_usd existed): an upper bound that ignores pairing.

    ``inventory_usd`` = unpaired leg cost + locked loss on pairs bought for
    more than $1 (a locked gain is never netted against it).
    """
    if not isinstance(status, dict):
        return None
    pos = status.get("positions")
    legs = None
    basis = None
    if isinstance(pos, dict) and pos:
        legs, basis = {}, "positions"
        for m, r in pos.items():
            if isinstance(r, dict):
                legs[m] = [_f(r.get("yes")) or 0.0, _f(r.get("no")) or 0.0,
                           _f(r.get("yes_cost")) or 0.0, _f(r.get("no_cost")) or 0.0]
    elif isinstance(status.get("fills"), list):
        legs, basis = {}, "fills"
        for f in status["fills"]:
            if not isinstance(f, dict) or f.get("side") not in ("yes", "no"):
                continue
            n = _f(f.get("count")) or 0.0
            px = _f(f.get("price_cents")) or 0.0
            leg = legs.setdefault(str(f.get("market_ticker") or f.get("market") or ""), [0.0, 0.0, 0.0, 0.0])
            i = 0 if f["side"] == "yes" else 1
            leg[i] += n
            leg[2 + i] += n * px / 100.0
    if legs is not None:
        unpaired = locked = locked_loss = 0.0
        for y, n, yc, nc in legs.values():
            u, lk, ll = _market_worst_case(y, n, yc, nc)
            unpaired += u; locked += lk; locked_loss += ll
        return {"basis": basis, "inventory_usd": round(unpaired + locked_loss, 6),
                "unpaired_usd": round(unpaired, 6), "paired_locked_usd": round(locked, 6),
                "paired_locked_loss_usd": round(locked_loss, 6)}
    mk = status.get("markouts")
    up = _f(mk.get("unpaired_usd")) if isinstance(mk, dict) else None
    if up is not None:
        return {"basis": "engine_unpaired_usd", "inventory_usd": up, "unpaired_usd": up,
                "paired_locked_usd": None, "paired_locked_loss_usd": None}
    gross = _f(status.get("premium_paid_usd"))
    if gross is None:
        b = status.get("buckets")
        if isinstance(b, dict) and b:
            gross = sum((_f(r.get("premium_usd")) or 0.0) for r in b.values() if isinstance(r, dict))
    if gross is None and "premium_paid_usd" not in status:
        p = _f(status.get("pnl_usd"))  # pre-rename engines: pnl_usd was -premium paid for fills
        gross = abs(p) if p is not None else None
    if gross is None:
        return None
    return {"basis": "gross_premium_upper_bound", "inventory_usd": abs(gross), "unpaired_usd": None,
            "paired_locked_usd": None, "paired_locked_loss_usd": None}


def inventory_usd(status: dict):
    inv = inventory_breakdown(status)
    return None if inv is None else inv["inventory_usd"]


def daily_pnl(cfg: Config, state: dict, status: dict, now: float):
    """Today's (UTC) MTM P&L. Re-based at each UTC day; carried across engine restarts."""
    cur = session_pnl(status, cfg.include_rewards)
    if cur is None:
        return None
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    elapsed = _f(status.get("session_elapsed_s")) or 0.0
    ref = _f(status.get("last_frame_ts")) or now
    start = ref - elapsed
    base = state.get("pnl_base") or {}
    same_session = base.get("session_start") is not None and abs(base["session_start"] - start) < 120
    started_today = datetime.fromtimestamp(start, timezone.utc).date().isoformat() == day
    if base.get("day") != day:
        base = {"day": day, "session_start": start, "carry": 0.0,
                "baseline": cur if (same_session or not started_today) else 0.0}
    elif not same_session:
        base = {"day": day, "session_start": start, "carry": float(base.get("last_daily", 0.0)),
                "baseline": 0.0 if started_today else cur}
    daily = cur - base["baseline"] + base["carry"]
    base["last_daily"] = daily
    state["pnl_base"] = base
    return daily


def engine_reports_live(status) -> bool:
    """Engine /status says it is trading live (``live_armed`` true or ``mode`` live)."""
    return isinstance(status, dict) and (status.get("live_armed") is True or status.get("mode") == "live")


def evaluate(cfg: Config, state: dict, now: float, status, status_err, hb_ts):
    """Return (reasons:list[str], info:dict). Pure apart from mutating state
    counters and the persisted ``engine_seen_live`` flag."""
    reasons, info = [], {}
    # heartbeat
    if hb_ts is None:
        reasons.append("heartbeat_missing")
    else:
        age = now - hb_ts
        info["heartbeat_age_s"] = round(age, 1)
        if age > cfg.hb_stale_s:
            reasons.append(f"heartbeat_stale:{age:.0f}s>{cfg.hb_stale_s:.0f}s")
    # status reachability
    if status is None:
        state["status_fail_n"] = int(state.get("status_fail_n", 0)) + 1
        info["status_error"] = str(status_err)[:200]
        if state["status_fail_n"] >= cfg.status_fails:
            reasons.append(f"status_unreachable:{state['status_fail_n']}x")
        return reasons, info
    state["status_fail_n"] = 0
    elapsed = _f(status.get("session_elapsed_s")) or 0.0
    info["session_elapsed_s"] = elapsed
    info["stage"] = status.get("stage")
    # feed staleness
    lf = _f(status.get("last_frame_ts"))
    if elapsed >= cfg.grace_s:
        if lf is None:
            reasons.append("feed_no_frames")
        else:
            fage = now - lf
            info["feed_age_s"] = round(fage, 1)
            if fage > cfg.feed_stale_s:
                reasons.append(f"feed_stale:{fage:.0f}s>{cfg.feed_stale_s:.0f}s")
    elif lf is not None:
        info["feed_age_s"] = round(now - lf, 1)
    # P&L
    d = daily_pnl(cfg, state, status, now)
    info["daily_pnl_usd"] = None if d is None else round(d, 4)
    if d is not None and d < cfg.daily_loss:
        reasons.append(f"daily_loss:{d:.2f}<{cfg.daily_loss:.2f}")
    # inventory / capital / resting
    invb = inventory_breakdown(status)
    inv = None if invb is None else invb["inventory_usd"]
    info["inventory_usd"] = inv
    info["inventory"] = invb
    if inv is not None and inv > cfg.max_inventory:
        reasons.append(f"inventory:{inv:.2f}>{cfg.max_inventory:.2f}")
    cap = _f(status.get("paper_capital_usd"))
    info["capital_usd"] = cap
    if cap is not None and cap > cfg.max_capital:
        reasons.append(f"capital:{cap:.2f}>{cfg.max_capital:.2f}")
    rn = _f(status.get("resting_n"))
    info["resting_n"] = rn
    if rn is not None and rn > cfg.max_resting:
        reasons.append(f"resting:{rn:.0f}>{cfg.max_resting:.0f}")
    # Patch 21: per-venue capital, PM US feed freshness, blocked PM writes.
    venues = status.get("venues") if isinstance(status.get("venues"), dict) else {}
    for vn, cap_v in (("kalshi", cfg.max_capital_kalshi), ("pmus", cfg.max_capital_pmus)):
        row = venues.get(vn) if isinstance(venues.get(vn), dict) else {}
        vc = _f(row.get("capital_usd"))
        info[f"capital_{vn}_usd"] = vc
        if vc is not None and vc > cap_v:
            reasons.append(f"capital_{vn}:{vc:.2f}>{cap_v:.2f}")
    pm = status.get("pmus") if isinstance(status.get("pmus"), dict) else {}
    pm_rest = _f((venues.get("pmus") or {}).get("resting_n")) if isinstance(venues.get("pmus"), dict) else None
    age = _f(pm.get("book_age_s"))
    info["pmus_book_age_s"] = age
    if pm_rest and age is not None and age > cfg.pmus_stale_s:
        reasons.append(f"pmus_feed_stale:{age:.0f}s>{cfg.pmus_stale_s:.0f}s")
    if (_f(pm.get("blocked_writes")) or 0) > 0:
        reasons.append(f"pmus_blocked_write:{int(_f(pm.get('blocked_writes')))}")
    if status.get("kill"):
        info["engine_kill"] = status.get("kill")
        if cfg.trip_on_engine_kill:
            reasons.append("engine_kill")
    info["live_armed_status"] = bool(status.get("live_armed"))
    if engine_reports_live(status):
        # Persisted until --reset: a later dead/hung engine keeps cancel-all armed.
        state["engine_seen_live"] = state.get("engine_seen_live") or now
        if not config_armed(cfg):
            # Engine got LIP_PAPER=false from its own unit drop-in while the
            # watchdog's env says paper/unarmed: the watchdog could not cancel.
            reasons.append("engine_live_watchdog_unarmed")
    return reasons, info


# ----------------------------------------------------------------- alerts
def alert(cfg: Config, state: dict, key: str, level: str, message: str, data=None, now=None,
          force=False) -> bool:
    now = time.time() if now is None else now
    last = (state.setdefault("alert_last", {})).get(key)
    hist = [t for t in state.get("alert_hist", []) if now - t < 3600]
    if not force and ((last is not None and now - last < cfg.alert_min_interval_s) or len(hist) >= cfg.alert_max_per_hour):
        state["alerts_suppressed"] = int(state.get("alerts_suppressed", 0)) + 1
        return False
    state["alert_last"][key] = now
    hist.append(now)
    state["alert_hist"] = hist
    rec = {"ts": now, "time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
           "level": level, "key": key, "message": message, "data": data or {}}
    try:
        cfg.alerts_log.parent.mkdir(parents=True, exist_ok=True)
        with cfg.alerts_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        _atomic_write(cfg.alert_json, json.dumps(rec, indent=1, default=str))
    except Exception as exc:  # never let alerting break the watchdog
        log.error("alert file write failed: %s", exc)
    if cfg.ntfy_topic:
        try:
            req = urllib.request.Request(
                f"{cfg.ntfy_server}/{urllib.parse.quote(cfg.ntfy_topic)}",
                data=message.encode("utf-8")[:3500],
                headers={"Title": f"lip-watchdog {level}", "Priority": "urgent" if level == "TRIP" else "default",
                         "Tags": "rotating_light" if level == "TRIP" else "information_source"},
                method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            log.error("ntfy push failed: %s", exc)
    log.warning("ALERT %s %s: %s", level, key, message)
    return True


# ----------------------------------------------------------------- kill actions
def write_kill_file(cfg: Config, reasons, now) -> None:
    _atomic_write(cfg.kill_file, json.dumps(
        {"reason": "watchdog:" + ";".join(reasons)[:180], "reasons": reasons, "ts": now,
         "by": "lip-watchdog"}, indent=1))


def stop_engine(cfg: Config, state: dict, now, runner=None) -> dict:
    """Stop the engine with LIP_WD_STOP_CMD (fallback when the kill file
    cannot be written). Result recorded in state["engine_stop"]; failure is
    a TRIP alert, never silent."""
    try:
        cmd = shlex.split(cfg.stop_cmd or "")
        if not cmd:
            raise RuntimeError("LIP_WD_STOP_CMD is empty")
        r = (runner or subprocess.run)(cmd, capture_output=True, text=True, timeout=60)
        res = {"ts": now, "cmd": cmd, "rc": r.returncode, "ok": r.returncode == 0,
               "stderr": str(getattr(r, "stderr", "") or "")[-300:]}
    except Exception as exc:
        res = {"ts": now, "cmd": cfg.stop_cmd, "ok": False, "error": str(exc)[:300]}
    state["engine_stop"] = res
    if res["ok"]:
        alert(cfg, state, "engine_stopped", "TRIP",
              f"kill file unwritable -> engine stopped via `{cfg.stop_cmd}`", res, now)
    else:
        alert(cfg, state, "engine_stop_failed", "TRIP",
              f"kill file unwritable AND engine stop failed (`{cfg.stop_cmd}`): {res}. "
              "Engine may still be quoting -- stop it by hand.", res, now,
              force=not state.get("engine_stop_failed_alerted"))
        state["engine_stop_failed_alerted"] = True
    return res


def config_armed(cfg: Config) -> bool:
    """The watchdog's own config allows real API writes (LIP_WD_LIVE_ARMED and not LIP_PAPER)."""
    return bool(cfg.live_flag and not cfg.paper_env)


def live_armed(cfg: Config, status, state=None) -> bool:
    """Perform a real cancel-all?

    Never unless the watchdog's own config is armed. Given that, yes when
    the engine says it is live now, OR the persisted ``engine_seen_live``
    flag is set (cleared only by --reset), OR /status is unreachable (a
    dead or hung engine is exactly the case the watchdog exists for).
    A reachable engine that reports paper and was never seen live: no.
    """
    if not config_armed(cfg):
        return False
    if not isinstance(status, dict):
        return True
    return engine_reports_live(status) or bool((state or {}).get("engine_seen_live"))


class KalshiCanceller:
    """Cancel resting orders in scope (ours by client_order_id prefix, or all).

    Cancel uses Cancel Order V2, ``DELETE /portfolio/events/orders/{order_id}``
    with ``market_ticker`` and ``exchange_index`` taken from the listed order
    row (docs.kalshi.com/api-reference/orders/cancel-order-v2, fetched
    2026-10-01). The legacy ``DELETE /portfolio/orders/{id}`` route is
    rejected since June 2026 (mm/venues/kalshi.py), and a write without
    ``market_ticker`` defaults to shard 0, so a row with no ticker is not
    sent (counted as an error; fail closed). Listing uses
    ``GET /portfolio/orders`` which covers all shards when ``exchange_index``
    is omitted. Idempotent: 404 == already gone. Verified by re-listing.
    """

    def __init__(self, cfg: Config, opener=None):
        self.cfg = cfg
        self.opener = opener or urllib.request.urlopen
        self._key = None

    def _sign(self, method, path):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        if self._key is None:
            self._key = serialization.load_pem_private_key(
                Path(self.cfg.kalshi_key_path).read_bytes(), password=None)
        ts = str(int(time.time() * 1000))
        signed = urllib.parse.urlsplit(self.cfg.kalshi_rest + path).path
        sig = self._key.sign(f"{ts}{method}{signed}".encode(),
                             padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                         salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.cfg.kalshi_key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "Content-Type": "application/json", "Accept": "application/json"}

    def _req(self, method, path, query=None):
        url = self.cfg.kalshi_rest + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, headers=self._sign(method, path), method=method)
        with self.opener(req, timeout=15) as r:
            body = r.read().decode("utf-8") or "{}"
            return json.loads(body)

    def resting_orders(self):
        rows, cursor = [], None
        for _ in range(100):
            q = {"status": "resting", "limit": 200}
            if cursor:
                q["cursor"] = cursor
            data = self._req("GET", "/portfolio/orders", q)
            rows += [o for o in data.get("orders", []) if isinstance(o, dict) and o.get("order_id")]
            cursor = data.get("cursor")
            if not cursor:
                break
        return rows

    def in_scope(self, row) -> bool:
        if self.cfg.cancel_scope == "all":
            return True
        return str(row.get("client_order_id") or "").startswith(self.cfg.coid_prefixes)

    def resting_ids(self):
        return [o["order_id"] for o in self.resting_orders() if self.in_scope(o)]

    def cancel_one(self, row) -> None:
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            raise ValueError(f"order {row.get('order_id')} has no ticker; not cancelling on shard 0")
        q = {"market_ticker": ticker}
        if row.get("exchange_index") is not None:
            q["exchange_index"] = int(row["exchange_index"])
        if row.get("subaccount_number") not in (None, "", 0):
            q["subaccount"] = int(row["subaccount_number"])
        self._req("DELETE", f"/portfolio/events/orders/{urllib.parse.quote(str(row['order_id']))}", q)

    def cancel_all(self) -> dict:
        if not (self.cfg.kalshi_key_id and self.cfg.kalshi_key_path):
            raise RuntimeError("live cancel-all needs LIP_WD_KALSHI_KEY_ID/LIP_WD_KALSHI_KEY_PATH")
        listed = self.resting_orders()
        rows = [o for o in listed if self.in_scope(o)]
        errors = 0
        for row in rows:
            try:
                self.cancel_one(row)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:      # 404: already gone -> fine (idempotent)
                    errors += 1
            except Exception:
                errors += 1
        left = self.resting_ids()        # verify; fail closed if anything in scope remains
        return {"found": len(rows), "errors": errors, "remaining": len(left), "ok": not left,
                "scope": self.cfg.cancel_scope, "skipped_foreign": len(listed) - len(rows)}


def cancel_all_action(cfg: Config, state: dict, status, now, canceller=None) -> dict:
    if not live_armed(cfg, status, state):
        res = {"mode": "paper/noop", "ok": True, "note": "live not armed: cancel-all logged only", "ts": now}
        if not state.get("cancel_noop_logged"):
            log.warning("cancel-all: live not armed -> no-op (logged only)")
            state["cancel_noop_logged"] = True
        state["cancel"] = res
        return res
    try:
        out = (canceller or KalshiCanceller(cfg)).cancel_all()
        res = {"mode": "live", "ts": now, **out}
    except Exception as exc:
        res = {"mode": "live", "ok": False, "error": str(exc)[:300], "ts": now}
    state["cancel"] = res
    if not res.get("ok"):
        alert(cfg, state, "cancel_failed", "TRIP", f"LIVE cancel-all NOT confirmed: {res}", res, now)
    return res


# ----------------------------------------------------------------- main tick
def tick(cfg: Config, now=None, status_fn=None, canceller=None, runner=None) -> dict:
    now = time.time() if now is None else now
    state = load_state(cfg)
    status, err = None, None
    try:
        status = (status_fn or fetch_status)(cfg)
    except Exception as exc:
        err = exc
    hb = read_heartbeat(cfg)
    reasons, info = evaluate(cfg, state, now, status, err, hb)
    if reasons and not state.get("latched"):
        state.update({"latched": True, "reasons": reasons, "tripped_at": now, "cancel_noop_logged": False})
        alert(cfg, state, "trip", "TRIP", "lip-watchdog TRIPPED: " + "; ".join(reasons),
              {"reasons": reasons, "info": info}, now, force=True)
        state.setdefault("alert_last", {})["latched"] = now  # reminder starts after min interval
    if "engine_live_watchdog_unarmed" in reasons:
        if "engine_live_watchdog_unarmed" not in (state.get("reasons") or []):
            state["reasons"] = list(state.get("reasons") or []) + ["engine_live_watchdog_unarmed"]
        alert(cfg, state, "live_mismatch", "TRIP",
              "ENGINE LIVE BUT WATCHDOG UNARMED: engine /status reports live_armed, but this watchdog "
              f"has LIP_WD_LIVE_ARMED={cfg.live_flag} LIP_PAPER={cfg.paper_env} and cannot cancel orders. "
              "Kill file written; fix the watchdog env (or stop the engine) now.",
              {"info": info}, now, force=not state.get("live_mismatch_alerted"))
        state["live_mismatch_alerted"] = True
    if state.get("latched"):
        try:
            if not cfg.kill_file.exists():
                write_kill_file(cfg, state.get("reasons") or ["latched"], now)
        except Exception as exc:
            alert(cfg, state, "kill_file_failed", "TRIP",
                  f"cannot write kill file {cfg.kill_file}: {exc}; stopping engine via LIP_WD_STOP_CMD",
                  None, now, force=not state.get("kill_file_failed_alerted"))
            state["kill_file_failed_alerted"] = True
            stop_engine(cfg, state, now, runner)   # every tick until the kill file lands
        if not (state.get("cancel") or {}).get("ok") or live_armed(cfg, status, state):
            cancel_all_action(cfg, state, status, now, canceller)
        alert(cfg, state, "latched", "LATCHED",
              "lip-watchdog still latched: " + "; ".join(state.get("reasons") or [])
              + " | reset: python -m mm.safety.lip_watchdog --reset", None, now)
    elif info.get("engine_kill") and not cfg.trip_on_engine_kill:
        alert(cfg, state, "engine_kill", "WARN", f"engine reports kill: {info['engine_kill']}", None, now)
    state["last_check"] = now
    save_state(cfg, state)
    health = {"ts": now, "ok": not state.get("latched"), "latched": bool(state.get("latched")),
              "reasons_now": reasons, "trip_reasons": state.get("reasons") if state.get("latched") else [],
              "info": info, "cancel": state.get("cancel"), "kill_file": str(cfg.kill_file),
              "kill_file_present": cfg.kill_file.exists(), "live_armed": live_armed(cfg, status, state),
              "config_armed": config_armed(cfg), "engine_seen_live": state.get("engine_seen_live"),
              "limits": {"heartbeat_stale_s": cfg.hb_stale_s, "feed_stale_s": cfg.feed_stale_s,
                         "daily_loss_usd": cfg.daily_loss, "max_inventory_usd": cfg.max_inventory,
                         "max_capital_usd": cfg.max_capital, "max_resting": cfg.max_resting},
              "ntfy": bool(cfg.ntfy_topic)}
    try:
        _atomic_write(cfg.health_file, json.dumps(health, indent=1, default=str))
    except Exception as exc:
        log.error("health write failed: %s", exc)
    return health


def tick_failed(cfg: Config, n: int, exc, now=None, runner=None, canceller=None) -> None:
    """The main loop's tick raised ``n`` times in a row. Alert each time; at
    LIP_WD_TICK_FAILS consecutive failures treat the watchdog itself as a
    trip: latch, write the kill file (or stop the engine), cancel-all if
    armed. Never raises."""
    now = time.time() if now is None else now
    try:
        state = load_state(cfg)
    except Exception:
        state = {}
    msg = f"watchdog tick failed ({n}/{cfg.tick_fails}): {type(exc).__name__}: {exc}"[:500]
    try:
        if n < cfg.tick_fails:
            alert(cfg, state, "tick_failed", "WARN", msg, None, now)
        else:
            reason = f"watchdog_tick_failing:{n}x"
            if not state.get("latched"):
                state.update({"latched": True, "reasons": [reason], "tripped_at": now})
            elif reason.split(":")[0] not in ";".join(state.get("reasons") or []):
                state["reasons"] = list(state.get("reasons") or []) + [reason]
            alert(cfg, state, "tick_failing", "TRIP", "lip-watchdog TRIPPED: " + msg, None, now,
                  force=not state.get("tick_failing_alerted"))
            state["tick_failing_alerted"] = True
            try:
                if not cfg.kill_file.exists():
                    write_kill_file(cfg, state.get("reasons") or [reason], now)
            except Exception as kexc:
                alert(cfg, state, "kill_file_failed", "TRIP", f"cannot write kill file: {kexc}", None, now)
                stop_engine(cfg, state, now, runner)
            if config_armed(cfg):
                cancel_all_action(cfg, state, None, now, canceller)   # status unknown -> armed
    except Exception as e2:
        log.error("tick_failed handling failed: %s", e2)
    try:
        save_state(cfg, state)
    except Exception as e3:
        log.error("tick_failed state save failed: %s", e3)


def reset(cfg: Config) -> None:
    state = load_state(cfg)
    prev = state.get("reasons")
    for p in (cfg.kill_file,):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    keep = {k: state[k] for k in ("alert_last", "alert_hist", "pnl_base") if k in state}
    keep["reset_at"] = time.time()
    keep["reset_prev_reasons"] = prev
    save_state(cfg, keep)
    alert(cfg, keep, "reset", "INFO", f"lip-watchdog reset by operator (was: {prev})", None, force=True)
    save_state(cfg, keep)
    print("watchdog reset; kill file removed. Now: systemctl restart lip-unattended "
          "(engine kill is latched in memory)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lip-watchdog")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--reset", action="store_true", help="clear latch + kill file")
    ap.add_argument("--status", action="store_true", help="print health json")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config()
    if args.reset:
        reset(cfg)
        return 0
    if args.status:
        print(cfg.health_file.read_text() if cfg.health_file.exists() else "{}")
        return 0
    log.info("lip-watchdog start: interval=%ss kill_file=%s ntfy=%s live_flag=%s paper_env=%s",
             cfg.interval_s, cfg.kill_file, bool(cfg.ntfy_topic), cfg.live_flag, cfg.paper_env)
    fails = 0
    while True:
        try:
            h = tick(cfg)
            fails = 0
            if h["reasons_now"] or h["latched"]:
                log.warning("check: latched=%s now=%s", h["latched"], h["reasons_now"])
        except Exception as exc:
            fails += 1
            log.exception("tick failed (%d consecutive): %s", fails, exc)
            tick_failed(cfg, fails, exc)
        if args.once:
            return 1 if fails else 0
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    sys.exit(main())
