#!/usr/bin/env python3
"""Matched paper comparison of entry-cutoff policies. PAPER ONLY.

    python tools/run_experiment.py --capture-sec 900

Two phases:

  CAPTURE  one pass over the live venue, recording orderbook snapshots and
           public trades to JSONL with timestamps and provenance.
  REPLAY   the SAME captured stream is replayed through one runner per
           policy arm, each with its own independent $5,000 account and its
           own database.

Because every arm sees identical data, a difference between arms is
attributable to the policy rather than to different markets, a different
moment, or a shared balance.

Honesty about replay
--------------------
Replay is faster than wall-clock. Anything measured in TIME is therefore
measured in STREAM time (captured timestamps), never in replay wall time.
Book staleness is evaluated against the replay clock so the runner's gates
behave as they would live.

Nothing is fabricated: no fill is generated that a captured public trade
does not support, and an arm that quotes nothing reports nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from unittest import mock
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine import experiment_spec as SPEC
from engine.entry_cutoff import ALL_POLICIES, policy_fingerprint
from engine.lip_discovery import discover_result
from engine.market_clock import MarketClock
from execution.kalshi_ws import BookLevel, BookState, FillEvent
from execution.paper_fills import PaperFillSimulator
from execution.rest_book_feed import RestBookFeed, _cents
import run_paper as rp
from run_paper import PaperRunner, _program_params_from_market

_log = logging.getLogger("experiment")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── market selection (frozen spec) ────────────────────────────────────────

def select_markets(reuse: bool, top_scan: int = 400) -> tuple[list, dict]:
    if reuse:
        conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM lip_programs WHERE COALESCE(paid_out,0)=0")]
        conn.close()
        disc = {"source": "persisted", "rows": len(rows)}
    else:
        d = discover_result(save=True)
        rows = d.programs
        disc = {"source": "live_scan", "rows": len(rows),
                "complete": d.complete, "rejected": d.n_rejected,
                "collisions": d.n_collisions}

    now = time.time()
    cands = []
    seen = set()
    for m in rows:
        t = m.get("market_ticker")
        if not t or t in seen:
            continue
        try:
            pp = _program_params_from_market(m)
        except Exception:
            continue
        if not pp.start_ts or not pp.end_ts or pp.end_ts <= now:
            continue
        # Cheap pre-filter: a program that has not STARTED cannot be quoted
        # now, and its market is usually not even open. Doing this before
        # the per-market API call keeps the scan inside rate limits.
        if pp.start_ts > now:
            continue
        if pp.period_reward_usd <= 0 or pp.target_size <= 0:
            continue
        seen.add(t)
        cands.append((pp.pool_rate_usd_per_sec, m))
    cands.sort(key=lambda x: -x[0])

    clock = MarketClock()
    feed = RestBookFeed()
    chosen: dict[str, list] = {s: [] for s in SPEC.STRATA}
    examined = 0
    excluded = {"unknown_duration": 0, "one_sided_book": 0, "no_book": 0,
                "market_not_open": 0}
    for rate, m in cands[:top_scan]:
        if all(len(v) >= SPEC.N_PER_STRATUM for v in chosen.values()):
            break
        t = m["market_ticker"]
        examined += 1
        ct = clock.close_time(t)
        stratum = SPEC.stratum_for(ct.duration_min)
        if stratum is None:
            excluded["unknown_duration"] += 1
            continue
        if SPEC.REQUIRE_MARKET_OPEN_NOW:
            # Program start, market open and market close are three
            # different clocks. The highest-rate programs are often for
            # windows hours ahead whose markets have not opened, so their
            # books are empty and they cannot be quoted now.
            if ct.open_ts is None or not (ct.open_ts <= now < (ct.close_ts or 0)):
                excluded["market_not_open"] = excluded.get("market_not_open", 0) + 1
                continue
        if len(chosen[stratum]) >= SPEC.N_PER_STRATUM:
            continue
        try:
            payload = feed._fetch_sync(t)
        except Exception:
            excluded["no_book"] += 1
            continue
        inner = (payload or {}).get("orderbook_fp") or {}
        y, n = inner.get("yes_dollars") or [], inner.get("no_dollars") or []
        if SPEC.REQUIRE_TWO_SIDED_BOOK and not (y and n):
            excluded["one_sided_book"] += 1
            continue
        m = dict(m)
        m["_stratum"] = stratum
        m["_pool_rate"] = rate
        m["_duration_min"] = ct.duration_min
        m["_close_ts"] = ct.close_ts
        m["_open_ts"] = ct.open_ts
        chosen[stratum].append(m)

    picked = [m for v in chosen.values() for m in v]
    return picked, {"discovery": disc, "examined": examined,
                    "excluded": excluded,
                    "per_stratum": {k: len(v) for k, v in chosen.items()}}


# ── capture ───────────────────────────────────────────────────────────────

async def capture(markets, seconds: float, out_path: Path,
                  poll_sec: float, trade_poll_sec: float) -> dict:
    tickers = [m["market_ticker"] for m in markets]
    feed = RestBookFeed(poll_interval_sec=poll_sec)
    sim = PaperFillSimulator()          # used only as a trade fetcher here
    events: list[dict] = []

    async def on_book(book: BookState):
        events.append({"t": time.time(), "kind": "book",
                       "ticker": book.market_ticker,
                       "yes": [[l.price_cents, l.size] for l in book.yes_bids],
                       "no": [[l.price_cents, l.size] for l in book.no_bids]})

    feed.on_update(on_book)
    await feed.subscribe_orderbook(tickers)
    stop = time.time() + seconds
    feeder = asyncio.create_task(feed.run(stop_after_sec=seconds))
    trade_ids = set()
    try:
        while time.time() < stop:
            await asyncio.sleep(trade_poll_sec)
            trades = await asyncio.to_thread(sim.fetch_trades, tickers)
            fresh = [tr for tr in trades if tr.get("trade_id") not in trade_ids]
            for tr in fresh:
                trade_ids.add(tr["trade_id"])
            if fresh:
                events.append({"t": time.time(), "kind": "trades",
                               "trades": fresh})
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)

    events.sort(key=lambda e: e["t"])
    with out_path.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    books = sum(1 for e in events if e["kind"] == "book")
    trs = sum(len(e["trades"]) for e in events if e["kind"] == "trades")
    return {"events": len(events), "book_snapshots": books,
            "trades": trs, "polls": feed.polls,
            "fetch_errors": feed.fetch_errors,
            "stream_sec": (events[-1]["t"] - events[0]["t"]) if events else 0.0,
            "path": str(out_path)}


# ── replay one arm ────────────────────────────────────────────────────────

async def replay_arm(policy: str, markets, events, workdir: Path) -> dict:
    db = workdir / f"{policy}.db"
    import init_db as _init
    orig_db = settings.DB_PATH
    settings.DB_PATH = str(db)
    try:
        _init.main() if hasattr(_init, "main") else None
    except Exception:
        pass
    conn = sqlite3.connect(db)
    conn.executescript(_init.SCHEMA if hasattr(_init, "SCHEMA") else "")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fill_ledger (
            trade_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT, side TEXT,
            count INTEGER, yes_price_cents INTEGER, no_price_cents INTEGER,
            is_taker INTEGER, created_at TEXT, synced_at TEXT);
        CREATE TABLE IF NOT EXISTS settlement_log (ticker TEXT PRIMARY KEY,
            settled_at TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS market_blacklist (ticker TEXT PRIMARY KEY,
            expires_at TEXT, reason TEXT);
    """)
    conn.commit(); conn.close()

    try:
        runner = PaperRunner([dict(m) for m in markets])
        runner.qm.paper = True
        runner.qm.db_path = str(db)
        runner.qm.resting = {}
        runner.entry_cutoff_policy = policy
        runner.last_complete_scan_ts = time.time()
        # Captured close/open times; no network during replay.
        times = {m["market_ticker"]: (m.get("_open_ts"), m.get("_close_ts"))
                 for m in markets}

        def fetcher(t):
            o, c = times.get(t, (None, None))
            iso = lambda x: (datetime.fromtimestamp(x, tz=timezone.utc)
                             .isoformat().replace("+00:00", "Z")) if x else None
            return {"open_time": iso(o), "close_time": iso(c)}
        runner.market_clock = MarketClock(fetcher=fetcher)

        # Replay runs far faster than wall clock, but the runner's throttles
        # (scoring, reprice, persist) are wall-clock based. Left alone they
        # suppress almost every evaluation and the few that survive happen
        # at the start, when nothing has been observed yet — which is how an
        # earlier run recorded "measured=False" after 491 seconds of data.
        # Driving time.time() from the STREAM makes those throttles behave
        # as they would live.
        clock = {"now": events[0]["t"] if events else time.time()}
        real_time = time.time

        def stream_now():
            return clock["now"]

        sim = PaperFillSimulator(latency_ms=250.0)
        books: dict[str, BookState] = {}
        runner.books = books
        quotes: list[dict] = []
        refusals: dict[str, int] = {}
        seen_orders: set[str] = set()
        fills_applied = 0
        qualified_stream_sec = 0.0
        last_t = None
        t0 = events[0]["t"] if events else time.time()

        patcher = mock.patch("time.time", stream_now)
        patcher.start()
        try:
          for ev in events:
            stream_t = ev["t"]
            clock["now"] = stream_t
            if last_t is not None:
                dt = stream_t - last_t
                # Qualified resting time, in STREAM seconds.
                resting_now = sum(len(v) for v in runner.qm.resting.values())
                if resting_now:
                    qualified_stream_sec += dt
            last_t = stream_t

            if ev["kind"] == "book":
                tkr = ev["ticker"]
                b = books.get(tkr) or BookState(market_ticker=tkr)
                b.yes_bids = [BookLevel(int(p), float(s)) for p, s in ev["yes"]]
                b.no_bids = [BookLevel(int(p), float(s)) for p, s in ev["no"]]
                b.last_update_ts = time.time()     # replay clock, for staleness
                b.snapshot_count += 1
                b.stale = False
                books[tkr] = b
                # Watching counts as observation even when nothing trades:
                # silence is a measurement, and without this a quiet market
                # has no window and falls back to the placeholder.
                runner.execution_model.observe_market(tkr, ts=stream_t)
                try:
                    await runner.on_book_update(b)
                except Exception as e:
                    _log.debug(f"[{policy}] on_book_update {tkr}: {e}")
                r = runner._skip_reason.get(tkr)
                if r:
                    refusals[r] = refusals.get(r, 0) + 1
                for o in runner.qm.resting.get(tkr, []):
                    if o.order_id in seen_orders:
                        continue
                    seen_orders.add(o.order_id)
                    sim.track(order_id=o.order_id, market_ticker=tkr,
                              side=o.side, price_cents=o.price_cents,
                              size=float(o.size_contracts), book=b,
                              program_id=getattr(o, "program_id", ""),
                              now=0.0)
                    quotes.append({
                        "stream_offset_sec": round(stream_t - t0, 2),
                        "ticker": tkr, "side": o.side,
                        "price_cents": o.price_cents,
                        "size": float(o.size_contracts),
                        "program_id": getattr(o, "program_id", ""),
                        "stratum": next((m.get("_stratum") for m in markets
                                         if m["market_ticker"] == tkr), None),
                        "economics": runner._econ_last.get(tkr, {}),
                        "execution_estimate": runner._exec_last.get(tkr, {}),
                    })
            else:
                trades = ev["trades"]
                runner.execution_model.observe_trades(trades)
                for f in sim.apply_trades(trades):
                    e = FillEvent(order_id=f["order_id"],
                                  market_ticker=f["market_ticker"],
                                  side=f["side"], count=float(f["count"]),
                                  price_cents_exact=float(f["price_cents"]),
                                  is_taker=False, trade_id=f["trade_id"],
                                  ts=f["ts"])
                    try:
                        runner.on_fill(e)
                        fills_applied += 1
                    except Exception as ex:
                        _log.debug(f"[{policy}] on_fill: {ex}")

        finally:
            patcher.stop()

        try:
            runner.manage_exits(real_time())
        except Exception as e:
            _log.debug(f"[{policy}] manage_exits: {e}")

        return _arm_report(policy, runner, sim, quotes, refusals,
                           fills_applied, qualified_stream_sec, markets)
    finally:
        settings.DB_PATH = orig_db


