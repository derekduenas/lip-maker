#!/usr/bin/env python3
"""One command: run the LIP paper maker continuously against real Kalshi data.

    python tools/run_session.py --duration 600 --top-n 20

What it does, in one process
----------------------------
  discovery        live /incentive_programs scan, persisted
  books            real orderbooks, polled and captured with timestamps
  quoting          the runner's economic selection decides size, or no quote
  fills            simulated causally from observed PUBLIC trades
  accounting       one shared $5,000 ledger: cash, reservations, inventory
  rewards          per-program estimates, never labelled as paid
  exits            the wired inventory exit policy
  report           a readable session report plus a machine-readable JSON

Paper only. No order ever reaches the exchange: PAPER_MODE stays on and the
live-execution interlock is independently armed.

Credentials
-----------
Discovery, orderbooks and trades are PUBLIC and need no key. The Kalshi
WebSocket does require one (an unauthenticated handshake is rejected 401),
so with no key configured this runs on REST snapshots and says so in the
report. That is a real limitation, not a formality: snapshots cannot show
queue position or the exact instant of a cross, so the fill evidence they
support is weaker than a WS run's.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from engine.lip_discovery import discover_result
from execution.kalshi_ws import FillEvent
from execution.paper_fills import PaperFillSimulator
from execution.rest_book_feed import RestBookFeed
import run_paper as rp
from run_paper import PaperRunner, _program_params_from_market

_log = logging.getLogger("session")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance(args) -> dict:
    """Configuration actually in force, plus the code identity, so a report
    can be tied to the thing that produced it."""
    src = Path(__file__).resolve().parent.parent
    def sha(p):
        try:
            return hashlib.sha256((src / p).read_bytes()).hexdigest()[:16]
        except Exception:
            return "unavailable"
    return {
        "started_utc": _utc(),
        "args": vars(args),
        "paper_mode": bool(settings.PAPER_MODE),
        "live_armed": bool(getattr(settings, "LIVE_ARMED", False)),
        "db_path": str(settings.DB_PATH),
        "opening_cash_usd": float(getattr(settings, "ACCOUNT_OPENING_CASH_USD", 5000.0)),
        "code_sha256": {
            "run_paper.py": sha("run_paper.py"),
            "engine/quote_economics.py": sha("engine/quote_economics.py"),
            "execution/paper_fills.py": sha("execution/paper_fills.py"),
            "execution/quote_manager.py": sha("execution/quote_manager.py"),
        },
        "fee_schedule": _fee_provenance(),
    }


def _fee_provenance() -> dict:
    try:
        from engine import fees
        s = fees.active_schedule()
        return {"name": s.name, "source": s.source, "verified": bool(s.verified)}
    except Exception as e:
        return {"error": str(e)}


def _ensure_tables() -> None:
    """Tables the runner creates lazily. Making them up front keeps a first
    session from failing halfway through on a missing table."""
    import sqlite3
    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fill_ledger (
                trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                ticker TEXT NOT NULL, side TEXT NOT NULL, count INTEGER NOT NULL,
                yes_price_cents INTEGER, no_price_cents INTEGER, is_taker INTEGER,
                created_at TEXT NOT NULL, synced_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_fill_ledger_ticker ON fill_ledger(ticker);
            CREATE TABLE IF NOT EXISTS settlement_log (
                ticker TEXT PRIMARY KEY, settled_at TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS market_blacklist (
                ticker TEXT PRIMARY KEY, expires_at TEXT, reason TEXT);
        """)
        conn.commit()
    finally:
        conn.close()


class _DiscoveryFromDb:
    """Shape-compatible with DiscoveryResult for a reuse run."""
    def __init__(self, programs):
        self.programs = programs
        # A reused scan is NOT fresh evidence about the universe. Marked
        # complete only so the runner's freshness gate can be satisfied for
        # a bounded commissioning run; the report says discovery was reused.
        self.complete = True
        self.n_rejected = 0
        self.n_collisions = 0
        self.errors = []
        self.started_ts = self.finished_ts = time.time()


