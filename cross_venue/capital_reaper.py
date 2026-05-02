#!/usr/bin/env python3
"""Capital Reaper v2 — NEXUS-OMNI D2.

Cross-venue stale-order pruner. Runs every 5 min via cron.

For each venue, fetches resting orders + current top-of-book.
Cancels any order:
  (a) older than MAX_AGE_SEC (default 600s = 10min) AND
  (b) more than STALE_TICKS_OFF below best (default 3 ticks)

Frees idle capital for redeploy. Doesn't touch at-best or near-best orders
(those are the ones earning rebate).

USAGE:
  python capital_reaper.py             # run check + cancel
  python capital_reaper.py --dry-run   # report only, no cancel
  python capital_reaper.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

MAX_AGE_SEC      = 600   # 10 min — generous, only catches truly stale
STALE_TICKS_OFF  = 3     # > 3 ticks below best = unlikely to fill, dead capital


def reap_kalshi(dry_run: bool) -> dict:
    """Subprocess to lip-maker venv. Cancels stale Kalshi orders."""
    script = f"""
import os, sys, time, json
sys.path.insert(0, '/root/lip-maker')
os.chdir('/root/lip-maker')
from execution.kalshi_auth import KalshiClient
c = KalshiClient()

# Fetch resting orders
r = c.get('/portfolio/orders', params={{'status':'resting','limit':200}})
orders = r.get('orders', [])
now = time.time()
stale = []
cancelled = 0
cancelled_tkrs = []
errors = 0

for o in orders:
    tkr = o.get('ticker', '')
    side = o.get('side', '?')
    oid = o.get('order_id', '')
    created = o.get('created_time', '')
    yp_c = o.get('yes_price', 0)
    np_c = o.get('no_price', 0)
    our_price = yp_c if side == 'yes' else np_c
    # Age
    try:
        from datetime import datetime
        ct = datetime.fromisoformat(created.replace('Z','+00:00'))
        age = (datetime.now(ct.tzinfo) - ct).total_seconds()
    except Exception:
        continue
    if age < {MAX_AGE_SEC}:
        continue

    # Get current best on this market+side
    try:
        ob = c.get(f'/markets/{{tkr}}/orderbook')
        book = ob.get('orderbook', {{}})
        if side == 'yes':
            levels = book.get('yes', [])  # [[price_cents, count], ...]
        else:
            levels = book.get('no', [])
        if not levels:
            continue
        best = max(int(l[0]) for l in levels) if levels else None
    except Exception:
        continue
    if best is None or our_price is None:
        continue

    # If our price is more than STALE_TICKS_OFF below best → reap
    ticks_off = best - int(our_price)
    if ticks_off > {STALE_TICKS_OFF}:
        stale.append({{'tkr': tkr, 'side': side, 'price': our_price, 'best': best,
                       'age_sec': round(age,0), 'ticks_off': ticks_off, 'oid': oid}})
        if not {dry_run}:
            try:
                c.delete(f'/portfolio/orders/{{oid}}')
                cancelled += 1
                cancelled_tkrs.append(tkr)
            except Exception as e:
                errors += 1

print(json.dumps({{
    'venue': 'kalshi',
    'orders_checked': len(orders),
    'stale_found': len(stale),
    'cancelled': cancelled,
    'cancelled_tkrs': cancelled_tkrs,
    'errors': errors,
    'samples': stale[:5],
}}))
"""
    try:
        r = subprocess.run(["/root/lip-maker/venv/bin/python", "-c", script],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().split("\n")[-1])
        return {"venue": "kalshi", "error": (r.stderr or r.stdout)[:200]}
    except Exception as e:
        return {"venue": "kalshi", "error": str(e)[:120]}


def reap_pm(dry_run: bool) -> dict:
    """Subprocess to PM venv. Cancels stale PM orders."""
    script = f"""
import os, sys, time, json
sys.path.insert(0, '/root/polymarket-maker')
os.chdir('/root/polymarket-maker')
from execution.pm_auth import _load_dotenv_simple
_load_dotenv_simple('/root/polymarket-maker/.env')
from polymarket_us import PolymarketUS
c = PolymarketUS(
    key_id=os.getenv('PM_API_KEY_ID'),
    secret_key=open('/root/polymarket-maker/config/polymarket_secret_key.b64').read().strip(),
)
o_resp = c.orders.list({{'limit':100}})
orders = o_resp.get('orders', []) if isinstance(o_resp, dict) else o_resp
stale = []
cancelled = 0
cancelled_slugs = []
errors = 0
now = time.time()