def _arm_report(policy, runner, sim, quotes, refusals, fills_applied,
                qualified_stream_sec, markets) -> dict:
    acct = runner.account.state()
    inv = {}
    for m in markets:
        t = m["market_ticker"]
        pos = runner._position_for(t)
        if pos is not None and (pos.yes_qty or pos.no_qty):
            inv[t] = {"yes": float(pos.yes_qty), "no": float(pos.no_qty),
                      "net_yes": float(pos.net_yes),
                      "paired": float(pos.paired),
                      "stratum": m.get("_stratum")}
    rewards = {k: round(st.accrued_usd, 6)
               for k, st in runner._accrual.items()}
    by_stratum = {}
    for q in quotes:
        s = q.get("stratum") or "unknown"
        by_stratum[s] = by_stratum.get(s, 0) + 1
    return {
        "policy": policy,
        "cutoffs_applied": dict(runner._cutoff_seen),
        "opportunities": {
            "markets": len(markets),
            "refusal_reasons": refusals,
            "economic_rejects": runner.econ_rejects,
            "unknown_close_skips": runner.unknown_close_skips,
        },
        "quoting": {"quotes": len(quotes), "by_stratum": by_stratum,
                    "detail": quotes[:100]},
        # Every candidate considered, including the ones refused. A refusal
        # without its decomposition is not a result anyone can check.
        "candidates_considered": {
            t: {"economics": runner._econ_last.get(t, {}),
                "execution_estimate": runner._exec_last.get(t, {}),
                "stratum": next((m.get("_stratum") for m in markets
                                 if m["market_ticker"] == t), None)}
            for t in sorted(set(list(runner._econ_last)
                                + list(runner._exec_last)))},
        "placement": {
            "capital_refusals": runner.qm.capital_refusals,
            "live_blocked": runner.qm.live_blocked,
            "skip_counts": dict(runner.skip_counts),
            "safety_blocks": getattr(runner.qm, "safety_blocks", None),
        },
        "execution": {"trades_observed": sim.trades_observed,
                      "modelled_fills": sim.fills_generated,
                      "fills_applied": fills_applied,
                      "still_resting": len(sim.orders)},
        "qualified_resting_stream_sec": round(qualified_stream_sec, 1),
        "account": {"cash_usd": float(acct.cash_usd),
                    "reserved_usd": float(acct.reserved_usd),
                    "inventory_cost_usd": float(acct.inventory_cost_usd),
                    "available_usd": float(runner.account.available_usd())},
        "inventory": inv,
        "exits": {"actions": runner.exit_actions,
                  "reasons": dict(runner._exit_reasons)},
        "observation": runner.execution_model.summary(),
        "reward_estimates_only": rewards,
        "reward_payments": {},        # none: nothing has been paid
    }


