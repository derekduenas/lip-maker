#!/usr/bin/env python3
"""Go-live readiness report for mm.unattended (read-only, advisory).

Reads the running engine's /status (GET http://127.0.0.1:8765/status, or
--status-file), the daily summary file(s) (--summary; the format written by
mm/unattended/health.render_daily_summary), the engine state file, the
watchdog's health/state files and the alert logs, and grades each criterion
PASS / FAIL / INSUFFICIENT (N/A where a criterion does not apply). Missing or
unreadable data is INSUFFICIENT, never PASS. Overall READY only when every
applicable criterion is PASS.

It never changes anything: it sends one GET to the status page and reads
files. It does not touch LIP_PAPER / LIP_LIVE_ACK, the maker-only
enforcement flags in execution/order_request.py, the watchdog's
LIP_WD_LIVE_ARMED, or any unit or env file. Going live stays an explicit
human decision behind those existing gates.

Criteria (thresholds are flags):

  paper_days        >= --min-days (14) distinct UTC days whose daily summary
                    says ``data_source production-books``, and /status now
                    reports paper, production-books and no
                    book_source_warning. The engine overwrites its summary
                    file every refresh, so it holds ONE day: pass dated
                    copies too (``--summary 'dir/daily-summary.*'``) or this
                    stays INSUFFICIENT.
  kalshi_fills      >= --min-kalshi-fills (300) non-synthetic Kalshi fills
                    (status venues.kalshi, else the state file).
  settled_positions >= --min-settled (100). Uses a lifetime counter
                    (status ``settled_positions_n`` / state ``settled_total``)
                    when the engine provides one; otherwise the state file's
                    ``settled`` rows, which the engine forgets after
                    LIP_SETTLED_KEEP_DAYS (7): a lower bound, so a short count
                    is INSUFFICIENT, not FAIL.
  pnl_ex_rewards    status pnl_attribution: spread_capture + adverse_selection
                    + inventory_mtm + fees (i.e. EXCLUDING estimated rewards
                    and rebates) >= 0. Also reports rewards+rebates and their
                    share of total P&L. The attribution is cumulative over the
                    engine state file's life, not a trailing window.
  markout_10m       status markout_horizons, reference --markout-ref (mid),
                    horizon 10m, scope --markout-scope (all | kalshi | pmus):
                    mean_cents >= --markout-floor-cents (-0.5) over at least
                    --min-markout-n (100) fills.
  no_kills          no engine kill latch and no watchdog TRIP/LATCHED alert
                    or reset-after-trip in the last --kill-lookback-days (7),
                    except events whose text matches --operator-test-regex
                    (default ``operator[ _-]?test``: write that phrase into the
                    kill file / reason when drilling). Also FAIL when status
                    or the state file shows a kill, or the watchdog is latched
                    now. INSUFFICIENT without a watchdog health file updated
                    in the last --health-max-age-s (600 s).
  fv_calibration    only if status has ``fv_calibration``: model Brier < book
                    Brier on >= --min-fv-markets (200) settled markets.
                    Accepted keys: n_settled|settled_n|settled_markets|
                    n_markets|n, model_brier|brier_model|model.brier,
                    book_brier|brier_book|book.brier. Absent: N/A.
  daily_loss        the daily loss limit was never hit: no ``daily_loss`` kill
                    (engine alert) or watchdog ``daily_loss:`` trip anywhere
                    in the alert logs (or since --since), and today's
                    daily_mtm_pnl_usd above -risk_limits.daily_loss_usd.
                    INSUFFICIENT when no alert log can be read.

Exit status: 0 READY, 1 NOT READY (a FAIL), 2 INSUFFICIENT DATA.

    sudo -u lip /opt/lip-maker/.venv/bin/python /opt/lip-maker/tools/readiness_report.py
    ... --summary /var/lib/lip-maker/daily-summary --summary '/var/lib/lip-maker/summaries/*' --json
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD_BOOKS_FLAG = "production-books"   # mm.venues.readonly.PROD_BOOKS_FLAG
VAR = Path("/var/lib/lip-maker")
REWARD_KEYS = ("est_rewards_kalshi_usd", "est_rewards_pmus_usd", "rebates_usd")
STRATEGY_KEYS = ("spread_capture_usd", "adverse_selection_usd", "inventory_mtm_usd", "fees_usd")

PASS, FAIL, INSUFF, NA = "PASS", "FAIL", "INSUFFICIENT", "N/A"

REMINDER = (
    "Going live is an explicit human decision. This report is advisory and changed nothing. "
    "It does not replace the existing gates, all of which stay manual: LIP_PAPER=false with "
    "the LIP_LIVE_ACK acknowledgement phrase, per-venue maker-only enforcement verification "
    "(execution/order_request.py: KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED / "
    "MAKER_ONLY_ENFORCEMENT_VERIFIED, with recorded evidence), and the watchdog armed "
    "(LIP_WD_LIVE_ARMED=true in the watchdog's own env, verified with --status)."
)


# --------------------------------------------------------------------- io
def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{path}: not found"
    except (OSError, ValueError) as e:
        return None, f"{path}: {type(e).__name__}: {e}"


def fetch_status(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:   # GET
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:  # unreachable engine = no evidence
        return None, f"{url}: {type(e).__name__}: {e}"


def _expand(patterns) -> list:
    out = []
    for p in patterns or ():
        hits = sorted(glob.glob(p))
        out.extend(hits if hits else [p])
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def parse_summary(text: str) -> dict:
    """One daily summary (health.render_daily_summary)."""
    out = {"day": None, "data_source": None, "attribution": None}
    for line in text.splitlines():
        if line.startswith("daily summary "):
            out["day"] = line[len("daily summary "):].strip() or None
        elif line.startswith("data_source "):
            out["data_source"] = line[len("data_source "):].strip()
        elif line.startswith("pnl_attribution_estimate_paper "):
            parts = line.split()[1:]
            try:
                out["attribution"] = {parts[i]: float(parts[i + 1]) for i in range(0, len(parts) - 1, 2)}
            except ValueError:
                out["attribution"] = None
        else:
            k, _, v = line.partition(" ")
            if k in ("fills", "pnl_usd", "rewards_usd", "premium_paid_usd"):
                try:
                    out[k] = float(v)
                except ValueError:
                    pass
    return out


def _ts(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read_alerts(paths) -> tuple[list, list, list]:
    """Events from the watchdog's JSON-lines alerts.log and the engine's text
    alerts log (monitor.alerts: ``ISO  LEVEL  source  message``)."""
    events, read, errors = [], [], []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            errors.append(f"{p}: not found")
            continue
        except OSError as e:
            errors.append(f"{p}: {e}")
            continue
        read.append(p)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                events.append({"ts": _ts(rec.get("ts") or rec.get("time_utc")), "origin": "watchdog",
                               "level": str(rec.get("level") or ""), "key": str(rec.get("key") or ""),
                               "message": str(rec.get("message") or ""), "file": p})
            else:
                parts = re.split(r"\s{2,}", line, maxsplit=3)
                if len(parts) < 4:
                    continue
                events.append({"ts": _ts(parts[0]), "origin": "engine", "level": parts[1].strip(),
                               "key": parts[2].strip(), "message": parts[3], "file": p})
    return events, read, errors


# ---------------------------------------------------------------- helpers
def _crit(cid, title, status, value=None, detail="", evidence=None):
    return {"id": cid, "title": title, "status": status, "value": value, "detail": detail,
            "evidence": evidence or []}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dig(d, *paths):
    for path in paths:
        cur = d
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            return cur
    return None


# --------------------------------------------------------------- criteria
def crit_paper_days(status, status_err, summaries, a):
    title = f">= {a.min_days} days paper on production books, no book_source_warning"
    days = {}
    for s in summaries:
        if not s.get("day"):
            continue
        prod = s.get("data_source") == PROD_BOOKS_FLAG
        days[s["day"]] = days.get(s["day"], True) and prod   # any non-prod copy taints the day
    prod_days = sorted(d for d, ok in days.items() if ok)
    other = sorted(d for d, ok in days.items() if not ok)
    value = {"production_days": len(prod_days), "first": prod_days[0] if prod_days else None,
             "last": prod_days[-1] if prod_days else None, "non_production_days": other,
             "status_data_source": None if status is None else status.get("data_source"),
             "book_source_warning": None if status is None else status.get("book_source_warning")}
    if status is None:
        return _crit("paper_days", title, INSUFF, value, f"no /status: {status_err}")
    if status.get("paper") is not True:
        return _crit("paper_days", title, FAIL, value, "status does not report paper mode")
    if status.get("book_source_warning"):
        return _crit("paper_days", title, FAIL, value, "unresolved book_source_warning in /status")
    if status.get("data_source") != PROD_BOOKS_FLAG:
        return _crit("paper_days", title, FAIL, value,
                     f"status data_source is {status.get('data_source')!r}, not {PROD_BOOKS_FLAG!r}")
    if len(prod_days) >= a.min_days:
        return _crit("paper_days", title, PASS, value)
    return _crit("paper_days", title, INSUFF, value,
                 f"{len(prod_days)} production-books day(s) in the summaries read; the engine keeps "
                 "only today's summary, so pass dated copies with --summary")


def crit_kalshi_fills(status, state, a):
    title = f">= {a.min_kalshi_fills} Kalshi fills (non-synthetic)"
    src, n = None, None
    k = _dig(status or {}, "venues.kalshi")
    if isinstance(k, dict) and k.get("fills_n") is not None:
        n = int(k.get("fills_n") or 0) - int(k.get("synthetic_fills_n") or 0)
        src = "status venues.kalshi"
    elif isinstance(state, dict) and isinstance(state.get("fills_by_venue"), dict):
        n = int(state["fills_by_venue"].get("kalshi") or 0) - int(
            (state.get("fills_synthetic_by_venue") or {}).get("kalshi") or 0)
        src = "state fills_by_venue"
    if n is None:
        return _crit("kalshi_fills", title, INSUFF, None, "no fill counts in status or state file")
    return _crit("kalshi_fills", title, PASS if n >= a.min_kalshi_fills else FAIL, n, f"source: {src}")


def crit_settled(status, state, a):
    title = f">= {a.min_settled} settled positions"
    exact = _dig(status or {}, "settled_positions_n")
    if exact is None and isinstance(state, dict):
        exact = state.get("settled_total")
    if exact is not None:
        n = int(exact)
        return _crit("settled_positions", title, PASS if n >= a.min_settled else FAIL, n,
                     "lifetime settled counter")
    if not isinstance(state, dict) or not isinstance(state.get("settled"), dict):
        return _crit("settled_positions", title, INSUFF, None, "no settled data in status or state file")
    n = len(state["settled"])
    if n >= a.min_settled:
        return _crit("settled_positions", title, PASS, n, "state file settled rows (lower bound)")
    return _crit("settled_positions", title, INSUFF, n,
                 "state file settled rows are a lower bound (the engine forgets them after "
                 "LIP_SETTLED_KEEP_DAYS, 7) and the engine has no lifetime settled counter")


def crit_pnl(status, summaries, a):
    title = "P&L excluding rewards and rebates >= 0"
    attr, src = None, None
    if isinstance(status, dict) and isinstance(status.get("pnl_attribution"), dict):
        attr, src = status["pnl_attribution"], "status pnl_attribution"
    else:
        for s in sorted((s for s in summaries if s.get("attribution")),
                        key=lambda s: s.get("day") or "", reverse=True):
            attr, src = s["attribution"], f"daily summary {s.get('day')}"
            break
    if attr is None:
        return _crit("pnl_ex_rewards", title, INSUFF, None, "no pnl_attribution in status or summary")
    vals = {k: _num(attr.get(k)) for k in STRATEGY_KEYS + REWARD_KEYS}
    if any(vals[k] is None for k in STRATEGY_KEYS):
        return _crit("pnl_ex_rewards", title, INSUFF, None,
                     f"{src} lacks {[k for k in STRATEGY_KEYS if vals[k] is None]}")
    ex = sum(vals[k] for k in STRATEGY_KEYS)
    rew = sum(vals[k] or 0.0 for k in REWARD_KEYS)
    total = _num(attr.get("total_usd"))
    total = ex + rew if total is None else total
    value = {"ex_rewards_usd": round(ex, 6), "rewards_usd": round(rew, 6), "total_usd": round(total, 6),
             "rewards_share_of_total": (round(rew / total, 6) if total > 0 else None),
             "components": vals}
    detail = (f"source: {src} (cumulative over the engine state file's life); rewards+rebates "
              + (f"{100 * rew / total:.1f}% of total P&L" if total > 0
                 else f"${rew:.2f} against total ${total:.2f}"))
    if all(vals[k] == 0 for k in STRATEGY_KEYS):
        return _crit("pnl_ex_rewards", title, INSUFF, value, "no measured strategy P&L yet; " + detail)
    return _crit("pnl_ex_rewards", title, PASS if ex >= 0 else FAIL, value, detail)


def crit_markout(status, a):
    title = (f"10m markout mean >= {a.markout_floor_cents:+.2f}c/contract "
             f"({a.markout_ref}, {a.markout_scope}, n >= {a.min_markout_n})")
    node = _dig(status or {}, f"markout_horizons.by_ref.{a.markout_ref}.10m")
    if not isinstance(node, dict):
        return _crit("markout_10m", title, INSUFF, None, "no markout_horizons 10m in status")
    cell = node.get("all") if a.markout_scope == "all" else (node.get("venue") or {}).get(a.markout_scope)
    venues = {k: (v or {}).get("mean_cents") for k, v in (node.get("venue") or {}).items()}
    if not isinstance(cell, dict) or cell.get("mean_cents") is None:
        return _crit("markout_10m", title, INSUFF, {"by_venue": venues}, "no measured 10m markouts")
    n, mean = int(cell.get("n") or 0), float(cell["mean_cents"])
    value = {"n": n, "mean_cents": mean, "by_venue": venues}
    if n < a.min_markout_n:
        return _crit("markout_10m", title, INSUFF, value, f"only {n} measured fills")
    return _crit("markout_10m", title, PASS if mean >= a.markout_floor_cents else FAIL, value)


def _is_trip(ev) -> bool:
    if ev["origin"] == "watchdog":
        return ev["level"] in ("TRIP", "LATCHED")
    return "engine kill latched" in ev["message"]


def crit_no_kills(status, state, health, health_err, wd_state, events, alert_read, now, a):
    title = (f"no engine kill latch / watchdog trip in the last {a.kill_lookback_days:g} days "
             "(operator tests excepted)")
    test_re = re.compile(a.operator_test_regex)
    since = now - a.kill_lookback_days * 86400.0
    hits, excused = [], []
    for ev in events:
        if not _is_trip(ev) or ev["ts"] is None or ev["ts"] < since:
            continue
        (excused if test_re.search(ev["message"]) else hits).append(
            {"time": datetime.fromtimestamp(ev["ts"], timezone.utc).isoformat(),
             "origin": ev["origin"], "level": ev["level"], "message": ev["message"][:200]})
    for label, kill in (("status", (status or {}).get("kill")), ("state file", (state or {}).get("kill"))):
        if kill:
            reason = json.dumps(kill, default=str)
            (excused if test_re.search(reason) else hits).append(
                {"time": "now", "origin": label, "level": "KILL", "message": reason[:200]})
    if isinstance(wd_state, dict):
        for key, reasons_key in (("tripped_at", "reasons"), ("reset_at", "reset_prev_reasons")):
            at = _num(wd_state.get(key))
            reasons = wd_state.get(reasons_key)
            if at is not None and at >= since and reasons:
                text = "; ".join(map(str, reasons)) if isinstance(reasons, list) else str(reasons)
                (excused if test_re.search(text) else hits).append(
                    {"time": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                     "origin": f"watchdog_state {key}", "level": "TRIP", "message": text[:200]})
    value = {"events": hits, "operator_tests": excused, "alert_logs_read": alert_read}
    if isinstance(health, dict) and health.get("latched"):
        hits.append({"time": "now", "origin": "watchdog_health", "level": "LATCHED",
                     "message": json.dumps(health.get("trip_reasons"))[:200]})
    if hits:
        return _crit("no_kills", title, FAIL, value, f"{len(hits)} kill/trip event(s)")
    if not isinstance(health, dict):
        return _crit("no_kills", title, INSUFF, value, f"no watchdog health: {health_err}")
    age = now - (_num(health.get("ts")) or 0.0)
    if age > a.health_max_age_s:
        return _crit("no_kills", title, INSUFF, value,
                     f"watchdog health is {age:.0f}s old (> {a.health_max_age_s:g}s): watchdog not running?")
    if not alert_read:
        return _crit("no_kills", title, INSUFF, value, "no alert log could be read")
    return _crit("no_kills", title, PASS, value,
                 "the watchdog health file shows it running now; its history before that is not provable "
                 "from these files")


def crit_fv(status, a):
    title = f"model Brier < book Brier on >= {a.min_fv_markets} settled markets (if reported)"
    cal = (status or {}).get("fv_calibration") if isinstance(status, dict) else None
    if cal is None:
        return _crit("fv_calibration", title, NA, None, "status has no fv_calibration")
    if not isinstance(cal, dict):
        return _crit("fv_calibration", title, INSUFF, cal, "fv_calibration is not an object")
    n = _num(_dig(cal, "n_settled", "settled_n", "settled_markets", "n_markets", "markets", "n"))
    mb = _num(_dig(cal, "model_brier", "brier_model", "model.brier", "fv_brier"))
    bb = _num(_dig(cal, "book_brier", "brier_book", "book.brier", "market_brier"))
    value = {"n": n, "model_brier": mb, "book_brier": bb}
    if n is None or mb is None or bb is None:
        return _crit("fv_calibration", title, INSUFF, value, "fv_calibration lacks n / model / book Brier")
    if n < a.min_fv_markets:
        return _crit("fv_calibration", title, INSUFF, value, f"only {int(n)} settled markets")
    return _crit("fv_calibration", title, PASS if mb < bb else FAIL, value)


def crit_daily_loss(status, events, alert_read, a):
    title = "daily loss limit never hit"
    since = _ts(a.since) if a.since else None
    hits = []
    for ev in events:
        if since is not None and (ev["ts"] is None or ev["ts"] < since):
            continue
        m = ev["message"]
        if (ev["origin"] == "engine" and "engine kill latched" in m and "daily_loss" in m) or \
                (ev["origin"] == "watchdog" and "daily_loss:" in m):
            hits.append({"time": None if ev["ts"] is None else
                         datetime.fromtimestamp(ev["ts"], timezone.utc).isoformat(),
                         "origin": ev["origin"], "message": m[:200]})
    kill = json.dumps((status or {}).get("kill") or "", default=str)
    if "daily_loss" in kill:
        hits.append({"time": "now", "origin": "status kill", "message": kill[:200]})
    d = _num((status or {}).get("daily_mtm_pnl_usd"))
    lim = _num(_dig(status or {}, "risk_limits.daily_loss_usd"))
    if d is not None and lim is not None and d <= -abs(lim):
        hits.append({"time": "now", "origin": "status", "message": f"daily_mtm_pnl_usd {d} <= -{abs(lim)}"})
    value = {"events": hits, "alert_logs_read": alert_read, "today_mtm_usd": d, "limit_usd": lim}
    if hits:
        return _crit("daily_loss", title, FAIL, value, f"{len(hits)} daily-loss event(s)")
    if not alert_read:
        return _crit("daily_loss", title, INSUFF, value, "no alert log could be read")
    return _crit("daily_loss", title, PASS, value,
                 "searched " + ("all of " if since is None else f"since {a.since} in ") + ", ".join(alert_read))


# ------------------------------------------------------------------ report
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Read-only go-live readiness report (advisory).")
    ap.add_argument("--status-url", default="http://127.0.0.1:8765/status")
    ap.add_argument("--status-file", default=None, help="read status JSON from a file instead")
    ap.add_argument("--summary", action="append", default=None,
                    help="daily summary file or glob (repeatable; default /var/lib/lip-maker/daily-summary)")
    ap.add_argument("--state", default=str(VAR / "engine_state.json"))
    ap.add_argument("--watchdog-health", default=str(VAR / "watchdog_health.json"))
    ap.add_argument("--watchdog-state", default=str(VAR / "watchdog_state.json"))
    ap.add_argument("--alerts", action="append", default=None,
                    help="alert log (repeatable; default /var/lib/lip-maker/alerts.log, "
                         "<repo>/logs/alerts.log and <repo>.prev/logs/alerts.log)")
    ap.add_argument("--min-days", type=int, default=14)
    ap.add_argument("--min-kalshi-fills", type=int, default=300)
    ap.add_argument("--min-settled", type=int, default=100)
    ap.add_argument("--markout-floor-cents", type=float, default=-0.5)
    ap.add_argument("--markout-ref", default="mid", choices=("mid", "fv"))
    ap.add_argument("--markout-scope", default="all", choices=("all", "kalshi", "pmus"))
    ap.add_argument("--min-markout-n", type=int, default=100)
    ap.add_argument("--kill-lookback-days", type=float, default=7.0)
    ap.add_argument("--operator-test-regex", default=r"(?i)operator[ _-]?test")
    ap.add_argument("--health-max-age-s", type=float, default=600.0)
    ap.add_argument("--min-fv-markets", type=int, default=200)
    ap.add_argument("--since", default=None, help="ISO date/time: daily-loss history starts here")
    ap.add_argument("--now", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    return ap.parse_args(argv)


def build_report(a) -> dict:
    now = _ts(a.now) if a.now else datetime.now(timezone.utc).timestamp()
    if a.status_file:
        status, status_err = _read_json(a.status_file)
    else:
        status, status_err = fetch_status(a.status_url)
    if status is not None and not isinstance(status, dict):
        status, status_err = None, "status is not a JSON object"
    summaries, summary_err = [], []
    for p in _expand(a.summary or [str(VAR / "daily-summary")]):
        try:
            summaries.append(dict(parse_summary(Path(p).read_text(encoding="utf-8")), file=p))
        except OSError as e:
            summary_err.append(f"{p}: {e}")
    state, state_err = _read_json(a.state)
    health, health_err = _read_json(a.watchdog_health)
    wd_state, _ = _read_json(a.watchdog_state)
    # The engine's alerts (monitor.alerts) go to <repo>/logs/alerts.log, and
    # deploy.sh moves the previous tree to <repo>.prev: read that copy too.
    alerts = a.alerts or [str(VAR / "alerts.log"), str(ROOT / "logs" / "alerts.log"),
                          str(ROOT.parent / (ROOT.name + ".prev") / "logs" / "alerts.log")]
    events, alert_read, alert_err = read_alerts(_expand(alerts))

    criteria = [
        crit_paper_days(status, status_err, summaries, a),
        crit_kalshi_fills(status, state, a),
        crit_settled(status, state, a),
        crit_pnl(status, summaries, a),
        crit_markout(status, a),
        crit_no_kills(status, state, health, health_err, wd_state, events, alert_read, now, a),
        crit_fv(status, a),
        crit_daily_loss(status, events, alert_read, a),
    ]
    graded = [c["status"] for c in criteria if c["status"] != NA]
    overall = ("NOT READY" if FAIL in graded else
               "READY" if graded and all(s == PASS for s in graded) else "INSUFFICIENT DATA")
    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "read_only": True,
        "overall": overall,
        "criteria": criteria,
        "inputs": {"status": a.status_file or a.status_url, "status_error": status_err,
                   "summaries": [s["file"] for s in summaries], "summary_errors": summary_err,
                   "state": a.state, "state_error": state_err,
                   "watchdog_health": a.watchdog_health, "watchdog_health_error": health_err,
                   "alerts_read": alert_read, "alert_errors": alert_err},
        "info": {"status_live_armed": None if status is None else status.get("live_armed"),
                 "status_mode": None if status is None else status.get("mode"),
                 "watchdog_config_armed": None if not isinstance(health, dict) else health.get("config_armed")},
        "reminder": REMINDER,
    }


def render(rep: dict) -> str:
    out = [f"go-live readiness report ({rep['generated_at']}) - read-only, advisory", ""]
    for c in rep["criteria"]:
        out.append(f"[{c['status']:<12}] {c['title']}")
        if c["value"] is not None:
            out.append(f"               value: {json.dumps(c['value'], default=str)[:600]}")
        if c["detail"]:
            out.append(f"               {c['detail']}")
    out.append("")
    inp = rep["inputs"]
    for k in ("status_error", "state_error", "watchdog_health_error"):
        if inp.get(k):
            out.append(f"input: {inp[k]}")
    for e in inp.get("summary_errors", []) + inp.get("alert_errors", []):
        out.append(f"input: {e}")
    out.append(f"info: status mode={rep['info']['status_mode']} live_armed={rep['info']['status_live_armed']} "
               f"watchdog config_armed={rep['info']['watchdog_config_armed']}")
    out.append("")
    out.append(f"OVERALL: {rep['overall']}")
    out.append("")
    out.append(rep["reminder"])
    return "\n".join(out)


def main(argv=None) -> int:
    a = parse_args(argv)
    rep = build_report(a)
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    return {"READY": 0, "NOT READY": 1}.get(rep["overall"], 2)


if __name__ == "__main__":
    sys.exit(main())