for ord_ in orders:
    oid = ord_.get('orderId') or ord_.get('id', '')
    slug = ord_.get('marketSlug', '')
    created = ord_.get('createdTime') or ord_.get('createdAt', '')
    intent = ord_.get('intent', '?')
    px_obj = ord_.get('price', {{}})
    px_str = px_obj.get('value') if isinstance(px_obj, dict) else px_obj
    if px_str is None:
        continue
    our_price = float(px_str)
    try:
        from datetime import datetime
        ct = datetime.fromisoformat(created.replace('Z','+00:00'))
        age = (datetime.now(ct.tzinfo) - ct).total_seconds()
    except Exception:
        continue
    if age < {MAX_AGE_SEC}:
        continue

    try:
        bbo = c.markets.bbo(slug)
        md = bbo.get('marketData', {{}}) if isinstance(bbo, dict) else {{}}
        best_bid = md.get('bestBid', {{}}).get('value')
        best_ask = md.get('bestAsk', {{}}).get('value')
        if best_bid is None or best_ask is None:
            continue
        best_bid = float(best_bid)
        best_ask = float(best_ask)
    except Exception:
        continue

    # Tick on PM is generally 0.001 (1/10 cent) for most markets — use 0.01 conservative
    PM_TICK = 0.01
    if 'BUY_LONG' in intent:
        ticks_off = (best_bid - our_price) / PM_TICK
    elif 'BUY_SHORT' in intent:
        # SHORT means selling YES; our 'price' is the NO price = (1-yes_sell)
        yes_sell_price = 1.0 - our_price
        ticks_off = (yes_sell_price - best_ask) / PM_TICK
    else:
        continue

    if ticks_off > {STALE_TICKS_OFF}:
        stale.append({{'slug': slug[:50], 'intent': intent, 'price': our_price,
                       'age_sec': round(age,0), 'ticks_off': round(ticks_off,1), 'oid': oid}})
        if not {dry_run}:
            try:
                c.orders.cancel(oid, {{'marketSlug': slug}})
                cancelled += 1
                cancelled_slugs.append(slug)
            except Exception as e:
                errors += 1

print(json.dumps({{
    'venue': 'pm',
    'orders_checked': len(orders),
    'stale_found': len(stale),
    'cancelled': cancelled,
    'cancelled_slugs': cancelled_slugs,
    'errors': errors,
    'samples': stale[:5],
}}))
"""
    try:
        r = subprocess.run(["/root/polymarket-maker/venv/bin/python", "-c", script],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().split("\n")[-1])
        return {"venue": "pm", "error": (r.stderr or r.stdout)[:200]}
    except Exception as e:
        return {"venue": "pm", "error": str(e)[:120]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    k = reap_kalshi(a.dry_run)
    p = reap_pm(a.dry_run)

    # 2026-05-02 PREDATOR K1: enqueue cancelled keys so runners can immediately
    # re-quote instead of waiting up to 30 min for the next discovery cycle.
    # Skipped on dry-run (no cancellations actually happened).
    if not a.dry_run:
        try:
            from cross_venue.requote_queue import enqueue
            for tkr in k.get("cancelled_tkrs", []) or []:
                enqueue("kalshi", tkr, reason="reaper_stale")
            for slug in p.get("cancelled_slugs", []) or []:
                enqueue("pm", slug, reason="reaper_stale")
        except Exception as e:
            print(f"[K1] requote_queue write failed: {e}")

    out = {"ts": datetime.now(timezone.utc).isoformat(),
           "dry_run": a.dry_run, "kalshi": k, "pm": p}

    if a.json:
        print(json.dumps(out, indent=2))
    else:
        prefix = "[DRY] " if a.dry_run else ""
        for venue_data in (k, p):
            v = venue_data.get("venue", "?")
            if "error" in venue_data:
                print(f"{prefix}🔴 {v}: {venue_data['error']}")
                continue
            chk = venue_data.get("orders_checked", 0)
            stale = venue_data.get("stale_found", 0)
            cancelled = venue_data.get("cancelled", 0)
            print(f"{prefix}✂️  {v}: {chk} resting orders → {stale} stale → {cancelled} cancelled")
            for s in venue_data.get("samples", []):
                tkr = s.get("tkr") or s.get("slug", "?")
                print(f"    {tkr[:45]:<45} age={s.get('age_sec',0):.0f}s ticks_off={s.get('ticks_off')}")

    total_cancelled = (k.get("cancelled", 0) + p.get("cancelled", 0))
    return 0 if total_cancelled >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