# ── main ──────────────────────────────────────────────────────────────────

async def run(args) -> dict:
    markets, sel = select_markets(args.reuse_discovery)
    if not markets:
        return {"error": "no markets met the frozen selection spec",
                "selection": sel}
    _log.info(f"selected {len(markets)} markets: {sel['per_stratum']}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stream_path = outdir / f"stream-{stamp}.jsonl"

    if args.stream:
        # Replay an existing capture. Same data, so a change to the runner
        # can be compared against a previous run without the market moving
        # underneath it.
        stream_path = Path(args.stream)
        events = [json.loads(l) for l in stream_path.read_text().splitlines() if l]
        cap = {"events": len(events), "reused": True, "path": str(stream_path),
               "book_snapshots": sum(1 for e in events if e["kind"] == "book"),
               "trades": sum(len(e["trades"]) for e in events
                             if e["kind"] == "trades"),
               "polls": None, "fetch_errors": None,
               "stream_sec": (events[-1]["t"] - events[0]["t"]) if events else 0.0}
        _log.info(f"reusing capture {stream_path} ({cap['book_snapshots']} "
                  f"snapshots, {cap['trades']} trades)")
        tickers_in_stream = {e["ticker"] for e in events if e["kind"] == "book"}
        markets = [m for m in markets if m["market_ticker"] in tickers_in_stream]
        if not markets:
            return {"error": "no selected market appears in the reused stream",
                    "selection": sel, "capture": cap}
    else:
        _log.info(f"capturing {args.capture_sec}s ...")
        cap = await capture(markets, args.capture_sec, stream_path,
                            args.poll_sec, args.trade_poll_sec)
        _log.info(f"captured {cap['book_snapshots']} snapshots, {cap['trades']} trades")
        events = [json.loads(l) for l in stream_path.read_text().splitlines() if l]
    arms = {}
    workdir = outdir / f"arms-{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    for policy in ALL_POLICIES:
        _log.info(f"replaying arm: {policy}")
        arms[policy] = await replay_arm(policy, markets, events, workdir)

    return {
        "utc": _utc(),
        "spec": SPEC.describe(),
        "policy_fingerprint": policy_fingerprint(),
        "selection": sel,
        "markets": [{"ticker": m["market_ticker"], "stratum": m["_stratum"],
                     "pool_rate_usd_per_sec": round(m["_pool_rate"], 8),
                     "duration_min": round(m["_duration_min"], 2)}
                    for m in markets],
        "capture": cap,
        "arms": arms,
        "baseline": {"policy": "do_nothing",
                     "net_usd": 0.0,
                     "note": "not quoting is always available and costs nothing"},
        "caveats": [
            "Rewards are ESTIMATES. Nothing has been paid or reconciled.",
            "Fills are MODELLED from captured public trades with a "
            "conservative queue assumption; they are not observed fills of "
            "our orders, which do not exist.",
            "REST snapshots have no sequence and no queue position, so "
            "execution modelling is the weakest part of this evidence.",
            "Replay is faster than wall clock; all durations are STREAM "
            "seconds from captured timestamps.",
            "A bounded capture cannot span complete incentive periods for "
            "long-dated markets; their reward figures are partial.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-sec", type=float, default=900.0)
    ap.add_argument("--poll-sec", type=float, default=5.0)
    ap.add_argument("--trade-poll-sec", type=float, default=15.0)
    ap.add_argument("--outdir", default="data/experiments")
    ap.add_argument("--reuse-discovery", action="store_true")
    ap.add_argument("--stream", default="",
                    help="replay an existing stream-*.jsonl instead of capturing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.PAPER_MODE:
        print("REFUSING: PAPER_MODE is off. This experiment is paper-only.")
        return 2
    Path(args.outdir).mkdir(parents=True, exist_ok=True)
    rep = asyncio.run(run(args))
    out = Path(args.outdir) / f"experiment-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(rep, indent=2, default=str) + "\n")
    _print(rep)
    print(f"\nreport: {out}")
    return 0


def _print(rep: dict) -> None:
    print("\n" + "=" * 74)
    print("ENTRY-CUTOFF PAPER EXPERIMENT")
    print("=" * 74)
    if "error" in rep:
        print("ERROR:", rep["error"]); print(json.dumps(rep.get("selection"), indent=2)); return
    print(f"spec {rep['spec']['spec_fingerprint']}  policies {rep['policy_fingerprint']}")
    c = rep["capture"]
    print(f"capture: {c['book_snapshots']} snapshots, {c['trades']} trades, "
          f"{c['stream_sec']:.0f}s stream, {c['fetch_errors']} errors")
    print(f"markets: {rep['selection']['per_stratum']}")
    print()
    hdr = f"{'arm':22} {'quotes':>7} {'fills':>6} {'rest_s':>7} {'rewardEst':>10} {'cash':>9} {'inv':>7}"
    print(hdr); print("-" * len(hdr))
    for name, a in rep["arms"].items():
        rew = sum(a["reward_estimates_only"].values())
        print(f"{name:22} {a['quoting']['quotes']:7d} "
              f"{a['execution']['modelled_fills']:6d} "
              f"{a['qualified_resting_stream_sec']:7.0f} "
              f"{rew:10.6f} {a['account']['cash_usd']:9.2f} "
              f"{len(a['inventory']):7d}")
    print(f"{'do_nothing (baseline)':22} {0:7d} {0:6d} {0:7.0f} {0:10.6f} "
          f"{SPEC.ACCOUNT_USD:9.2f} {0:7d}")
    print("\nrefusals by arm:")
    for name, a in rep["arms"].items():
        rs = a["opportunities"]["refusal_reasons"]
        top = sorted(rs.items(), key=lambda kv: -kv[1])[:5]
        print(f"  {name:22} {', '.join(f'{k}={v}' for k, v in top) or 'none'}")
    print("\ncaveats:")
    for c in rep["caveats"]:
        print(f"  - {c}")
    print("=" * 74)


if __name__ == "__main__":
    raise SystemExit(main())
