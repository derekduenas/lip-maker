#!/usr/bin/env python3
"""One-screen performance scorecard from the engine's /status (read-only, paper).

    python tools/perf_summary.py [--url http://127.0.0.1:8765/status]
    curl -s 127.0.0.1:8765/status | python tools/perf_summary.py -

Everything here is an ESTIMATE from simulated paper fills; rewards are never paid money.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def _g(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _f(x, n=2):
    return "-" if x is None else (f"{x:.{n}f}" if isinstance(x, (int, float)) else str(x))


def render(s: dict) -> str:
    out = []
    pa = s.get("pnl_attribution") or {}
    out.append(f"MODE      {s.get('mode')}  armed={s.get('live_armed')}  books={s.get('data_source')}  kill={s.get('kill')}")
    out.append(f"P&L       total ${_f(s.get('pnl_usd'), 3)}  today MTM ${_f(s.get('daily_mtm_pnl_usd'), 3)}  (estimate, paper)")
    out.append(f"  spread ${_f(pa.get('spread_capture_usd'))}  adverse ${_f(pa.get('adverse_selection_usd'))}  "
               f"inventory MTM ${_f(pa.get('inventory_mtm_usd'))}  fees ${_f(pa.get('fees_usd'))}  "
               f"est rewards ${_f(pa.get('est_rewards_kalshi_usd'), 3)}")
    cap = s.get("capital") or {}
    kc = cap.get("kalshi") or {}
    if kc:
        flag = "  <<< STARVED: " + str(cap.get("hint")) if cap.get("starved") else ""
        out.append(f"CAPITAL   budget ${_f(kc.get('budget_usd'))} of ${_f(kc.get('cap_usd'), 0)}; locked ${_f(kc.get('locked_usd'))} "
                   f"(paired ${_f(kc.get('locked_paired_usd'))}, unpaired ${_f(kc.get('locked_unpaired_usd'))}); "
                   f"pair release {'on' if cap.get('pair_release') else 'off'}{flag}")
    acc = s.get("accrual_seconds") or {}
    known, idle = acc.get("known"), acc.get("idle")
    if known is not None and idle is not None and (known + idle) > 0:
        out.append(f"QUOTING   {100.0 * idle / (known + idle):.0f}% of market-seconds idle (not quoted); {known} quoted")
    k = (s.get("venues") or {}).get("kalshi") or {}
    out.append(f"ACTIVITY  resting {s.get('resting_n')}  selected {s.get('selected_n')}  quotes {s.get('quotes_n')}  "
               f"fills {s.get('fills_n')}  kalshi fills {k.get('fills_n')}")
    c = s.get("checkpoint") or {}
    ch, kf = c.get("checks") or {}, c.get("kalshi_fills") or {}
    out.append(f"OCT 6     {c.get('overall')}: real fills {_g(ch, 'real_kalshi_fills', 'value')}/{_g(ch, 'real_kalshi_fills', 'min')} "
               f"(sampling group {kf.get('from_sampling_group')}), skew pulls/day {_g(ch, 'clock_skew_pulls_per_day', 'value')}")
    m5 = c.get("markout_5m") or {}
    out.append(f"MARKOUT   5-min: {m5.get('fills')} fills, {_f(m5.get('cents_per_contract'))}c/contract (positive = mid moved our way)")
    gn = _g(s, "series_gate", "go_no_go") or {}
    st, v = gn.get("statistics") or {}, gn.get("verdict") or {}
    if v:
        out.append(f"OCT 10    {v.get('verdict')} ({v.get('why')}): {st.get('events')} independent events, edge mean "
                   f"{_f(v.get('edge_mean_cents'))}c, 90% lower {_f(v.get('edge_lower_90_cents'))}c, need ~{v.get('events_needed_for_target')} events; "
                   f"frozen {_f(v.get('frozen_days'), 1)}d of {_g(v, 'criteria', 'min_frozen_days')}d; "
                   f"pair release assumed: {_g(gn, 'assumptions', 'pair_release')}")
    for name, r in sorted((_g(s, "series_gate", "series") or {}).items(), key=lambda kv: -float(kv[1].get("net_usd") or 0))[:12]:
        out.append(f"  {name[:22]:22} fills {r.get('settled_fills')}/{r.get('fills')}  net ${_f(r.get('net_usd'))}  "
                   f"reward ${_f(r.get('reward_usd'))}  markout ${_f(r.get('markout_5m_cost_usd'))}  go={r.get('go')} {r.get('why')}")
    rr = s.get("rewards_reconciliation") or {}
    out.append(f"REWARDS   paid vs estimated: matched {rr.get('matched')}, ratio {rr.get('ratio')}  (no credit file = no paid data yet)")
    for a in (s.get("engine_alerts") or [])[-3:]:
        out.append(f"ALERT     {a.get('level')}: {str(a.get('message'))[:200]}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", nargs="?", default="", help="'-' to read JSON from stdin")
    ap.add_argument("--url", default="http://127.0.0.1:8765/status")
    args = ap.parse_args(argv)
    if args.src == "-":
        status = json.load(sys.stdin)
    else:
        with urllib.request.urlopen(args.url, timeout=10) as resp:
            status = json.loads(resp.read().decode("utf-8"))
    print(render(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
