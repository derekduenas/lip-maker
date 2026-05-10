"""LIP program discovery.

Polls Kalshi's /incentive_programs endpoint, parses active liquidity
programs, filters by our settings (reward threshold, target-size cap,
discount-factor floor, series blocklist), and persists to SQLite.

The /incentive_programs endpoint is UNAUTHENTICATED per Kalshi docs.

API response fields (from Kalshi docs):
  period_reward:       int, CENTI-CENTS (divide by 10,000 for USD)
  discount_factor_bps: int, basis points (divide by 10,000 for multiplier)
  target_size_fp:      fixed-point string, up to 2 decimals
  incentive_type:      "liquidity" | "volume"
  paid_out:            bool
  start_date/end_date: ISO 8601
  market_ticker:       Kalshi market identifier
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)


def _series_from_market_ticker(ticker: str) -> str:
    """Extract series prefix. E.g., 'KXHIGHCHI-26APR19-B50.5' → 'KXHIGHCHI'."""
    if not ticker:
        return ""
    # Series is everything before the first hyphen
    return ticker.split("-", 1)[0]


def _parse_program(raw: dict) -> dict:
    """Normalize raw incentive program fields."""
    period_reward_usd = float(raw.get("period_reward", 0)) / 10_000.0  # centi-cents → USD
    discount_factor   = float(raw.get("discount_factor_bps", 10_000)) / 10_000.0  # bps → multiplier
    target_size       = float(raw.get("target_size_fp", 0))
    start_date        = raw.get("start_date", "")
    end_date          = raw.get("end_date", "")

    days_in_period = 1
    try:
        s = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        days_in_period = max(1, (e - s).days or 1)
    except Exception:
        pass

    reward_per_day = period_reward_usd / days_in_period

    return {
        "id":                 raw.get("id"),
        "market_ticker":      raw.get("market_ticker", ""),
        "series_ticker":      _series_from_market_ticker(raw.get("market_ticker", "")),
        "start_date":         start_date,
        "end_date":           end_date,
        "period_reward_usd":  period_reward_usd,
        "discount_factor":    discount_factor,
        "target_size":        target_size,
        "paid_out":           int(bool(raw.get("paid_out", False))),
        "reward_per_day_usd": reward_per_day,
    }


def is_active_clause(alias: str = "") -> str:
    """SQL fragment: row represents a CURRENTLY active enrolled market.

    Single source of truth for quotability gating. Use as:
        f"SELECT ... FROM lip_programs WHERE {is_active_clause()}"
    Or with a table alias:
        f"SELECT ... FROM lip_programs p WHERE {is_active_clause('p')}"

    Three gates: enrolled (our decision), paid_out (Kalshi's done flag),
    end_date (program window). All three must hold for "currently quotable".
    """
    pre = f"{alias}." if alias else ""
    return (f"{pre}enrolled = 1 AND {pre}paid_out = 0 "
            f"AND datetime({pre}end_date) > datetime('now')")


def _decide_enrol(p: dict, now_iso: str | None = None) -> tuple[int, str]:
    """Our quoting decision for a program. Returns (enrol 0/1, reason)."""
    series = p["series_ticker"] or ""
    # 2026-04-22: prefix-match (startswith) instead of exact. Kalshi spawns
    # subseries like KXNBAMENTION/KXNBARETURN under the KXNBA family — exact
    # match missed 125+ NBA prop markets ($2.5k/day reward exposure) that
    # SIG dominates. KXFEDDECISION still matches only itself (no KXFEDDECISION*
    # subseries exist; doesn't accidentally match KXFEDERALCHARGE).
    if any(series.startswith(b) for b in settings.SERIES_BLOCKLIST):
        return 0, f"blocklist:series({series})"
    if p["reward_per_day_usd"] < settings.MIN_REWARD_PER_DAY_USD:
        return 0, f"reward_too_small:{p['reward_per_day_usd']:.2f}"
    if p["target_size"] > settings.MAX_TARGET_SIZE_CONTRACTS:
        return 0, f"target_too_large:{p['target_size']:.0f}"
    if p["discount_factor"] < settings.MIN_DISCOUNT_FACTOR:
        return 0, f"discount_too_low:{p['discount_factor']:.2f}"
    if p["paid_out"]:
        return 0, "already_paid_out"
    # 2026-05-10 Phase 3: end_date gate at write-time. Kalshi's API returns
    # programs past end_date until they flip paid_out=1 (lag of hours/days).
    # Without this check, callers see ~10% stale enrolled rows that aren't
    # actually quotable. Belt-and-suspenders with daily lip_state_hygiene cron.
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    if p["end_date"] and p["end_date"] <= now_iso:
        return 0, "expired"
    return 1, "ok"


def discover(*, status: str | None = None, save: bool = True) -> list[dict]:
    """Fetch + persist LIP programs. Returns parsed list.

    2026-04-30: pull ALL status flavors (active, closed, pending, paid)
    so settlement_log → lip_programs JOIN works for already-settled markets.
    Previously pulled only active → closed programs got purged after settle
    → _estimate_rebate returned $0 silently. Bug fixed.
    """
    c = KalshiClient()
    programs: list[dict] = []
    statuses = ("active", "closed", "pending", "paid") if status is None else (status,)

    for s in statuses:
        cursor = None
        page_n = 0
        status_total = 0
        status_kept = 0
        first_5_tickers = []
        while True:
            params = {"status": s, "type": "liquidity", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = c.get_unauth("/incentive_programs", params=params)
            except Exception as e:
                _log.warning(f"/incentive_programs status={s} failed: {e}")
                break
            batch = resp.get("incentive_programs", [])
            page_n += 1
            status_total += len(batch)
            for raw in batch:
                tk = raw.get("market_ticker") or ""
                if len(first_5_tickers) < 5 and tk:
                    first_5_tickers.append(tk[:30])
                if raw.get("incentive_type") != "liquidity":
                    continue
                status_kept += 1
                programs.append(_parse_program(raw))
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        _log.info(
            f"CHECKPOINT-1 status={s}  pages={page_n}  raw_returned={status_total}  "
            f"kept_as_liquidity={status_kept}  first_5={first_5_tickers}"
        )

    _log.info(f"CHECKPOINT-1-TOTAL programs_after_status_loop={len(programs)}")

    # Decide enrolment and persist
    now_iso = datetime.now(timezone.utc).isoformat()
    # CHECKPOINT-2: drop counts per filter + 3 sample victims each
    drop_buckets: dict[str, list] = {
        "blocklist": [], "reward_too_small": [], "target_too_large": [],
        "discount_too_low": [], "already_paid_out": [],
    }
    n_enrolled = 0
    n_total = 0
    if save and programs:
        conn = sqlite3.connect(settings.DB_PATH)
        try:
            for p in programs:
                n_total += 1
                enrol, reason = _decide_enrol(p, now_iso=now_iso)
                if enrol:
                    n_enrolled += 1
                else:
                    bucket_key = reason.split(":", 1)[0]
                    drop_buckets.setdefault(bucket_key, []).append(
                        f"{p['market_ticker'][:35]}|{reason}|rew=${p['reward_per_day_usd']:.2f}|"
                        f"tgt={p['target_size']:.0f}|df={p['discount_factor']:.2f}"
                    )
                conn.execute(
                    """INSERT OR REPLACE INTO lip_programs
                       (id, market_ticker, series_ticker, start_date, end_date,
                        period_reward_usd, discount_factor, target_size, paid_out,
                        enrolled, blocked_reason, reward_per_day_usd, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p["id"], p["market_ticker"], p["series_ticker"],
                        p["start_date"], p["end_date"], p["period_reward_usd"],
                        p["discount_factor"], p["target_size"], p["paid_out"],
                        enrol, reason if not enrol else None,
                        p["reward_per_day_usd"], now_iso,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    # CHECKPOINT-2: filter drop summary
    for bucket, victims in drop_buckets.items():
        _log.info(
            f"CHECKPOINT-2 filter={bucket:<22} dropped={len(victims):>5}  "
            f"sample3={victims[:3]}"
        )
    # CHECKPOINT-3: final tallies
    _log.info(
        f"CHECKPOINT-3 total_processed={n_total}  enrolled={n_enrolled}  "
        f"dropped={n_total-n_enrolled}  saved_to_db={n_total}"
    )

    return programs


def _build_series_capture_ratios(db_path: str) -> dict:
    """TIER 2A: per-series (capture_pct, fill_rate) from historical data."""
    import sqlite3 as _sq
    out = {}
    try:
        conn = _sq.connect(db_path, timeout=5.0)
        rb = conn.execute("""
            SELECT series_prefix, COUNT(*) AS n,
                   ROUND(AVG(rebate_earned_usd), 4) AS avg_r
            FROM settlement_log
            WHERE datetime(close_time) > datetime('now','-14 days')
            GROUP BY series_prefix
        """).fetchall()
        fr = conn.execute("""
            SELECT substr(market_ticker,1,instr(market_ticker||'-','-')-1) AS s,
                   COUNT(*) AS placed,
                   SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) AS filled
            FROM quotes
            WHERE paper=0 AND placed_at > datetime('now','-14 days')
            GROUP BY s HAVING placed > 0
        """).fetchall()
        fmap = {r[0]: (r[2] / r[1] if r[1] > 0 else 0) for r in fr}
        for sp, n, avg_r in rb:
            out[sp] = {
                "capture": min(1.0, (avg_r or 0) / 6.0),
                "fill_rate": fmap.get(sp, 0),
                "avg_rebate": avg_r or 0,
                "n": n,
            }
        conn.close()
    except Exception:
        pass
    return out


def top_n_to_quote(n: int = 100, max_target_size: int = 2500,
                   db_path: str = settings.DB_PATH,
                   exclude_tickers: set[str] | None = None) -> list[dict]:
    """Return top-N enrolled REACHABLE markets ranked by PRIORITY-WEIGHTED reward.

    2026-05-01 PREDATOR — Saturation-aware filtering:
      Pass `exclude_tickers` (set of saturated tickers from runner) to skip
      markets where existing positions/orders consume >80% of per-market cap
      OR existing exposure is already >= bankroll_share_cap. Without this
      filter, the ranker keeps surfacing the same saturated markets at top,
      blocking access to lower-ranked markets that have room. Result: 4 of
      30 top picks attacked, 26 starved. With filter: ranker output IS the
      attack list.

    2026-04-28 (#100 EV-PRIORITY V2):
      First attempt used observed share (our_score/total_score) as an EV
      multiplier, but the snapshot share metric understates true rebate
      capture by 50-100x — it's per-second snapshot fraction, not
      time-integrated $-flow. With share=6e-05, even MAMDANIEO ranked
      below random political markets at the 0.20 prior. Net effect: would
      have crowded out proven winners (BRENTD/CORNW/COPPERD per Apr 27
      audit) for unproven Trump-time/endorsement markets.

      V2 ranks by reward × series_priority. Series priority comes from the
      same SIZE_MULTIPLIER_BY_SERIES table that tier-2x's our quote sizes
      (#119) — known winners get ranking lift, defaults to 1.0. Filters
      (end_date past, blacklist, target_size cap) still apply.

    Filters:
      1. enrolled=1 AND paid_out=0
      2. target_size <= max_target_size
      3. NOT in active market_blacklist
      4. end_date > today (don't quote settled markets)
    """

    # 2026-05-08 TIER1E: ATTACK_TARGETS as primary source. Uses live Kalshi
    # /incentive_programs API + competitor_density + auto-prune blacklist.
    # This is THE source-of-truth — no more algorithmic ranker drift.
    # Falls back to legacy SQL path if attack_targets fails.
    try:
        from tools.attack_targets import compute_attack_targets
        targets = compute_attack_targets(top_n=n * 5, db_path=db_path,
                                         max_target_size=max_target_size)
        if targets:
            ex = exclude_tickers or set()
            tconn = sqlite3.connect(db_path, timeout=5.0)
            try:
                out_rows = []
                for t in targets:
                    tk = t.get("market_ticker") or ""
                    if not tk or tk in ex:
                        continue
                    row = tconn.execute(
                        "SELECT market_ticker, series_ticker, reward_per_day_usd, "
                        "target_size, discount_factor, start_date, end_date "
                        "FROM lip_programs WHERE market_ticker = ? AND enrolled = 1 "
                        "AND paid_out = 0 AND datetime(end_date) > datetime('now') "
                        "AND target_size <= ?",
                        (tk, max_target_size),
                    ).fetchone()
                    if not row:
                        continue
                    out_rows.append({
                        "market_ticker":         row[0],
                        "series_ticker":         row[1],
                        "reward_per_day_usd":    row[2],
                        "target_size":           row[3],
                        "discount_factor":       row[4],
                        "start_date":            row[5],
                        "end_date":              row[6],
                        "series_priority":       1.0,
                        "competition_mult":      1.0,
                        "observed_share":        t.get("observed_share"),
                        "realized_mult":         1.0,
                        "unified_rebate":        round(t.get("expected_net_per_day", 0), 4),
                        "series_realized_net_14d": 0.0,
                        "series_settlements_14d": 0,
                        "priority_weighted_reward": round(t.get("attack_score", 0), 4),
                        "attack_priority":       t.get("attack_priority", "?"),
                        "expected_net_per_day":  round(t.get("expected_net_per_day", 0), 4),
                    })
                    # don't break early — let TIER1F prune from full set
                    if len(out_rows) >= n * 4:  # cap at 4n for memory
                        break
                if out_rows:
                    # 2026-05-09 TIER 2A: tag each market with capture_score
                    capture_map = _build_series_capture_ratios(db_path)
                    for m in out_rows:
                        s = m.get("series_ticker", "")
                        info = capture_map.get(s, {})
                        cap = info.get("capture", 0.10)  # default 10% for unproven
                        fr = info.get("fill_rate", 0.10)
                        m["_capture_score"] = (m.get("reward_per_day_usd", 0) or 0) * \
                                              max(0.05, cap) * max(0.10, fr)
                        m["_capture_pct"] = cap
                        m["_fill_rate"] = fr
                    # 2026-05-08 TIER1G SNIPER MODE
                    # 1. Drop low-reward variants within each series (top-K + 60% threshold)
                    # 2. Apply $25/d reward floor
                    # 3. Re-rank by capture_score (TIER 2A) — proven > theoretical
                    PER_SERIES_TOP_K = 3
                    REWARD_DOMINANCE_THRESHOLD = 0.60
                    MIN_REWARD_PER_DAY = 25.0
                    from collections import defaultdict as _dd
                    by_series = _dd(list)
                    for m in out_rows:
                        by_series[m.get("series_ticker", "")].append(m)
                    pruned = []
                    dropped_var = 0
                    dropped_floor = 0
                    for series, mkts in by_series.items():
                        if len(mkts) > 1:
                            max_r = max((m["reward_per_day_usd"] or 0) for m in mkts)
                            if max_r > 0:
                                sb = sorted(mkts, key=lambda m: -(m["reward_per_day_usd"] or 0))
                                kept = []
                                for i, m in enumerate(sb):
                                    r = m["reward_per_day_usd"] or 0
                                    if i < PER_SERIES_TOP_K or r >= max_r * REWARD_DOMINANCE_THRESHOLD:
                                        kept.append(m)
                                    else:
                                        dropped_var += 1
                                mkts = kept
                        for m in mkts:
                            if (m.get("reward_per_day_usd") or 0) < MIN_REWARD_PER_DAY:
                                dropped_floor += 1
                            else:
                                pruned.append(m)
                    pruned.sort(key=lambda m: -(m.get("_capture_score", 0) or 0))
                    out_rows = pruned[:n]
                    top_rwd = (out_rows[0].get("reward_per_day_usd", 0) if out_rows else 0)
                    top_cap = (out_rows[0].get("_capture_score", 0) if out_rows else 0)
                    top_series = (out_rows[0].get("series_ticker", "?") if out_rows else "?")
                    _log.info(
                        f"TIER 2A CAPTURE: {len(out_rows)} mkts "
                        f"(low_var={dropped_var}, below_floor={dropped_floor}); "
                        f"top={top_series} reward=${top_rwd:.2f}/d capture=${top_cap:.2f}"
                    )
                    return out_rows
            finally:
                tconn.close()
    except Exception as _e:
        _log.warning(f"attack_targets path failed, falling back to legacy: {_e}")


    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        try:
            rows = conn.execute(
                """SELECT p.market_ticker, p.series_ticker, p.reward_per_day_usd,
                          p.target_size, p.discount_factor, p.start_date, p.end_date
                   FROM lip_programs p
                   LEFT JOIN market_blacklist b
                     ON p.market_ticker = b.ticker
                     AND datetime(b.expires_at) > datetime('now')
                   WHERE p.enrolled = 1 AND p.paid_out = 0
                     AND p.target_size <= ?
                     AND datetime(p.end_date) > datetime('now')
                     AND b.ticker IS NULL
                   ORDER BY p.reward_per_day_usd DESC
                   LIMIT ?""",
                (max_target_size, n * 3),  # over-fetch so priority can re-sort top-n
            ).fetchall()
        except sqlite3.OperationalError as e:
            # 2026-05-02 PREDATOR K5: FAIL-LOUD instead of falling back.
            # The fallback path silently quoted SETTLED + BLACKLISTED markets
            # for hours when the primary SQL had a typo. Better to skip ONE
            # quoting cycle (returning [] = quote loop pauses) than to dump
            # capital into known-bad markets undetected. Same philosophy
            # as the recent pm_rebate_verifier deposit bug — silent failures
            # cost real money over real time.
            _log.error(f"🔴 top_n_to_quote PRIMARY QUERY FAILED — failing closed: {e}")
            try:
                from monitor.alerts import alert as _alert
                _alert(
                    "ERROR",
                    "lip_discovery",
                    f"top_n_to_quote SQL failed: {e}. Returned EMPTY list this "
                    f"cycle. Quoting paused until SQL fixed.",
                )
            except Exception as _alert_err:
                _log.warning(f"alert dispatch failed: {_alert_err}")
            return []
    finally:
        conn.close()

    # Re-rank by reward × series priority × COMPETITION × REALIZED YIELD.
    # NEXUS Ship 1 (2026-04-29): per-series realized rebate from settlement_log
    # over last 14d as ground-truth multiplier. Markets that ACTUALLY paid us
    # rank higher than markets that look good on paper. Avoids the "theoretical
    # EV" trap where competition_mult understates and pool_reward overstates.
    multipliers = settings.SIZE_MULTIPLIER_BY_SERIES
    default = settings.DEFAULT_SIZE_MULTIPLIER

    density_map: dict[str, dict] = {}
    try:
        from tools.competitor_density import scan as _density_scan
        for d in _density_scan(db_path=db_path):
            tkr = d.get("market_ticker") if isinstance(d, dict) else None
            if tkr:
                density_map[tkr] = d
    except Exception as _e:
        logging.getLogger(__name__).debug(f"competitor_density unavailable: {_e}")

    # Per-series realized rebate density (last 14d)
    realized_series: dict[str, dict] = {}
    try:
        with sqlite3.connect(db_path, timeout=5.0) as rconn:
            for r in rconn.execute("""
                SELECT series_prefix,
                       COUNT(*)                                AS n,
                       COALESCE(SUM(rebate_earned_usd), 0)     AS rebate,
                       COALESCE(SUM(our_realized_usd), 0)      AS realized,
                       COALESCE(SUM(net_outcome_usd), 0)       AS net
                FROM settlement_log
                WHERE datetime(close_time) > datetime('now','-14 days')
                GROUP BY series_prefix
            """).fetchall():
                realized_series[r[0]] = {
                    "n": r[1], "rebate": r[2], "realized": r[3], "net": r[4],
                }
    except Exception as _e:
        logging.getLogger(__name__).debug(f"realized_series unavailable: {_e}")

    # 2026-04-30: import unified yield equation for cross-venue parity
    try:
        from engine.yield_equation import MarketYield
        _have_yield_eq = True
    except Exception:
        _have_yield_eq = False

    from datetime import datetime as _dt, timezone as _tz

    enriched = []
    for r in rows:
        series = r[1] or ""
        priority = multipliers.get(series, default)
        d = density_map.get(r[0], {})
        observed_share = d.get("share") if isinstance(d, dict) else None
        if observed_share is not None and observed_share > 0:
            comp_mult = max(0.25, min(3.0, observed_share / 0.20))
        else:
            comp_mult = 1.0
        # Realized yield multiplier
        rs = realized_series.get(series, {})
        n_settled = rs.get("n", 0)
        series_net = rs.get("net", 0.0)
        # 2026-05-08 TIER1D: ATTACK MODE — only EXCLUDE proven LOSERS;
        # unproven HIGH-REWARD markets are the actual money makers
        # (KXMAKARYOUT $50/d, KXUSPPI $30/d, KXMEDIARELEASEICEMAN — all
        # unproven but low-comp + high-pool = our share is huge).
        # Auto-prune cron handles known losers (already in market_blacklist).
        if n_settled >= 3:
            net_per_settle = series_net / n_settled
            # Proven LOSER (n>=3 + negative net): EXCLUDE.
            # Whitelist override: commodities allowed even if -ve (physics edge).
            if series_net < 0 and series not in multipliers:
                continue
            realized_mult = max(0.5, min(2.0, 1.0 + net_per_settle / 4.0))
        else:
            # Unproven: take it but at 0.7x penalty so proven winners win ties.
            realized_mult = 0.7
        # 2026-05-08 TIER1C-FIX: REMOVED time-weight (was dividing by
        # days_to_settle, which kicked out the actual rebate machine —
        # commodity weeklies + episodic events that settle in 5-7d but
        # pay big on settle day). Empirical evidence (Apr 24 = $207 rebates
        # from commodity weeklies, May 5 = $48 from Met Gala) showed
        # weekly settles ARE the income, not intraday churn.
        # Keep proven gate, drop time-weight.
        weighted = (r[2] or 0.0) * priority * comp_mult * realized_mult

        # NEW: parallel Unified Yield Equation projection (audit/visibility)
        # Approximations: our_size from settings, top_book = 50% of target.
        # Real precision would require per-market book lookup (expensive in
        # this hot path); current proxies match Kalshi calibration empirics.
        unified_rebate = 0.0
        if _have_yield_eq:
            try:
                target_size = int(r[3] or 1000)
                df_value = float(r[4] or 0.50)
                end_iso = r[6] or ""
                hours = 24.0
                try:
                    ed = _dt.fromisoformat(end_iso.replace("Z", "+00:00"))
                    hours = max(0.5, (ed - _dt.now(_tz.utc)).total_seconds() / 3600)
                except Exception:
                    pass
                our_size = int(getattr(settings, "DEFAULT_QUOTE_SIZE_CONTRACTS",
                                       getattr(settings, "MIN_QUOTE_SIZE_CONTRACTS", 100)))
                top_book_estimate = max(int(target_size * 0.5), our_size)
                y = MarketYield(
                    market_id=r[0],
                    pool_per_day=float(r[2] or 0),
                    our_size=our_size,
                    top_book_size=top_book_estimate,
                    target_size=target_size,
                    discount_factor=df_value,
                    hours_to_settle=hours,
                    midpoint=0.5,
                    calibration=0.25,
                    series_priority=priority * comp_mult * realized_mult,
                    observed_share=observed_share,
                )
                unified_rebate = y.expected_daily_rebate
            except Exception:
                pass
        enriched.append({
            "market_ticker":         r[0],
            "series_ticker":         r[1],
            "reward_per_day_usd":    r[2],
            "target_size":           r[3],
            "discount_factor":       r[4],
            "start_date":            r[5],
            "end_date":              r[6],
            "series_priority":       priority,
            "competition_mult":      round(comp_mult, 3),
            "observed_share":        observed_share,
            "realized_mult":         round(realized_mult, 3),
            "unified_rebate":        round(unified_rebate, 4),
            "series_realized_net_14d": round(series_net, 2),
            "series_settlements_14d": n_settled,
            "priority_weighted_reward": round(weighted, 4),
        })
    # 2026-04-30: SORT BY UNIFIED_REBATE (physics equation) when populated.
    # Fall back to legacy weighted when unified_rebate is 0 (e.g. data missing).
    # Unified equation factors in time_decay + adverse_cost + qualify_prob —
    # same math as PM. Side-by-side A/B for next 7d, then drop legacy.
    # 2026-05-08 TIER1F: Per-contract reward dominance within series.
    # Iceman insight: a series can have 4-10x reward variance per contract
    # (HANTACOUNTRY GER/UK/USA $90/d vs CAN/CHI/IND/JAP $20/d). The
    # comp_mult was DOWN-weighting high-reward thin-liquidity contracts
    # (the OPPOSITE of what we want). Fix: drop low-reward variants
    # within each series, keep only top-K by raw reward_per_day_usd.
    PER_SERIES_TOP_K = 3
    REWARD_DOMINANCE_THRESHOLD = 0.60  # keep contracts with reward >= 60% of series max
    from collections import defaultdict
    by_series = defaultdict(list)
    for m in enriched:
        by_series[m.get("series_ticker", "")].append(m)
    pruned = []
    dropped_low_variants = 0
    for series, mkts in by_series.items():
        if len(mkts) <= 1:
            pruned.extend(mkts)
            continue
        max_reward = max(m["reward_per_day_usd"] or 0 for m in mkts)
        if max_reward <= 0:
            pruned.extend(mkts)
            continue
        # Keep TOP-K AND any contract with reward >= threshold of series max
        sorted_by_reward = sorted(mkts, key=lambda m: -(m["reward_per_day_usd"] or 0))
        kept = []
        for i, m in enumerate(sorted_by_reward):
            r = m["reward_per_day_usd"] or 0
            if i < PER_SERIES_TOP_K or r >= max_reward * REWARD_DOMINANCE_THRESHOLD:
                kept.append(m)
            else:
                dropped_low_variants += 1
        pruned.extend(kept)
    if dropped_low_variants > 0:
        _log.info(f"TIER1F: dropped {dropped_low_variants} low-reward variants within series "
                  f"(kept top-{PER_SERIES_TOP_K} or >={int(REWARD_DOMINANCE_THRESHOLD*100)}% of series max)")
    enriched = pruned

    def _rank_key(d):
        u = d.get("unified_rebate", 0) or 0
        # 2026-05-08 TIER1F: Within same composite score, prefer higher raw reward.
        return (-(u if u > 0 else d.get("priority_weighted_reward", 0) / 100),
                -(d.get("reward_per_day_usd", 0) or 0))
    enriched.sort(key=_rank_key)
    # 2026-05-01 PREDATOR: filter saturated markets BEFORE truncating to n.
    # Logs what was filtered so we can verify the gate is doing right work.
    if exclude_tickers:
        before = len(enriched)
        filtered = [m for m in enriched if m["market_ticker"] not in exclude_tickers]
        skipped = before - len(filtered)
        if skipped > 0:
            _log.info(f"top_n_to_quote: saturation filter removed {skipped} "
                      f"market(s) from {before} → {len(filtered)} candidates")
        return filtered[:n]
    return enriched[:n]


def report_enrolled(db_path: str = settings.DB_PATH) -> None:
    """Print what we'd quote."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT market_ticker, series_ticker, reward_per_day_usd,
                      target_size, discount_factor
               FROM lip_programs
               WHERE enrolled = 1 AND paid_out = 0
               ORDER BY reward_per_day_usd DESC"""
        ).fetchall()
        total_reward = conn.execute(
            "SELECT COALESCE(SUM(reward_per_day_usd), 0) FROM lip_programs WHERE enrolled = 1 AND paid_out = 0"
        ).fetchone()[0]
        # Top-30 view — where we'd actually quote
        top30_reward = sum(r[2] for r in rows[:30])
        blocked_rows = conn.execute(
            """SELECT blocked_reason, COUNT(*) FROM lip_programs
               WHERE enrolled = 0 GROUP BY blocked_reason ORDER BY 2 DESC"""
        ).fetchall()
        # Series breakdown of enrolled
        series_rows = conn.execute(
            """SELECT series_ticker, COUNT(*) as n, SUM(reward_per_day_usd) as rwd
               FROM lip_programs
               WHERE enrolled = 1 AND paid_out = 0
               GROUP BY series_ticker
               ORDER BY rwd DESC
               LIMIT 15"""
        ).fetchall()
    finally:
        conn.close()

    print(f"Enrolled markets: {len(rows)}  Total daily reward pool: ${total_reward:.2f}")
    print(f"Top-30 by reward-per-day: ${top30_reward:.2f}/day total")
    print(f"  at 10% realized share = ~${top30_reward * 0.10:.2f}/day = ~${top30_reward * 0.10 * 30:.0f}/month")
    print()
    print("Top 20 markets:")
    for tkr, series, rwd, ts, df in rows[:20]:
        print(f"  {tkr:40s} series={series:20s} ${rwd:>6.2f}/day target={ts:>6.0f} df={df:.2f}")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")
    print()
    print("Enrolled by series (top 15):")
    for series, n, rwd in series_rows:
        print(f"  {series:20s}: {n:3d} markets  ${rwd:>7.2f}/day total")
    print()
    print("Blocked (top reasons):")
    for reason, n in blocked_rows[:10]:
        print(f"  {reason}: {n}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    programs = discover()
    print(f"fetched {len(programs)} active liquidity programs")
    print()
    report_enrolled()
