"""lip-watchdog: independent kill switch for the lip-maker engine (patch 17).

Runs as its own systemd service (``lip-watchdog``), separate from
``lip-unattended``. Standard library only for the checks; the live
cancel-all path lazily imports ``cryptography`` to sign Kalshi requests.

Every ``LIP_WD_INTERVAL_S`` (30s) it checks:
  * heartbeat file age            > LIP_WD_HEARTBEAT_STALE_S (90)
  * /status unreachable           LIP_WD_STATUS_FAILS (2) consecutive failures
  * market-data feed stale        now - status.last_frame_ts > LIP_WD_FEED_STALE_S (120)
                                  (skipped for LIP_WD_GRACE_S (240) after an engine session starts)
  * daily P&L                     < -abs(LIP_WD_DAILY_LOSS) (50). P&L = sum of bucket markout_usd
                                  (MTM of fills, rewards excluded unless LIP_WD_PNL_INCLUDE_REWARDS=1),
                                  re-based at each UTC day and on engine restart.
  * inventory                     held premium > LIP_WD_MAX_INVENTORY_USD (500)
  * capital                       paper_capital_usd > LIP_WD_MAX_CAPITAL_USD (1500)
  * resting orders                resting_n > LIP_WD_MAX_RESTING (200)
  * engine-reported kill          status.kill set (alert only, not a trip by default)

On trip (latched until ``--reset``):
  1. write the kill file (LIP_KILL_FILE, /var/lib/lip-maker/KILL); the engine
     checks it once per second and cancels all quotes / stops quoting;
  2. cancel-all resting orders via the Kalshi API -- ONLY when live is armed
     (LIP_WD_LIVE_ARMED=true and LIP_PAPER!=true and status.live_armed true).
     Otherwise logged as a no-op. Idempotent; retried every tick until the
     API shows zero resting orders (fail-closed: latch stays, alerts repeat);
  3. alert (alerts.log + alert JSON; optional ntfy push), rate-limited.

Reset:  python -m mm.safety.lip_watchdog --reset   (then restart lip-unattended)
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
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
        self.trip_on_engine_kill = _truthy(env.get("LIP_WD_TRIP_ON_ENGINE_KILL"))
        self.alert_min_interval_s = _num(env, "LIP_WD_ALERT_MIN_INTERVAL_S", 600)
        self.alert_max_per_hour = int(_num(env, "LIP_WD_ALERT_MAX_PER_HOUR", 12))
        self.ntfy_topic = (env.get("LIP_WD_NTFY_TOPIC") or "").strip()
        self.ntfy_server = env.get("LIP_WD_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.live_flag = _truthy(env.get("LIP_WD_LIVE_ARMED"))
        self.paper_env = _truthy(env.get("LIP_PAPER", "true"))
        self.kalshi_rest = env.get("LIP_WD_KALSHI_REST", PROD_REST).rstrip("/")
        self.kalshi_key_id = env.get("LIP_WD_KALSHI_KEY_ID", "")
        self.kalshi_key_path = env.get("LIP_WD_KALSHI_KEY_PATH", "")


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
        # Corrupt state: fail closed -- treat as latched so a human looks.
        return {"latched": True, "reasons": ["watchdog_state_corrupt"], "tripped_at": time.time()}


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
    """MTM P&L of the current engine session from status buckets."""
    b = status.get("buckets")
    if isinstance(b, dict) and b:
        tot = 0.0
        for row in b.values():
            if not isinstance(row, dict):
                continue
            tot += _f(row.get("markout_usd")) or 0.0
            if include_rewards:
                tot += _f(row.get("raw_est_usd")) or 0.0
        return tot
    return None


def inventory_usd(status: dict):
    b = status.get("buckets")
    if isinstance(b, dict) and b:
        return sum((_f(r.get("premium_usd")) or 0.0) for r in b.values() if isinstance(r, dict))
    p = _f(status.get("pnl_usd"))  # engine pnl_usd is -premium paid for fills
    return abs(p) if p is not None else None


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


def evaluate(cfg: Config, state: dict, now: float, status, status_err, hb_ts):
    """Return (reasons:list[str], info:dict). Pure apart from mutating state counters."""
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
    inv = inventory_usd(status)
    info["inventory_usd"] = inv
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
    if status.get("kill"):
        info["engine_kill"] = status.get("kill")
        if cfg.trip_on_engine_kill:
            reasons.append("engine_kill")
    info["live_armed_status"] = bool(status.get("live_armed"))
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


def live_armed(cfg: Config, status) -> bool:
    """All three must agree before any real API write."""
    return bool(cfg.live_flag and not cfg.paper_env and isinstance(status, dict)
                and status.get("live_armed") is True)


class KalshiCanceller:
    """Cancel every resting order on the account. Idempotent: 404 == done."""

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

    def resting_ids(self):
        ids, cursor = [], None
        for _ in range(100):
            q = {"status": "resting", "limit": 200}
            if cursor:
                q["cursor"] = cursor
            data = self._req("GET", "/portfolio/orders", q)
            ids += [o["order_id"] for o in data.get("orders", []) if o.get("order_id")]
            cursor = data.get("cursor")
            if not cursor:
                break
        return ids

    def cancel_all(self) -> dict:
        if not (self.cfg.kalshi_key_id and self.cfg.kalshi_key_path):
            raise RuntimeError("live cancel-all needs LIP_WD_KALSHI_KEY_ID/LIP_WD_KALSHI_KEY_PATH")
        ids = self.resting_ids()
        errors = 0
        for oid in ids:
            try:
                self._req("DELETE", f"/portfolio/orders/{urllib.parse.quote(oid)}")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:      # 404: already gone -> fine (idempotent)
                    errors += 1
            except Exception:
                errors += 1
        left = self.resting_ids()        # verify; fail closed if anything remains
        return {"found": len(ids), "errors": errors, "remaining": len(left), "ok": not left}


def cancel_all_action(cfg: Config, state: dict, status, now, canceller=None) -> dict:
    if not live_armed(cfg, status):
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
def tick(cfg: Config, now=None, status_fn=None, canceller=None) -> dict:
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
    if state.get("latched"):
        try:
            if not cfg.kill_file.exists():
                write_kill_file(cfg, state.get("reasons") or ["latched"], now)
        except Exception as exc:
            alert(cfg, state, "kill_file_failed", "TRIP", f"cannot write kill file: {exc}", None, now)
        if not (state.get("cancel") or {}).get("ok") or live_armed(cfg, status):
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
              "kill_file_present": cfg.kill_file.exists(), "live_armed": live_armed(cfg, status),
              "limits": {"heartbeat_stale_s": cfg.hb_stale_s, "feed_stale_s": cfg.feed_stale_s,
                         "daily_loss_usd": cfg.daily_loss, "max_inventory_usd": cfg.max_inventory,
                         "max_capital_usd": cfg.max_capital, "max_resting": cfg.max_resting},
              "ntfy": bool(cfg.ntfy_topic)}
    try:
        _atomic_write(cfg.health_file, json.dumps(health, indent=1, default=str))
    except Exception as exc:
        log.error("health write failed: %s", exc)
    return health


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
    while True:
        try:
            h = tick(cfg)
            if h["reasons_now"] or h["latched"]:
                log.warning("check: latched=%s now=%s", h["latched"], h["reasons_now"])
        except Exception as exc:
            log.exception("tick failed: %s", exc)
        if args.once:
            return 0
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    sys.exit(main())