def _discovery_from_db():
    import sqlite3
    conn = sqlite3.connect(settings.DB_PATH, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM lip_programs WHERE COALESCE(paid_out,0)=0").fetchall()
    finally:
        conn.close()
    return _DiscoveryFromDb([dict(r) for r in rows])


def _eligible(programs: list[dict], top_n: int) -> list[dict]:
    """Enrolled, currently-open programs, richest pool first."""
    now = time.time()
    out = []
    for p in programs:
        try:
            pp = _program_params_from_market(p)
        except Exception:
            continue
        if pp.start_ts and pp.start_ts > now:
            continue
        if pp.end_ts and pp.end_ts <= now:
            continue
        if p.get("paid_out"):
            continue
        if pp.period_reward_usd <= 0 or pp.target_size <= 0:
            continue
        out.append(p)
    # Rank by pool RATE, not total pool. A $200 pool spread over an 8-day
    # program pays $0.0003/sec; a $20 pool over a 15-minute program pays
    # $0.022/sec — seventy times more per second of quoting, for a fraction
    # of the capital commitment and the settlement risk. Sorting by total
    # pool systematically selected long-dated 2027 markets whose reward rate
    # (and whose time_factor) rounds to nothing.
    def rate(m):
        try:
            pp = _program_params_from_market(m)
            return pp.pool_rate_usd_per_sec
        except Exception:
            return 0.0
    out.sort(key=lambda m: -rate(m))
    return out[:top_n]


async def run_session(args) -> dict:
    prov = _provenance(args)
    cap_dir = Path(args.capture_dir)
    cap_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    book_cap = cap_dir / f"books-{stamp}.jsonl"
    trade_cap = cap_dir / f"trades-{stamp}.jsonl"

    _ensure_tables()
    if args.reuse_discovery:
        disc = _discovery_from_db()
        _log.info(f"reusing persisted discovery: {len(disc.programs)} programs")
    else:
        _log.info("discovering live LIP programs (full scan) ...")
        disc = discover_result(save=True)
    _log.info(f"discovery: {len(disc.programs)} programs, complete={disc.complete}, "
              f"rejected={disc.n_rejected}, collisions={disc.n_collisions}")
    markets = _eligible(disc.programs, args.top_n)
    if not markets:
        return {"provenance": prov, "error": "no eligible open programs"}
    _log.info(f"selected {len(markets)} markets; richest pool "
              f"${float(markets[0].get('period_reward_usd') or 0):.2f}")

    runner = PaperRunner(markets)
    runner.qm.paper = True
    runner.last_complete_scan_ts = time.time() if disc.complete else None
    sim = PaperFillSimulator(latency_ms=args.latency_ms, capture_path=str(trade_cap))
    feed = RestBookFeed(poll_interval_sec=args.poll_sec, capture_path=str(book_cap))
    runner.books = feed.books
    tickers = [m["market_ticker"] for m in markets]
    await feed.subscribe_orderbook(tickers)

    quote_log: list[dict] = []
    seen_orders: set[str] = set()

    async def on_book(book):
        try:
            await runner.on_book_update(book)
        except Exception as e:
            _log.debug(f"on_book_update {book.market_ticker}: {e}")
        # Track any newly-rested paper order for fill simulation.
        for o in runner.qm.resting.get(book.market_ticker, []):
            if o.order_id in seen_orders:
                continue
            seen_orders.add(o.order_id)
            sim.track(order_id=o.order_id, market_ticker=o.market_ticker,
                      side=o.side, price_cents=o.price_cents,
                      size=float(o.size_contracts), book=book,
                      program_id=getattr(o, "program_id", ""))
            quote_log.append({
                "utc": _utc(), "market_ticker": o.market_ticker,
                "side": o.side, "price_cents": o.price_cents,
                "size": float(o.size_contracts),
                "program_id": getattr(o, "program_id", ""),
                "why": runner._econ_last.get(o.market_ticker, {}),
            })

    feed.on_update(on_book)

    stop_at = time.time() + args.duration
    hb = asyncio.create_task(runner.heartbeat_snapshot_loop(feed, interval_sec=args.heartbeat_sec))
    feeder = asyncio.create_task(feed.run(stop_after_sec=args.duration))

    fills_applied = 0
    try:
        while time.time() < stop_at:
            await asyncio.sleep(args.trade_poll_sec)
            trades = await asyncio.to_thread(sim.fetch_trades, tickers)
            # Feed observed flow BEFORE applying fills: the fill-rate
            # estimate the economics uses must come from the market, not
            # from an assumption.
            runner.flow_stats.observe_trades(trades)
            for f in sim.apply_trades(trades):
                ev = FillEvent(order_id=f["order_id"], market_ticker=f["market_ticker"],
                               side=f["side"], count=float(f["count"]),
                               price_cents_exact=float(f["price_cents"]),
                               is_taker=False, trade_id=f["trade_id"], ts=f["ts"])
                try:
                    runner.on_fill(ev)
                    fills_applied += 1
                except Exception as e:
                    _log.warning(f"on_fill failed: {e}")
    finally:
        for t in (hb, feeder):
            t.cancel()
        await asyncio.gather(hb, feeder, return_exceptions=True)

    prov["finished_utc"] = _utc()
    return _report(runner, sim, feed, prov, markets, quote_log, fills_applied,
                   disc, book_cap, trade_cap)


def _report(runner, sim, feed, prov, markets, quote_log, fills_applied,
            disc, book_cap, trade_cap) -> dict:
    acct = runner.account.state()
    positions = {}
    for m in markets:
        t = m["market_ticker"]
        pos = runner._position_for(t)
        if pos is not None and (pos.yes_qty or pos.no_qty):
            positions[t] = {"yes": float(pos.yes_qty), "no": float(pos.no_qty),
                            "net_yes": float(pos.net_yes),
                            "paired": float(pos.paired)}
    rewards = {}
    for key, st in runner._accrual.items():
        rewards[key] = {"accrued_estimate_usd": round(st.accrued_usd, 6),
                        "breaks": st.breaks}
    return {
        "provenance": prov,
        "data_source": {
            "books": feed.source,
            "polls": feed.polls,
            "fetch_errors": feed.fetch_errors,
            "websocket_available": False,
            "limitation": ("REST snapshots: no sequence, no queue position, "
                           "no per-tick causality. Fill evidence is weaker "
                           "than a WebSocket run's."),
            "book_capture": str(book_cap),
            "trade_capture": str(trade_cap),
        },
        "discovery": {"programs_seen": len(disc.programs),
                      "complete": disc.complete,
                      "rejected": disc.n_rejected,
                      "collisions": disc.n_collisions,
                      "markets_selected": len(markets)},
        "quoting": {
            "quotes_placed": len(quote_log),
            "economic_rejects": runner.econ_rejects,
            "skip_counts": dict(runner.skip_counts),
            "capital_refusals": runner.qm.capital_refusals,
            "live_blocked": runner.qm.live_blocked,
            "quotes": quote_log[:200],
        },
        "execution": {
            "trades_observed": sim.trades_observed,
            "fills_simulated": sim.fills_generated,
            "fills_applied": fills_applied,
            "orders_still_resting": len(sim.orders),
        },
        "account": {
            "cash_usd": float(acct.cash_usd),
            "reserved_usd": float(acct.reserved_usd),
            "inventory_cost_usd": float(acct.inventory_cost_usd),
            "n_reservations": acct.n_reservations,
            "available_usd": float(runner.account.available_usd()),
        },
        "inventory": positions,
        "rewards_estimated_only": rewards,
        "exits": {"actions": runner.exit_actions,
                  "reasons": dict(runner._exit_reasons)},
        "observed_flow": runner.flow_stats.summary(),
        "economics_last_seen": {k: v for k, v in
                                list(runner._econ_last.items())[:20]},
        "caveats": [
            "Rewards are ESTIMATES computed from observed share. No reward "
            "has been paid or reconciled; do not read them as income.",
            "Fills are SIMULATED from public trades with a conservative "
            "queue model that ignores cancellations ahead of us, so it "
            "under-fills rather than over-fills.",
            "Paired inventory is riskless at settlement but is NOT spendable "
            "cash until the venue settles it.",
            "No profitability conclusion should be drawn from a single "
            "bounded session.",
        ],
    }


def _print_human(rep: dict) -> None:
    p = rep.get("provenance", {})
    print("\n" + "=" * 72)
    print("LIP PAPER MAKER — SESSION REPORT")
    print("=" * 72)
    if "error" in rep:
        print("ERROR:", rep["error"]); return
    print(f"started  {p.get('started_utc')}")
    print(f"finished {p.get('finished_utc')}")
    print(f"paper_mode={p.get('paper_mode')}  live_armed={p.get('live_armed')}")
    fs = p.get("fee_schedule", {})
    print(f"fees: {fs.get('name')} (verified={fs.get('verified')}, src={fs.get('source')})")
    d, q, e = rep["discovery"], rep["quoting"], rep["execution"]
    ds, a = rep["data_source"], rep["account"]
    print(f"\nDATA     books={ds['books']} polls={ds['polls']} errors={ds['fetch_errors']}")
    print(f"         {ds['limitation']}")
    print(f"\nDISCOVERY  {d['programs_seen']} programs, complete={d['complete']}, "
          f"selected {d['markets_selected']}")
    print(f"QUOTING    placed={q['quotes_placed']} econ_rejects={q['economic_rejects']} "
          f"capital_refusals={q['capital_refusals']} live_blocked={q['live_blocked']}")
    if q["skip_counts"]:
        top = sorted(q["skip_counts"].items(), key=lambda kv: -kv[1])[:6]
        print(f"           skips: {', '.join(f'{k}={v}' for k, v in top)}")
    print(f"EXECUTION  trades_observed={e['trades_observed']} "
          f"fills={e['fills_simulated']} applied={e['fills_applied']} "
          f"resting={e['orders_still_resting']}")
    print(f"ACCOUNT    cash=${a['cash_usd']:.2f} reserved=${a['reserved_usd']:.2f} "
          f"inventory=${a['inventory_cost_usd']:.2f} available=${a['available_usd']:.2f}")
    if rep["inventory"]:
        print("INVENTORY")
        for t, v in rep["inventory"].items():
            print(f"           {t}: yes={v['yes']:g} no={v['no']:g} "
                  f"net={v['net_yes']:g} paired={v['paired']:g}")
    else:
        print("INVENTORY  none")
    tot = sum(v["accrued_estimate_usd"] for v in rep["rewards_estimated_only"].values())
    print(f"REWARD     ${tot:.6f} ESTIMATED across "
          f"{len(rep['rewards_estimated_only'])} programs (never paid)")
    print(f"EXITS      actions={rep['exits']['actions']}")
    print("\nCAVEATS")
    for c in rep["caveats"]:
        print(f"  - {c}")
    print("=" * 72 + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=int, default=600, help="seconds to run")
    ap.add_argument("--top-n", type=int, default=20, help="markets to quote")
    ap.add_argument("--poll-sec", type=float, default=5.0, help="orderbook poll interval")
    ap.add_argument("--trade-poll-sec", type=float, default=10.0, help="trade poll interval")
    ap.add_argument("--heartbeat-sec", type=int, default=30)
    ap.add_argument("--latency-ms", type=float, default=250.0)
    ap.add_argument("--capture-dir", default="data/captures")
    ap.add_argument("--reuse-discovery", action="store_true",
                    help="use programs already in the DB instead of a full "
                         "scan (a full scan walks ~1,000 pages of history)")
    ap.add_argument("--report", default="", help="write the JSON report here")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.PAPER_MODE:
        print("REFUSING: PAPER_MODE is off. This command is paper-only.")
        return 2
    rep = asyncio.run(run_session(args))
    out = Path(args.report) if args.report else Path(
        args.capture_dir) / f"session-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, default=str) + "\n")
    _print_human(rep)
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
